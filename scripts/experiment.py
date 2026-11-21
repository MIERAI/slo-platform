"""Fault experiments: inject a fault, check that exactly the expected alerts fire (and nothing else pages), remove it.
An alert that has never fired in an experiment is a hope, not a safety net (docs/concepts.md, 3).

  python3 scripts/experiment.py [--kubeconfig F] [--context C] [--prometheus NS/SVC:PORT] [--results DIR]
                                [--no-recover] [--record NS/CONFIGMAP] examples/<app>/experiments/<name>.yaml [...]

Several experiments run in parallel (give them different namespaces: they share the 1h burn-rate windows).
Experiment format (docs/experiments.md):

  hypothesis: what is broken           namespace: where (needs chaos-mesh.org/inject=enabled)
  fault: [objects]                     created in the namespace; deleted at the end, whatever happens. Each one must
                                       end by itself if the runner dies (a laptop asleep): Chaos Mesh objects with a
                                       spec.duration >= the experiment's time, or a fault-switch ConfigMap (the runner
                                       adds `expires`: the application ignores it after that); nothing else
  load: [Kubernetes objects]           optional: what the fault needs to matter (e.g. a Job that writes), created
                                       before the fault, deleted with it; not checked for injection (a Job needs
                                       activeDeadlineSeconds)
  timeout: 15m                         how long the fault stays at most
  expect:
    fire:  [{alert, labels: {...}, within: 10m}]   each must fire within its time
    quiet: [{alert, labels: {...}}]                these must not fire (besides: no other page may fire)
    may_fire: [{alert, labels: {...}}]             allowed, not required: neither expected nor unexpected
  recover: 10m                         after the fault: the fired alerts resolve within this (skip: --no-recover)
  ignore: '.+-(dev|staging)'           namespaces (regex) whose pages notify nobody on this cluster: not judged
  effect: {query: <promql>, within: 3m}  the fault must visibly bite (the query returns data) — Chaos Mesh's
                                       "AllInjected" only says the rules are in place, not that traffic hits them
                                       (a NetworkChaos on the client side, filtered on pod IPs, never matched Service traffic)

Namespaces are marked while an experiment runs (annotation slo-platform/experiment, which is also the lock: one
experiment per namespace, across processes and people) and for 30 min after (a burn-rate alert can still fire after
recovery). Pages in a marked namespace belong to its experiment; every other page fails every running experiment.

A run is INVALID — neither PASS nor FAIL — when time did not flow normally: the wall clock jumped more than 4 polls
between two polls (the machine slept, the VM paused), or Prometheus has a gap in the run or the hour before it (the
burn-rate windows). On macOS the runner keeps the machine awake while it runs (caffeinate; a closed lid still sleeps).

Exit 1 if any experiment failed, 2 if none failed but some were invalid. A report per experiment and day:
<results>/<date>-<name>.md (default results/ next to the experiment file); a second run on the same day appends a
section, it never replaces the first. --record also stores each result in a ConfigMap (key <name>.json: pass/fail,
when, detection and recovery times per expected alert), which platform/experiments exports as Prometheus metrics; the
scheduled regression (scripts/experiment-regression.py) uses it.
"""
import argparse
import datetime
import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required (apt install python3-yaml, or pip install pyyaml)")

PROMETHEUS = "monitoring/kps-prometheus:9090"   # <namespace>/<service>:<port>, reached through the API server's proxy
PROM = None                                      # the proxy path of PROMETHEUS (set_prometheus)
POLL = 15
KUBE = ["kubectl"]
lock = threading.Lock()
MARK = "slo-platform/experiment"               # <name> <host> <started epoch>
COOLDOWN = "slo-platform/experiment-cooldown"  # epoch until which late alerts still belong to the last experiment
COOLDOWN_S = 1800
STALE_S = 4 * 3600                                # a mark older than this is left over from a killed run
STOP = threading.Event()                          # set on SIGTERM: every running experiment removes its fault and ends
JUMP_S = 4 * POLL                                 # more than this between two polls: the clock jumped (sleep, VM paused)
PROM_GAP_S = 120                                  # a hole this long in Prometheus' own samples: the run is invalid
LOOKBACK_S = 3600                                 # the burn-rate windows (1h) reach this far back before a run
EXPIRY_MARGIN_S = 300                             # a fault switch expires this long after the experiment's time
LOGLINE = re.compile(r"^(\d\d):(\d\d):(\d\d) \[([^\]]+)\] (.*)$")
RUN_HEADING = re.compile(r"^## Run (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[+-]\d\d:\d\d|Z)?)")
VERDICTS = {"PASS", "FAIL", "INVALID"}


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def prom_path(spec):
    """'monitoring/kps-prometheus:9090' → the API server's service proxy path of Prometheus' HTTP API."""
    m = re.fullmatch(r"([a-z0-9-]+)/([a-z0-9-]+):(\d+)", spec or "")
    if not m:
        raise ValueError(f"not <namespace>/<service>:<port>: {spec!r}")
    return f"/api/v1/namespaces/{m.group(1)}/services/{m.group(2)}:{m.group(3)}/proxy/api/v1/"


def set_prometheus(spec):
    global PROMETHEUS, PROM
    PROM, PROMETHEUS = prom_path(spec), spec


set_prometheus(PROMETHEUS)


def kube_args(kubeconfig=None, context=None):
    """kubectl's global flags for an explicit kubeconfig / context (none: kubectl's own defaults)."""
    return (["--kubeconfig", os.path.expanduser(kubeconfig)] if kubeconfig else []) + (["--context", context] if context else [])


def context_name(kube=None):
    """The context kubectl uses ("in-cluster" inside a pod without a kubeconfig, "unknown" if kubectl cannot say)."""
    kube = kube or KUBE
    if "--context" in kube:
        return kube[kube.index("--context") + 1]
    r = subprocess.run(kube + ["config", "current-context"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return "in-cluster" if os.environ.get("KUBERNETES_SERVICE_HOST") else "unknown"


def parse_report(path, text, tz):
    """A results file (<date>-<name>[-suffix].md) → one dict per run section, oldest first:
    {experiment, start, end (epoch of the first / last log line), verdict, lines, context}. A section starts with
    `## Run <ISO start>` (its offset dates the log lines); text before the first one is a run of the older format,
    dated by the file name in tz. Log lines are HH:MM:SS: a time earlier than the one before means past midnight."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})-", os.path.basename(path))
    if not m:
        return []
    sections, cur = [], {"anchor": None, "tz": tz, "day": datetime.date.fromisoformat(m.group(1)), "lines": [], "context": None}
    for line in text.splitlines():
        h = RUN_HEADING.match(line)
        if h:
            sections.append(cur)
            try:
                at = datetime.datetime.fromisoformat(h.group(1))
            except ValueError:
                at = None
            cur = {"anchor": at, "tz": (at.tzinfo or tz) if at else tz, "day": at.date() if at else cur["day"],
                   "lines": [], "context": None}
            continue
        c = re.match(r"^Context: `([^`]*)`", line)
        if c:
            cur["context"] = c.group(1)
        cur["lines"].append(line)
    sections.append(cur)
    out = []
    for sec in sections:
        day, prev, name, times, msgs = sec["day"], None, None, [], []
        for line in sec["lines"]:
            lm = LOGLINE.match(line.rstrip())
            if not lm:
                continue
            hh, mi, ss, nm, msg = lm.groups()
            t = datetime.datetime.combine(day, datetime.time(int(hh), int(mi), int(ss)), sec["tz"])
            if prev and t < prev:          # past midnight
                day += datetime.timedelta(days=1)
                t += datetime.timedelta(days=1)
            prev = t
            name = name or nm
            times.append(t.timestamp())
            msgs.append(msg.strip())
        if not name:
            continue
        verdict = msgs[-1] if msgs[-1] in VERDICTS else "FAIL"   # a runner that died before its verdict did not pass
        out.append({"experiment": name, "start": min(times), "end": max(times), "verdict": verdict, "lines": msgs,
                    "context": sec["context"]})
    return out


def marked_namespaces():
    """Namespaces with an experiment running or cooling down (any process): their pages are theirs."""
    out, now = set(), time.time()
    for n in json.loads(kubectl("get", "namespaces", "-o", "json"))["items"]:
        a = n["metadata"].get("annotations") or {}
        if MARK in a and now - float(a[MARK].split()[-1]) < STALE_S:
            out.add(n["metadata"]["name"])
        elif COOLDOWN in a and now < float(a[COOLDOWN]):
            out.add(n["metadata"]["name"])
    return out


def dur(s):
    """'90s', '15m', '1h30m' → seconds"""
    if not re.fullmatch(r"(\d+[smh])+", str(s)):
        raise ValueError(f"not a duration: {s!r} (e.g. 15m, 1h30m)")
    return sum(int(n) * {"s": 1, "m": 60, "h": 3600}[u] for n, u in re.findall(r"(\d+)([smh])", str(s)))


def keep_awake():
    """macOS: no idle / system sleep while this process runs (caffeinate ends with it). A closed lid still sleeps."""
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"warning: could not keep the Mac awake (caffeinate): {e}", flush=True)


def largest_hole(start, end, ts, born=None):
    """The longest hole (seconds, where it began) between start and end in the sample timestamps ts. Before `born`
    (the oldest sample Prometheus holds) there is no hole, only no history: a freshly installed Prometheus is young,
    not asleep (its short history is the "young SLO" caveat, not missing data)."""
    if born is not None:
        start = min(max(start, born), end)
    pts = [start] + sorted(t for t in ts if start <= t <= end) + [end]
    return max((b - a, a) for a, b in zip(pts, pts[1:]))


def prom_gap(start, end):
    """The longest hole (seconds, where it began) in Prometheus' samples of itself between start and end."""
    job = PROMETHEUS.split("/")[1].split(":")[0]   # kube-prometheus-stack: Prometheus scrapes itself as job <service>
    q = urllib.parse.quote(f'up{{job="{job}", container="prometheus"}}[{max(60, int(end - start))}s]')
    out = json.loads(kubectl("get", "--raw", f"{PROM}query?query={q}&time={end:.3f}"))
    ts = [float(v[0]) for r in out["data"]["result"] for v in r["values"]]
    q = urllib.parse.quote(f'min(prometheus_tsdb_lowest_timestamp_seconds{{job="{job}"}})')
    low = json.loads(kubectl("get", "--raw", f"{PROM}query?query={q}&time={end:.3f}"))["data"]["result"]
    return largest_hole(start, end, ts, float(low[0]["value"][1]) if low else None)


def prom_gap_reason(start, end):
    """Why Prometheus' data between start and end cannot be trusted (None: it can)."""
    try:
        g, at = prom_gap(start, end)
    except Exception as e:  # cannot check: cannot vouch for the data either
        return f"could not check Prometheus for gaps: {e}"
    if g > PROM_GAP_S:
        return f"Prometheus has no samples for {g / 60:.0f} min from {datetime.datetime.fromtimestamp(at):%H:%M:%S} (machine asleep, VM paused)"
    return None


def fault_limit(x):
    """How long the fault may stay at most (the runner's deadline after creating it)."""
    return max([dur(x.get("timeout", "15m"))] + [dur(w.get("within", "10m")) for w in x.get("expect", {}).get("fire", [])])


def check_deadlines(x):
    """Every fault (and load Job) must end by itself when the runner dies: [problems]."""
    limit, out = fault_limit(x), []
    for o in x.get("fault") or []:
        what = f"{o.get('kind')}/{o.get('metadata', {}).get('name')}"
        if o.get("apiVersion", "").startswith("chaos-mesh.org/"):
            d = (o.get("spec") or {}).get("duration")
            if not d or dur(d) < limit:
                out.append(f"{what}: a Chaos Mesh fault needs spec.duration ≥ the experiment's {limit // 60} min (got {d})")
        elif o.get("kind") != "ConfigMap":
            out.append(f"{what}: a fault is a Chaos Mesh object (spec.duration) or a fault-switch ConfigMap (expires); "
                       "nothing else ends by itself when the runner dies")
    for o in x.get("load") or []:
        if o.get("kind") == "Job" and not (o.get("spec") or {}).get("activeDeadlineSeconds"):
            out.append(f"Job/{o.get('metadata', {}).get('name')} (load): needs activeDeadlineSeconds")
    return out


KEYS = {"hypothesis", "namespace", "fault", "load", "effect", "timeout", "hold", "expect", "recover", "ignore", "skip",
        "clusters", "generated"}   # clusters: an older format the regression reports; generated: generate-experiments.py


def check_format(x):
    """The experiment's shape (docs/experiments.md): [problems]. `generated: {from, template, version}` marks a file
    written by scripts/generate-experiments.py; the runner ignores it."""
    if not isinstance(x, dict):
        return ["not a mapping"]
    out = [f"unknown field {k!r}" for k in sorted(set(x) - KEYS)]
    for k in ("hypothesis", "namespace"):
        if not isinstance(x.get(k), str) or not x[k].strip():
            out.append(f"{k}: required (text)")
    if not isinstance(x.get("fault", []), list) or not isinstance(x.get("load", []), list):
        out.append("fault and load are lists")
    e = x.get("expect")
    if not isinstance(e, dict) or set(e) - {"fire", "quiet", "may_fire"}:
        out.append("expect: a mapping of fire / quiet / may_fire")
    else:
        for part in ("fire", "quiet", "may_fire"):
            for w in e.get(part) or []:
                if not isinstance(w, dict) or not isinstance(w.get("alert"), str) or not isinstance(w.get("labels", {}), dict):
                    out.append(f"expect.{part}: entries are {{alert, labels}}: {w!r}")
    for k in ("timeout", "hold", "recover"):
        if k in x:
            try:
                dur(x[k])
            except ValueError as err:
                out.append(f"{k}: {err}")
    if isinstance(x.get("effect"), dict) and not x["effect"].get("query"):
        out.append("effect: needs a query")
    g = x.get("generated")
    if g is not None and (not isinstance(g, dict) or set(g) != {"from", "template", "version"}):
        out.append("generated: {from: <slo>, template: <type>, version: <n>}")
    return out


def experiment_paths(d):
    """The experiment files of an application's experiments/ directory: hand-written (*.yaml) and generated
    (generated/*.yaml, scripts/generate-experiments.py)."""
    return sorted(glob.glob(os.path.join(d, "*.yaml"))) + sorted(glob.glob(os.path.join(d, "generated", "*.yaml")))


def default_results_dir(path):
    """Default reports directory: results/ next to the experiment; generated/<name>.yaml reports next to the
    hand-written ones (experiments/results/), where the scorecard and alert-report look."""
    d = os.path.dirname(os.path.abspath(path))
    return os.path.join(os.path.dirname(d) if os.path.basename(d) == "generated" else d, "results")


def kubectl(*args, stdin=None):
    r = subprocess.run(KUBE + list(args), input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def firing():
    """Every firing alert: [labels]."""
    q = urllib.parse.quote('ALERTS{alertstate="firing"}')
    out = json.loads(kubectl("get", "--raw", f"{PROM}query?query={q}"))
    return [r["metric"] for r in out["data"]["result"]]


def matches(alert, want):
    return alert.get("alertname") == want["alert"] and all(alert.get(k) == str(v) for k, v in want.get("labels", {}).items())


def quiet_matches(alert, q):
    """A quiet entry without a severity means "must not page": the slow-burn tickets (1d / 4d windows) of a lab
    namespace that gets a fault every day stay above 1x however well the SLO works, and are not what an experiment
    predicts. Name `severity` in the entry to hold a ticket quiet too."""
    if "severity" not in q.get("labels", {}) and alert.get("severity") != "page":
        return False
    return matches(alert, q)


def classify(a, fire, quiet, may_fire, ns, marked):
    """What a firing alert (not in the baseline, not ignored) means for an experiment in namespace ns:
    'quiet' (must stay quiet but fired), 'allowed' (may_fire), 'unexpected' (a page nobody expected), or None."""
    if any(quiet_matches(a, q) for q in quiet):
        return "quiet"
    if any(matches(a, w) for w in fire):
        return None
    if any(matches(a, m) for m in may_fire):
        return "allowed"
    if a.get("severity") == "page" and (a.get("namespace") == ns or a.get("namespace") not in marked):
        # judged here: pages in our namespace, and in namespaces no experiment (of anyone) marks
        # (a page next door is the neighbour's result, not noise caused by us)
        return "unexpected"
    return None


def describe(a):
    keep = ("namespace", "slo", "severity", "long", "short")
    return a["alertname"] + "{" + ", ".join(f"{k}={a[k]}" for k in keep if k in a) + "}"


class Run:
    def __init__(self, path, recover, context=None):
        self.path, self.recover, self.context = path, recover, context
        self.x = load_yaml(path)
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.ns = self.x["namespace"]
        self.log, self.ok = [], True
        self.started = None          # the namespace was locked (None: not started, e.g. busy)
        self.not_started = None      # why not
        self.t0 = self.finished = None
        self.fired, self.resolved, self.broken = {}, {}, set()   # fire index → s after the fault; quiet indices that fired
        self.invalid = None          # why the result means nothing (time did not flow normally)
        self.last_tick = None

    @property
    def status(self):
        return "invalid" if self.invalid else "pass" if self.ok else "fail"

    def tick(self):
        """Called at every poll: a jump of the wall clock (machine asleep, VM paused) invalidates the run."""
        now = time.time()
        if self.last_tick and now - self.last_tick > JUMP_S and not self.invalid:
            self.invalid = f"the clock jumped {(now - self.last_tick) / 60:.1f} min between two polls (machine asleep, VM paused)"
            self.say("⚠ " + self.invalid)
        self.last_tick = now

    def say(self, msg):
        line = f"{datetime.datetime.now():%H:%M:%S} [{self.name}] {msg}"
        with lock:
            print(line, flush=True)
        self.log.append(line)

    def fail(self, msg):
        self.ok = False
        self.say("✗ " + msg)

    def run(self):
        try:
            self.lock()
        except Exception as e:
            self.not_started = str(e)
            self.fail(f"not started: {e}")
            self.say("FAIL")
            return
        self.started = self.last_tick = time.time()
        try:
            self._run()
        except Exception as e:  # a crash is a failed experiment, never a silent pass
            if STOP.is_set():
                self.invalid = self.invalid or "stopped (SIGTERM) before the end"
            self.fail(f"runner error: {e}")
        finally:
            self.finished = time.time()
            self.unlock()
        if not self.invalid and self.t0:   # the burn-rate windows reach an hour back: a gap there skews them too
            self.invalid = prom_gap_reason(self.started - LOOKBACK_S, self.finished)
            if self.invalid:
                self.say("⚠ " + self.invalid)
        self.say(self.status.upper() + (f" ({self.invalid})" if self.invalid else ""))

    def lock(self):
        """Mark the namespace; kubectl annotate without --overwrite refuses an existing mark: the lock."""
        a = (json.loads(kubectl("get", "namespace", self.ns, "-o", "json"))["metadata"].get("annotations") or {})
        if MARK in a and time.time() - float(a[MARK].split()[-1]) >= STALE_S:
            kubectl("annotate", "namespace", self.ns, f"{MARK}-")   # left over from a killed run
        try:
            kubectl("annotate", "namespace", self.ns, f"{MARK}={self.name} {socket.gethostname()} {time.time():.0f}")
        except RuntimeError:
            raise RuntimeError(f"{self.ns} is busy: {a.get(MARK)} (one experiment per namespace)")

    def unlock(self):
        try:
            kubectl("annotate", "--overwrite", "namespace", self.ns, f"{COOLDOWN}={time.time() + COOLDOWN_S:.0f}", f"{MARK}-")
        except RuntimeError as e:
            self.say(f"could not unmark {self.ns}: {e}")

    def _run(self):
        x = self.x
        fire = x.get("expect", {}).get("fire", [])
        quiet = x.get("expect", {}).get("quiet", [])
        may_fire = x.get("expect", {}).get("may_fire", [])
        before = firing()
        # alerts firing before the fault (another problem, not ours) are not counted for or against it
        baseline = {json.dumps(a, sort_keys=True) for a in before}
        stale = [w for w in fire if any(matches(a, w) for a in before)]
        if stale:
            self.fail(f"expected alert already firing before the fault: {stale} — cannot test it now")
            return
        objs = x.get("fault") or []
        problems = check_format(x)
        if problems:
            raise RuntimeError("refused, not an experiment: " + "; ".join(problems))
        problems = check_deadlines(x)
        if problems:
            raise RuntimeError("refused, a fault must end by itself: " + "; ".join(problems))
        if any(not o.get("apiVersion", "").startswith("chaos-mesh.org/") for o in objs) and not x.get("effect"):
            raise RuntimeError("a fault that is not a Chaos Mesh object (e.g. a fault switch ConfigMap) needs `effect`: "
                               "nothing else proves it bites")
        manifest = self.manifest(objs)
        loads = x.get("load") or []
        load_manifest = self.manifest(loads)
        self.say(f"hypothesis: {x['hypothesis']}")
        ignore = re.compile(x["ignore"]) if x.get("ignore") else None

        def ignored(a):
            return bool(ignore and ignore.fullmatch(a.get("namespace", "")))

        if not objs:  # the negative test: a page already firing is noise too — unless another experiment's namespace
            marked = marked_namespaces() - {self.ns}   # (its pages are that experiment's result, as in the loop below)
            for a in before:
                if a.get("severity") == "page" and not ignored(a) and a.get("namespace") not in marked:
                    self.fail(f"page firing under normal conditions: {describe(a)}")   # once (baseline: not again)
        if loads:
            kubectl("apply", "-f", "-", stdin=load_manifest)
            self.say(f"load created in {self.ns}: " + ", ".join(f"{o['kind']}/{o['metadata']['name']}" for o in loads))
        t0 = self.t0 = time.time()
        if objs:
            expires = f"{t0 + fault_limit(x) + EXPIRY_MARGIN_S:.0f}"   # the fault switch ends even if we die now
            objs = [{**o, "data": {**(o.get("data") or {}), "expires": expires}} if o.get("kind") == "ConfigMap" else o for o in objs]
            manifest = self.manifest(objs)
            kubectl("apply", "-f", "-", stdin=manifest)
            self.say(f"fault created in {self.ns}: " + ", ".join(f"{o['kind']}/{o['metadata']['name']}" for o in objs))
        seen, unexpected = self.fired, {}
        try:
            if objs:
                self.wait_injected(objs)
                if x.get("effect"):
                    self.wait_effect(x["effect"])
            deadline = t0 + fault_limit(x)
            while time.time() < deadline:
                self.tick()
                if STOP.is_set():
                    raise RuntimeError("stopped (SIGTERM) before the end")
                now, marked = firing(), marked_namespaces()
                for i, w in enumerate(fire):
                    if i not in seen and any(matches(a, w) for a in now):
                        seen[i] = time.time() - t0
                        self.say(f"✓ fired after {seen[i] / 60:.1f} min: {w['alert']} {w.get('labels', {})}")
                for a in now:
                    k = json.dumps(a, sort_keys=True)
                    if k in baseline or k in unexpected or ignored(a):
                        continue
                    c = classify(a, fire, quiet, may_fire, self.ns, marked)
                    if c == "quiet":
                        self.broken |= {j for j, q in enumerate(quiet) if quiet_matches(a, q)}
                        unexpected[k] = f"must stay quiet but fired: {describe(a)}"
                    elif c == "unexpected":
                        unexpected[k] = f"unexpected page: {describe(a)}"
                    elif c == "allowed":
                        unexpected[k] = None   # said once
                        self.say(f"~ also fired (allowed): {describe(a)}")
                        continue
                    else:
                        continue
                    self.fail(unexpected[k])
                if len(seen) == len(fire) and time.time() - t0 >= dur(x.get("hold", "0s")):
                    break
                time.sleep(POLL)
        finally:
            if objs:
                self.remove(objs, manifest, t0)
            if loads:
                kubectl("delete", "--ignore-not-found", "--wait=false", "-f", "-", stdin=load_manifest)
                self.say("load removed")
        for i, w in enumerate(fire):
            if i not in seen:
                self.fail(f"did not fire within {w.get('within', '10m')}: {w['alert']} {w.get('labels', {})}")
            elif seen[i] > dur(w.get("within", "10m")):
                self.fail(f"fired too late ({seen[i] / 60:.1f} min > {w['within']}): {w['alert']} {w.get('labels', {})}")
        if self.recover and seen and x.get("recover"):
            t1, left = time.time(), set(seen)
            while left and time.time() - t1 < dur(x["recover"]) and not STOP.is_set():
                time.sleep(POLL)
                self.tick()
                now = firing()
                for i in list(left):
                    if not any(matches(a, fire[i]) for a in now):
                        left.discard(i)
                        self.resolved[i] = time.time() - t1
                        self.say(f"✓ resolved {(time.time() - t1) / 60:.1f} min after the fault: {fire[i]['alert']} {fire[i].get('labels', {})}")
            if STOP.is_set():
                self.invalid = self.invalid or "stopped (SIGTERM) during recovery"
            for i in left:
                if not STOP.is_set():
                    self.fail(f"still firing {x['recover']} after the fault: {fire[i]['alert']} {fire[i].get('labels', {})}")

    def manifest(self, objs):
        return "\n---\n".join(yaml.safe_dump({**o, "metadata": {**o.get("metadata", {}), "namespace": self.ns}}) for o in objs)

    def remove(self, objs, manifest, t0, limit=60):
        """Delete the fault without blocking on Chaos Mesh's finalizer (it hangs when the fault never got in, or the
        daemon is unreachable), then say plainly if it is still there."""
        kubectl("delete", "--ignore-not-found", "--wait=false", "-f", "-", stdin=manifest)
        t = time.time()
        while time.time() - t < limit:
            left = [o for o in objs if subprocess.run(KUBE + ["get", "-n", self.ns, o["kind"].lower(), o["metadata"]["name"]],
                                                      capture_output=True).returncode == 0]
            if not left:
                self.say(f"fault removed after {(time.time() - t0) / 60:.1f} min")
                return
            time.sleep(5)
        self.fail("fault still present after delete (finalizer): " + ", ".join(f"{o['kind']}/{o['metadata']['name']}" for o in left)
                  + " — check `kubectl -n " + self.ns + " get podnetworkchaos,podhttpchaos` and the chaos-daemon")

    def wait_injected(self, objs, limit=90):
        """Chaos Mesh accepts an object and may still inject nothing (a denied pause image, a daemon with an old
        certificate): then "no alert fired" would test nothing. Fail unless every object is AllInjected."""
        t = time.time()
        objs = [o for o in objs if o.get("apiVersion", "").startswith("chaos-mesh.org/")]   # others (a fault switch
        # ConfigMap) have no injection status: their `effect` query is the proof
        while objs:
            self.tick()
            pending = []
            for o in objs:
                st = json.loads(kubectl("get", "-n", self.ns, o["kind"].lower(), o["metadata"]["name"], "-o", "json")).get("status", {})
                if not any(c["type"] == "AllInjected" and c["status"] == "True" for c in st.get("conditions", [])):
                    events = [e["message"] for r in st.get("experiment", {}).get("containerRecords", []) for e in r.get("events", [])
                              if e.get("type") == "Failed"]
                    pending.append(f"{o['kind']}/{o['metadata']['name']}" + (f" ({events[-1]})" if events else ""))
            if not pending:
                self.say(f"✓ fault injected ({time.time() - t:.0f}s)")
                return
            if time.time() - t > limit or STOP.is_set():
                raise RuntimeError(f"fault not injected after {limit}s: {'; '.join(pending)}")
            time.sleep(5)

    def wait_effect(self, e):
        t = time.time()
        q = urllib.parse.quote(e["query"])
        while time.time() - t < dur(e.get("within", "3m")) and not STOP.is_set():
            self.tick()
            if json.loads(kubectl("get", "--raw", f"{PROM}query?query={q}"))["data"]["result"]:
                self.say(f"✓ fault takes effect ({time.time() - t:.0f}s): {e['query']}")
                return
            time.sleep(POLL)
        raise RuntimeError(f"fault injected but no effect within {e.get('within', '3m')}: {e['query']}")

    def result(self):
        """The run as data (for --record, and the metrics of platform/experiments)."""
        x, fire, quiet = self.x, self.x.get("expect", {}).get("fire", []), self.x.get("expect", {}).get("quiet", [])
        exp = []
        for i, w in enumerate(fire):
            ok = i in self.fired and self.fired[i] <= dur(w.get("within", "10m"))
            exp.append({"kind": "fire", "alert": w["alert"], "labels": {k: str(v) for k, v in w.get("labels", {}).items()},
                        "within_seconds": dur(w.get("within", "10m")), "met": bool(ok and self.t0),
                        "detection_seconds": round(self.fired[i], 1) if i in self.fired else None,
                        "recovery_seconds": round(self.resolved[i], 1) if i in self.resolved else None})
        for j, q in enumerate(quiet):
            exp.append({"kind": "quiet", "alert": q["alert"], "labels": {k: str(v) for k, v in q.get("labels", {}).items()},
                        "met": bool(self.t0) and j not in self.broken})
        return {"experiment": self.name, "namespace": self.ns, "success": self.ok, "status": self.status,
                "started": self.started, "fault_at": self.t0, "finished": self.finished,
                "expectations": exp, "report": "\n".join(self.log)}

    def record(self, where):
        """Store the result in a ConfigMap (namespace/name), key <name>.json; created if missing."""
        ns, cm = where.split("/")
        r = subprocess.run(KUBE + ["get", "-n", ns, "configmap", cm, "-o", "json"], capture_output=True, text=True)
        if r.returncode != 0:
            kubectl("create", "-n", ns, "configmap", cm)
            prev = {}
        else:
            prev = json.loads((json.loads(r.stdout).get("data") or {}).get(f"{self.name}.json") or "{}")
        if self.invalid:   # says nothing about the alerts: the last valid run stays the result
            res = {"experiment": self.name, "namespace": self.ns, **prev}
            res["last_invalid"] = {"finished": self.finished, "reason": self.invalid, "report": "\n".join(self.log)}
        else:
            res = self.result()
            # when it last passed, kept across failed runs ("verified by an experiment, and when")
            res["last_success"] = res["finished"] if self.ok else prev.get("last_success")
            res["last_invalid"] = prev.get("last_invalid")
        res["last_status"] = self.status
        kubectl("patch", "-n", ns, "configmap", cm, "--type", "merge", "-p",
                json.dumps({"data": {f"{self.name}.json": json.dumps(res, ensure_ascii=False)}}))

    def report(self, results_dir=None):
        """Write the run to <results_dir>/<date it started>-<name>.md (default: default_results_dir).
        The file holds every run of that day: a second run appends its own section and never replaces the first."""
        d = results_dir or default_results_dir(self.path)
        os.makedirs(d, exist_ok=True)
        began = datetime.datetime.fromtimestamp(self.started or time.time()).astimezone()
        f = os.path.join(d, f"{began.date()}-{self.name}.md")
        section = (f"## Run {began.isoformat(timespec='seconds')}: {self.status.upper()}\n\n"
                   + (f"Context: `{self.context}`.\n\n" if self.context else "")
                   + "```\n" + "\n".join(self.log) + "\n```\n")
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                prev = fh.read()
            text = prev + ("" if prev.endswith("\n") else "\n") + "\n" + section
        else:
            text = f"# {self.name}\n\n{' '.join(str(self.x['hypothesis']).split())}\n\n" + section
        with open(f, "w", encoding="utf-8") as out:
            out.write(text)
        return f


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kubeconfig", help="default: kubectl's (KUBECONFIG, ~/.kube/config, in-cluster)")
    p.add_argument("--context", help="default: the kubeconfig's current context (printed)")
    p.add_argument("--prometheus", default=PROMETHEUS, help="<namespace>/<service>:<port> (default: %(default)s)")
    p.add_argument("--results", metavar="DIR", help="where the reports go (default: results/ next to each experiment)")
    p.add_argument("--no-recover", action="store_true")
    p.add_argument("--record", metavar="NS/CONFIGMAP", help="also store each result in this ConfigMap (metrics: platform/experiments)")
    p.add_argument("files", nargs="+")
    a = p.parse_args()
    KUBE.extend(kube_args(a.kubeconfig, a.context))
    set_prometheus(a.prometheus)
    ctx = context_name()
    print(f"context: {ctx}{'' if a.context else ' (current)'}; Prometheus: {PROMETHEUS}", flush=True)
    keep_awake()
    runs = [Run(f, not a.no_recover, ctx) for f in a.files]
    if len({r.ns for r in runs}) < len(runs):
        sys.exit("two experiments in one namespace: they would share the burn-rate windows (run them one after the other)")
    threads = [threading.Thread(target=r.run) for r in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print()
    for r in runs:
        print(f"{r.status.upper():7}  {r.name}  ({os.path.relpath(r.report(a.results))}){'  ' + r.invalid if r.invalid else ''}")
        if a.record and r.started:
            r.record(a.record)
    sys.exit(1 if any(r.status == "fail" for r in runs) else 2 if any(r.invalid for r in runs) else 0)


if __name__ == "__main__":
    main()
