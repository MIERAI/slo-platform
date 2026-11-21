#!/usr/bin/env python3
"""Alert value report: which alerts fired, how often, how long, would they notify anyone, and was each firing useful.
docs/concepts.md, 2 (only an SLO burning fast pages: an unactionable page is a bug); meant for a monthly review.

  python3 scripts/alert-report.py [--kubeconfig F] [--context C] [--since 30d | --from ISO --to ISO]
                                  [--routing FILE] [-o reports/alerts/X.md]

Read-only: Prometheus and Alertmanager queries through the API server's service proxy, the namespaces' annotations,
and this repository's files.

Episodes: ALERTS{alertstate="firing"} (query_range, --step) per identity (alertname, namespace, slo, severity, long);
firing runs of the same identity less than --gap apart are one episode (a flap), the merged runs are counted.

Would it notify: an Alertmanager config, routes and inhibit rules applied to the episode's labels and to the other
alerts firing at the same time. --routing FILE (in a git repository: a plain Alertmanager config, Helm values with
alertmanager.config, or a kustomization patching .../alertmanager/config): the version in force when the episode
started (git log; merge time + 3 min ≈ Argo CD sync). Without it: the running Alertmanager's config (--alertmanager),
one version for the whole range. Neither readable → kube-prometheus-stack's default (every alert to the "null"
receiver: nobody is notified), said so in the report.

Classification, first match wins (the reasons are printed, so a human can disagree):
  label       a human said so: reports/alerts/labels.yaml (the monthly review resolves "unknown" there)
  experiment  a fault experiment marked that namespace: from its first log line to its last + the 30 min cooldown
              (results files of this context, see --results; slo_experiment_last_run_timestamp_seconds if exported;
              the namespaces' current marks); expected = matches the experiment's expect.fire, unexpected otherwise.
              Negative tests (no fault) explain nothing.
  incident    something independent of the alert was broken in that namespace around the onset (15 min before to
              15 min after): a container restarted, a running pod not ready, a scrape target down, a job failed, a
              ReplicaSet created (a rollout: change-related). Never the metric the alert's own rule reads.
  noise       nothing of the above, and: resolved in < --short (nobody could act), flapping (>= 3 runs merged), the
              SLO's long window held so few events that a handful of bad ones paged (low traffic), or an absent-metric
              alert right after Prometheus started (young data).
  unknown     none of these: the data cannot tell. Never guessed.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import glob
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required (apt install python3-yaml, or pip install pyyaml)")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)  # experiment.py (python3 -I does not add the script's dir)
import experiment  # noqa: E402

EXPERIMENTS = os.path.join(ROOT, "examples", "*", "experiments")
ALERTMANAGER = "monitoring/kps-alertmanager:9093"
IDENTITY = ("alertname", "namespace", "slo", "severity", "long")
ALWAYS_ON = {"Watchdog", "InfoInhibitor"}   # fire by design (dead man's switch, inhibition helper)
COOLDOWN_S = experiment.COOLDOWN_S         # scripts/experiment.py: late alerts still belong to the experiment
SYNC_S = 180                                # a merged overlay reaches Alertmanager after Argo CD's poll (3 min)
LOOKBEHIND_S = 900                          # incident evidence: from 15 min before the episode
CLASSES = ("experiment", "incident", "noise", "unknown")
# kube-prometheus-stack's Alertmanager config when values set none: every alert to "null"
DEFAULT_AM = {"route": {"receiver": "null", "routes": [{"receiver": "null", "matchers": ['alertname="Watchdog"']}]},
              "receivers": [{"name": "null"}], "inhibit_rules": []}


# ---------------------------------------------------------------- small helpers

def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def dur(s):
    """'30s' / '5m' / '1h' / '1d' / '2w' (or a number of seconds) → seconds."""
    if isinstance(s, (int, float)):
        return float(s)
    total, rest = 0.0, s.strip()
    for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)", rest):
        total += float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}[u]
    if not total and rest:
        raise ValueError(f"not a duration: {s}")
    return total


def fmt_dur(s):
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def union(iv):
    out = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def intersect(a, b):
    out, i, j = [], 0, 0
    a, b = union(a), union(b)
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def total(iv):
    return sum(e - s for s, e in union(iv))


# ---------------------------------------------------------------- Alertmanager routing

MATCHER = re.compile(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"?(.*?)"?\s*$')


def parse_matcher(s):
    m = MATCHER.match(s)
    if not m:
        raise ValueError(f"not a matcher: {s}")
    name, op, value = m.groups()
    return name, op, value.replace('\\"', '"')


def matches(labels, matchers):
    """Alertmanager semantics: a missing label is the empty string; regexes are anchored."""
    for m in matchers:
        name, op, value = parse_matcher(m) if isinstance(m, str) else m
        v = labels.get(name, "")
        if op == "=":
            ok = v == value
        elif op == "!=":
            ok = v != value
        else:
            ok = (re.fullmatch(value, v) is not None) == (op == "=~")
        if not ok:
            return False
    return True


def _route_matchers(r):
    ms = list(r.get("matchers") or [])
    ms += [f'{k}="{v}"' for k, v in (r.get("match") or {}).items()]          # deprecated forms
    ms += [f'{k}=~"{v}"' for k, v in (r.get("match_re") or {}).items()]
    return ms


def receivers(route, labels):
    """The receivers an alert reaches (Alertmanager's tree: first matching child, unless continue: true)."""
    out = []
    for child in route.get("routes") or []:
        if matches(labels, _route_matchers(child)):
            sub = receivers({**child, "receiver": child.get("receiver", route.get("receiver"))}, labels)
            out += sub
            if not child.get("continue"):
                return out
    return out or [route.get("receiver")]


def notifies(config, labels):
    """True if a receiver with any notifier configured gets the alert."""
    real = {r["name"] for r in config.get("receivers") or []
            if any(k.endswith("_configs") and r[k] for k in r)}
    return any(r in real for r in receivers(config["route"], labels))


def am_config_from_kustomization(text):
    """The alertmanager.config value a kustomization patches in (JSON 6902 operations), or None."""
    for p in (yaml.safe_load(text) or {}).get("patches") or []:
        ops = yaml.safe_load(p.get("patch") or "[]") or []
        if not isinstance(ops, list):   # a strategic-merge patch (e.g. $patch: delete), not JSON 6902 operations
            continue
        for op in ops:
            if op.get("path", "").endswith("/alertmanager/config"):
                return op["value"]
    return None


def am_config_from_text(text):
    """An Alertmanager config from a file: the config itself (route:), Helm values (alertmanager.config), or a
    kustomization patching it; None if the file holds none."""
    d = yaml.safe_load(text) or {}
    if not isinstance(d, dict):
        return None
    if "route" in d:
        return d
    am = d.get("alertmanager")
    if isinstance(am, dict) and isinstance(am.get("config"), dict):
        return am["config"]
    return am_config_from_kustomization(text)


def load_routing(path):
    """[(effective_from_epoch, config, version)] oldest first: every commit that changed the file (git), else the file."""
    path = os.path.realpath(path)   # git prints the real path of the top level (macOS: /var → /private/var)
    d, versions = os.path.dirname(path), []
    try:
        top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             check=True).stdout.strip()
        rel = os.path.relpath(path, top)
        log = subprocess.run(["git", "-C", top, "log", "--format=%H %ct", "--", rel],
                             capture_output=True, text=True, check=True).stdout.split("\n")
        for line in reversed([x for x in log if x.strip()]):
            sha, ts = line.split()
            text = subprocess.run(["git", "-C", top, "show", f"{sha}:{rel}"], capture_output=True, text=True,
                                  check=True).stdout
            versions.append((float(ts) + SYNC_S, am_config_from_text(text) or DEFAULT_AM, sha[:7]))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    if not versions:
        cfg = am_config_from_text(read(path)) if os.path.exists(path) else None
        versions = [(0.0, cfg or DEFAULT_AM, "worktree")]
    return versions


def live_routing(kube, alertmanager):
    """[(0, config, "live")] from the running Alertmanager (/api/v2/status), or None when unreachable."""
    ns, svc = alertmanager.split("/")
    url = f"/api/v1/namespaces/{ns}/services/{svc}/proxy/api/v2/status"
    p = subprocess.run(["kubectl", "--request-timeout=30s", *kube, "get", "--raw", url], capture_output=True, text=True)
    if p.returncode:
        return None
    try:
        cfg = yaml.safe_load(json.loads(p.stdout)["config"]["original"])
    except (ValueError, KeyError, TypeError, yaml.YAMLError):
        return None
    return [(0.0, cfg or DEFAULT_AM, "live")] if isinstance(cfg, dict) and "route" in cfg else None


def config_at(versions, t):
    cur = versions[0]
    for v in versions:
        if v[0] <= t:
            cur = v
    return cur


# ---------------------------------------------------------------- episodes

def series_runs(matrix, step):
    """query_range result → [(labels, [(start, end)])]; a run ends one step after its last sample."""
    out = []
    for r in matrix:
        labels = {k: v for k, v in r["metric"].items() if k not in ("__name__", "alertstate")}
        ts = sorted(float(t) for t, _ in r["values"])
        runs = []
        for t in ts:
            if runs and t - runs[-1][1] <= step * 0.5 + 1e-6:
                runs[-1][1] = t + step
            else:
                runs.append([t, t + step])
        out.append((labels, [tuple(x) for x in runs]))
    return out


def merge_series(parts):
    """The same series from several query_range chunks → one run list each."""
    by = {}
    for labels, runs in parts:
        k = json.dumps(labels, sort_keys=True)
        by.setdefault(k, (labels, []))[1].extend(runs)
    return [(labels, union(runs)) for labels, runs in by.values()]


def identity(labels):
    return tuple(labels.get(k, "") for k in IDENTITY)


def build_episodes(series, gap, range_start, range_end, step):
    """Group series by identity, merge runs closer than `gap` into episodes."""
    by = {}
    for labels, runs in series:
        if labels.get("alertname") in ALWAYS_ON:
            continue
        ent = by.setdefault(identity(labels), {"labels": dict(labels), "runs": []})
        ent["labels"] = {**labels, **ent["labels"]}
        ent["runs"].extend(runs)
    episodes = []
    for ident, ent in sorted(by.items()):
        runs = union(ent["runs"])
        groups = []
        for s, e in runs:
            if groups and s - groups[-1]["end"] <= gap:
                groups[-1]["end"] = max(groups[-1]["end"], e)
                groups[-1]["runs"].append((s, e))
            else:
                groups.append({"start": s, "end": e, "runs": [(s, e)]})
        for g in groups:
            episodes.append({
                "id": ident, "labels": ent["labels"], "start": g["start"], "end": min(g["end"], range_end),
                "runs": g["runs"], "firing_s": total(g["runs"]),
                "before_range": g["start"] <= range_start + step * 0.5,
                "ongoing": g["end"] >= range_end - step * 0.5,
            })
    return sorted(episodes, key=lambda e: (e["start"], e["id"]))


def apply_routing(episodes, series, versions):
    """notified: some firing time not inhibited, on a route with a real receiver (config in force at the start)."""
    for ep in episodes:
        t0, cfg, sha = config_at(versions, ep["start"])
        ep["routing"] = sha
        ep["routed"] = notifies(cfg, ep["labels"])
        inhibited = []
        for rule in cfg.get("inhibit_rules") or []:
            src = list(rule.get("source_matchers") or []) + [f'{k}="{v}"' for k, v in (rule.get("source_match") or {}).items()]
            tgt = list(rule.get("target_matchers") or []) + [f'{k}="{v}"' for k, v in (rule.get("target_match") or {}).items()]
            if not matches(ep["labels"], tgt):
                continue
            eq = rule.get("equal") or []
            for labels, runs in series:
                if identity(labels) == ep["id"]:
                    continue   # Alertmanager: an alert matching both sides does not inhibit itself
                if matches(labels, src) and all(labels.get(k, "") == ep["labels"].get(k, "") for k in eq):
                    inhibited += intersect(ep["runs"], runs)
        ep["inhibited_s"] = total(inhibited)
        ep["notified"] = ep["routed"] and ep["firing_s"] - ep["inhibited_s"] > 0
    return episodes


# ---------------------------------------------------------------- experiments

def spec_files(dirs):
    """{experiment name: file} of every experiment directory (globs allowed)."""
    out = {}
    for d in dirs:
        for f in [f for x in sorted(glob.glob(d)) for f in experiment.experiment_paths(x)]:   # with generated/
            out.setdefault(os.path.splitext(os.path.basename(f))[0], f)
    return out


def load_spec(specs, name):
    f = specs.get(name) if name else None
    return (yaml.safe_load(read(f)) or {}) if f else {}


def parse_results(path, tz, specs, context=None):
    """A runner report (<results>/<date>-<name>[-suffix].md) → one experiment window per run section. With `context`,
    only the runs recorded on that kubectl context."""
    out = []
    for r in experiment.parse_report(path, read(path), tz):
        if context is not None and r["context"] != context:
            continue
        spec = load_spec(specs, r["experiment"])
        has_fault = bool(spec.get("fault")) or any(x.startswith("fault created") for x in r["lines"])
        ns = spec.get("namespace")
        if not ns:
            fm = next((re.search(r"fault created in (\S+):", x) for x in r["lines"] if "fault created in" in x), None)
            ns = fm.group(1) if fm else None
        out.append({"name": r["experiment"], "file": os.path.relpath(path, ROOT), "namespace": ns, "start": r["start"],
                    "end": r["end"] + COOLDOWN_S, "fault": has_fault, "expect": spec.get("expect") or {},
                    "verdict": r["verdict"]})
    return out


def expectation(ep, x):
    want = lambda w: ep["labels"].get("alertname") == w.get("alert") and all(
        ep["labels"].get(k) == str(v) for k, v in (w.get("labels") or {}).items())
    if any(want(w) for w in x["expect"].get("fire") or []):
        return "expected"
    if any(want(w) for w in x["expect"].get("quiet") or []):
        return "must-stay-quiet"
    return "unexpected"


# ---------------------------------------------------------------- classification

def classify(ep, ctx):
    """→ (class, detail, [reasons]). ctx: experiments, evidence, traffic, tsdb_start, labels, short, rules."""
    L = ep["labels"]
    for lab in ctx.get("labels") or []:
        m = lab.get("match") or {}
        if all(L.get(k) == str(v) for k, v in m.items()) and \
                lab.get("from", 0) <= ep["start"] <= lab.get("to", float("inf")):
            return lab["class"], "labelled", [f"labelled: {lab.get('note', '')}".strip()]
    for x in ctx.get("experiments") or []:
        if x["fault"] is not False and x["namespace"] == L.get("namespace") and x["start"] <= ep["start"] <= x["end"]:
            detail = expectation(ep, x) if x["fault"] else "unverified"   # None: a mark without a name
            return "experiment", detail, [f"experiment {x['name']} ({x['verdict']}, {x['file']})"]
    circular = (ctx.get("circular") or {}).get(L.get("alertname"), set())
    ev = [x for x in ctx.get("evidence", {}).get(evidence_key(ep)) or [] if x.split(":")[0] not in circular]
    if ev:
        return "incident", "change" if all(e.startswith("rollout") for e in ev) else "fault", ev
    reasons = []
    if ep["firing_s"] < ctx.get("short", 180) and not ep["ongoing"]:
        reasons.append(f"short: resolved after {fmt_dur(ep['firing_s'])}, nobody could act")
    if len(ep["runs"]) >= 3:
        reasons.append(f"flapping: {len(ep['runs'])} runs merged")
    tr = ctx.get("traffic", {}).get(traffic_key(ep))
    if tr and tr["bad_needed"] < ctx.get("min_bad", 5):
        reasons.append(f"low traffic: {tr['events']:.0f} events in {L.get('long')}, "
                       f"{tr['bad_needed']:.1f} bad ones were enough")
    tsdb = ctx.get("tsdb_start")
    rule_for = (ctx.get("rules") or {}).get(rule_key(L), {}).get("for", 0)
    if tsdb and "Absent" in L.get("alertname", "") and ep["start"] - tsdb < rule_for + 600:
        reasons.append(f"young data: fired {fmt_dur(ep['start'] - tsdb)} after Prometheus' first sample")
    if reasons:
        return "noise", reasons[0].split(":")[0], reasons
    why = []
    if tsdb and L.get("long") and ep["start"] - tsdb < dur(L["long"]):
        why.append(f"the {L['long']} window was not full (Prometheus data from {fmt_dur(ep['start'] - tsdb)} before)")
    return "unknown", "", why or ["no experiment, no independent evidence, no noise signature"]


# the metric behind each kind of evidence: an alert built on it cannot be confirmed by it
EVIDENCE_METRIC = {"restart": "kube_pod_container_status_restarts_total", "not-ready": "kube_pod_status_ready",
                   "target-down": "up", "job-failed": "kube_job_status_failed", "rollout": "kube_replicaset_created"}


def circular_evidence(alerts):
    """{alertname: {evidence kinds its own rule reads}}."""
    out = {}
    for k, r in alerts.items():
        for kind, metric in EVIDENCE_METRIC.items():
            if re.search(rf"(?<![\w:]){re.escape(metric)}(?![\w:])", r.get("query", "")):
                out.setdefault(k[0], set()).add(kind)
    return out


def evidence_key(ep):
    """Evidence must be there around the onset: from 15 min before the start to 15 min after (or the end)."""
    return (ep["labels"].get("namespace", ""), round(ep["start"] - LOOKBEHIND_S), round(min(ep["end"], ep["start"] + LOOKBEHIND_S)))


def traffic_key(ep):
    L = ep["labels"]
    return (L.get("namespace", ""), L.get("slo", ""), L.get("long", ""), L.get("severity", ""), round(ep["start"]))


def rule_key(L):
    return (L.get("alertname", ""), L.get("namespace", ""), L.get("slo", ""), L.get("long", ""), L.get("severity", ""))


# ---------------------------------------------------------------- Prometheus

class Prom:
    """Prometheus' HTTP API through the API server's service proxy (kubectl get --raw)."""
    def __init__(self, kubeconfig=None, context=None, prometheus=experiment.PROMETHEUS):
        self.kube = experiment.kube_args(kubeconfig, context)
        self.base = experiment.prom_path(prometheus)

    def _get(self, path, params):
        url = self.base + path + "?" + urllib.parse.urlencode(params)
        p = subprocess.run(["kubectl", "--request-timeout=60s", *self.kube, "get", "--raw", url],
                           capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(p.stderr.strip()[:300])
        d = json.loads(p.stdout)
        if d.get("status") != "success":
            raise RuntimeError(str(d)[:300])
        return d["data"]

    def query(self, q, t=None):
        return self._get("query", {"query": q, **({"time": f"{t:.3f}"} if t else {})})["result"]

    def query_range(self, q, start, end, step):
        return self._get("query_range", {"query": q, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step:g}"})["result"]

    def rules(self):
        return self._get("rules", {})["groups"]


def fetch_alerts(prom, start, end, step, pool):
    """ALERTS{firing} over the range, in chunks under Prometheus' 11000 points per series."""
    chunk = step * 10000
    edges, t = [], start
    while t < end:
        edges.append((t, min(end, t + chunk)))
        t += chunk + step
    parts = []
    for res in pool.map(lambda e: prom.query_range('ALERTS{alertstate="firing"}', e[0], e[1], step), edges):
        parts += series_runs(res, step)
    return merge_series(parts)


def rule_info(groups):
    """Alert rules → {rule_key: {for, factor, objective}}; burn-rate recording rules → {(ns, slo, window): expr}."""
    alerts, records = {}, {}
    for g in groups:
        for r in g["rules"]:
            L = r.get("labels") or {}
            if r["type"] == "alerting":
                m = re.search(r">\s*\(\s*([\d.]+)\s*\*\s*\(\s*1\s*-\s*([\d.]+)\s*\)\s*\)", r["query"])
                ns = L.get("namespace") or (re.search(r'namespace="([^"]+)"', r["query"]) or [None, ""])[1]
                key = (r["name"], ns, L.get("slo", ""), L.get("long", ""), L.get("severity", ""))
                alerts[key] = {"for": float(r.get("duration") or 0), "query": r["query"],
                               **({"factor": float(m.group(1)), "objective": float(m.group(2))} if m else {})}
            else:
                m = re.match(r".+:burnrate(\w+)$", r["name"])
                if m and L.get("slo"):
                    records[(L.get("namespace", ""), L["slo"], m.group(1))] = r["query"]
    return alerts, records


def denominator(expr):
    """The right side of the top-level '/' of a Pyrra burn-rate expression (the SLI's events), or None."""
    depth = 0
    for i, c in enumerate(expr):
        depth += c == "("
        depth -= c == ")"
        if c == "/" and depth == 0:
            return expr[i + 1:].strip()
    return None


def gather_evidence(prom, ep):
    """Signals independent of the alert itself, in the episode's namespace, around its onset (evidence_key)."""
    ns, s, e = evidence_key(ep)
    if not ns:
        return []
    d = f"{max(60, int(e - s))}s"
    sel = f'namespace="{ns}"'
    out = []
    qs = {
        "restart": f"sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{sel}}}[{d}])) >= 1",
        "not-ready": f'max by (pod) (max_over_time((kube_pod_status_ready{{{sel},condition="false"}} * on(namespace, pod) '
                     f'group_left() (kube_pod_status_phase{{{sel},phase="Running"}} == 1))[{d}:1m])) > 0',
        "target-down": f"min by (job) (min_over_time(up{{{sel}}}[{d}])) == 0",
        "job-failed": f"max by (job_name) (max_over_time((kube_job_status_failed{{{sel}}} > 0 and on(namespace, job_name) "
                      f"kube_job_created{{{sel}}} > {s:.0f})[{d}:1m]))",
        "rollout": f"max by (replicaset) (max_over_time((kube_replicaset_created{{{sel}}} > {s:.0f})[{d}:1m]))",
    }
    for kind, q in qs.items():
        for r in prom.query(q, e):
            what = r["metric"].get("pod") or r["metric"].get("job") or r["metric"].get("job_name") \
                or r["metric"].get("replicaset") or "?"
            extra = f" ×{float(r['value'][1]):.0f}" if kind == "restart" else ""
            out.append(f"{kind}: {what}{extra}")
    return sorted(set(out))


def gather_traffic(prom, ep, alerts, records):
    L = ep["labels"]
    if L.get("alertname") != "ErrorBudgetBurn":
        return None
    rule = alerts.get(rule_key(L)) or {}
    expr = records.get((L.get("namespace", ""), L.get("slo", ""), L.get("long", "")))
    den = denominator(expr) if expr else None
    if not den or "factor" not in rule:
        return None
    res = prom.query(den, ep["start"])
    if not res:
        return None
    v = float(res[0]["value"][1])
    events = v * dur(L["long"]) if "rate(" in den else v
    return {"events": events, "bad_needed": events * rule["factor"] * (1 - rule["objective"])}


# ---------------------------------------------------------------- report

def name_of(ident):
    a, ns, slo, sev, long_ = ident
    extra = ", ".join(x for x in (slo, sev, long_ and f"long={long_}") if x)
    return f"{a} {ns or '-'}" + (f" ({extra})" if extra else "")


def app_of(ns, apps=None):
    """The application of a namespace: its slo-platform/app label (apps: {namespace: app}), else the namespace."""
    return (apps or {}).get(ns) or ns or "-"


def periodic(starts, tz):
    """Minute of the hour shared (±2 min) by >= 3 starts: something scheduled behind it."""
    if len(starts) < 3:
        return None
    mins = [dt.datetime.fromtimestamp(s, tz).minute for s in starts]
    dist = lambda a, b: min((a - b) % 60, (b - a) % 60)
    best = max(range(60), key=lambda m: (sum(dist(x, m) <= 2 for x in mins), -sum(dist(x, m) for x in mins)))
    near = sum(dist(x, best) <= 2 for x in mins)
    return best if near >= 3 and near >= 0.6 * len(mins) else None


def recommend(ident, eps, tz):
    """One line per alert identity (docs/concepts.md, 2 and 3). eps: its episodes (classified)."""
    sev = ident[3]
    notified = [e for e in eps if e["notified"]]
    real = [e for e in eps if e["class"] != "experiment"]
    c = {k: sum(e["class"] == k for e in real) for k in CLASSES}
    recs = []
    if real and c["noise"] >= max(1, 0.5 * len(real)):
        reasons = sorted({e["detail"] for e in real if e["class"] == "noise"})
        verb = "fix the SLI or demote to ticket" if sev == "page" else "fix the rule or delete it"
        recs.append(f"noise in {c['noise']}/{len(real)} ({', '.join(reasons)}): {verb} (an unactionable page is a bug)")
    if c["unknown"] and not c["incident"]:
        recs.append(f"{c['unknown']} unexplained: label them in reports/alerts/labels.yaml; if nobody acted, "
                    f"{'demote to ticket' if sev == 'page' else 'delete'}")
    changes = sum(e["class"] == "incident" and e["detail"] == "change" for e in real)
    if c["incident"] - changes:
        recs.append(f"{c['incident'] - changes} with an independent fault signal: keep; check the runbook's first step found it")
    if changes:
        recs.append(f"{changes} during a rollout only: was the change the cause? (promotion gate, canary analysis)")
    m = periodic([e["start"] for e in real], tz)
    if m is not None:
        recs.append(f"periodic: starts cluster at :{m:02d} of the hour — look for a schedule (CronJob) behind it")
    if not real and eps:
        recs.append("fired only in experiments: tested, nothing to do")
    if not recs:
        recs.append("—")
    if not notified and sev in ("page", "ticket"):
        recs.append("(notified nobody here)")
    return recs


def render(context, rng, episodes, info, tz):
    start, end = rng
    f = lambda t: dt.datetime.fromtimestamp(t, tz).strftime("%m-%d %H:%M")
    hours = (end - start) / 3600
    lines = [f"# Alert value report: {context}, {dt.datetime.fromtimestamp(start, tz):%Y-%m-%d %H:%M} – "
             f"{dt.datetime.fromtimestamp(end, tz):%Y-%m-%d %H:%M} ({tz})", ""]
    lines += [f"Generated by `scripts/alert-report.py` ({info['generated']}). Range: {fmt_dur(end - start)}"
              + (f" — requested from {f(info['requested_start'])}, Prometheus data only from {f(info['tsdb_start'])}"
                 if info.get("tsdb_start") and info["requested_start"] < info["tsdb_start"] else "") + ".", ""]
    lines += [f"- Routing: {info['routing']}", f"- Experiments known: {info['experiments']}",
              f"- Episode = `ALERTS{{alertstate=\"firing\"}}` runs (step {info['step']:g}s) merged when < "
              f"{fmt_dur(info['gap'])} apart; Watchdog / InfoInhibitor left out.", ""]
    for w in info.get("warnings") or []:
        lines.append(f"> ⚠ {w}")
    if info.get("warnings"):
        lines.append("")
    pages = [e for e in episodes if e["notified"] and e["labels"].get("severity") == "page"]
    tickets = [e for e in episodes if e["notified"] and e["labels"].get("severity") == "ticket"]
    nn = [e for e in episodes if e["notified"] and e["class"] != "experiment"]
    act = sum(e["class"] == "incident" for e in nn)
    unk = sum(e["class"] == "unknown" for e in nn)
    lines += ["## Headline", "",
              f"- {len(episodes)} episodes of {len({e['id'] for e in episodes})} alerts; "
              f"{sum(e['notified'] for e in episodes)} notified someone ({len(pages)} pages, {len(tickets)} tickets).",
              "- By class (all episodes): " + ", ".join(f"{k} {sum(e['class'] == k for e in episodes)}" for k in CLASSES) + ".",
              (f"- Actionable share of notifications outside experiments: {act}/{len(nn)}"
               + (f" (between {act / len(nn):.0%} and {(act + unk) / len(nn):.0%}: {unk} unknown)" if nn and unk else
                  (f" ({act / len(nn):.0%})" if nn else "")) + ".") if nn else
              "- No notification outside experiments in this range.", ""]
    weeks = max(hours / 168, 1e-9)
    per_app = {}
    for e in pages:
        per_app.setdefault(app_of(e["labels"].get("namespace"), info.get("apps")), []).append(e)
    if per_app:
        lines += ["Pages per application (target: < ~2 per week, all actionable)"
                  + (" — less than a day of data: no weekly rate" if hours < 24 else
                     " — extrapolated from less than a week" if hours < 168 else "") + ":", ""]
        lines += ["| Application | Pages | Per week | Actionable (incident) | Experiment | Noise | Unknown |", "|---|---|---|---|---|---|---|"]
        for app, es in sorted(per_app.items()):
            k = {c: sum(e["class"] == c for e in es) for c in CLASSES}
            lines.append(f"| {app} | {len(es)} | {f'{len(es) / weeks:.1f}' if hours >= 24 else '—'} | {k['incident']} | {k['experiment']} | {k['noise']} | {k['unknown']} |")
        lines.append("")
    by = {}
    for e in episodes:
        by.setdefault(e["id"], []).append(e)
    lines += ["## Per alert", "",
              "| Alert | Episodes | Firing | Notified | Experiment | Incident | Noise | Unknown | Actionable share |",
              "|---|---|---|---|---|---|---|---|---|"]
    order = sorted(by, key=lambda i: (-sum(e["notified"] for e in by[i]), -len(by[i]), -sum(e["firing_s"] for e in by[i])))
    for i in order:
        es = by[i]
        k = {c: sum(e["class"] == c for e in es) for c in CLASSES}
        n = [e for e in es if e["notified"] and e["class"] != "experiment"]
        share = "—" if not n else (f"{sum(e['class'] == 'incident' for e in n)}/{len(n)}")
        lines.append(f"| {name_of(i)} | {len(es)} | {fmt_dur(sum(e['firing_s'] for e in es))} | "
                     f"{sum(e['notified'] for e in es)} | {k['experiment']} | {k['incident']} | {k['noise']} | {k['unknown']} | {share} |")
    lines.append("")
    top = [i for i in order if any(e["notified"] for e in by[i])][:5]
    lines += ["## Top offenders and recommendations", ""]
    if not top:
        lines += ["Nothing notified anyone in this range. Recommendations for the alerts that fired:", ""]
        top = order[:8]
    for i in top:
        lines.append(f"- **{name_of(i)}** — {len(by[i])} episodes, {sum(e['notified'] for e in by[i])} notified: "
                     + "; ".join(recommend(i, by[i], tz)))
    rest = [i for i in order if i not in top]
    for i in rest:
        r = recommend(i, by[i], tz)
        if r and r[0] != "—":
            lines.append(f"- {name_of(i)}: " + "; ".join(r))
    never = info.get("never_fired") or []
    if never:
        lines += ["", f"Page rules that never fired in this range and have no experiment: {len(never)} "
                  f"(an alert that never fired is a hope). " + ", ".join(never[:12]) + (" …" if len(never) > 12 else "")]
    lines += ["", "## Episodes", "", "| Start | End | Alert | Firing | Notified | Class | Why |", "|---|---|---|---|---|---|---|"]
    for e in episodes:
        notified = ("yes" if e["notified"] else ("inhibited" if e["routed"] else "no"))
        if e.get("heard") is not None:
            notified += f", heard {e['heard']}×"
        t0 = ("≤" if e["before_range"] else "") + f(e["start"])
        t1 = "ongoing" if e["ongoing"] else f(e["end"])
        firing = fmt_dur(e["firing_s"]) + (" (%d runs)" % len(e["runs"]) if len(e["runs"]) > 1 else "")
        cls = e["class"] + (" / " + e["detail"] if e["detail"] else "")
        why = "; ".join(e["reasons"])[:300].replace("|", "/")
        lines.append(f"| {t0} | {t1} | {name_of(e['id'])} | {firing} | {notified} | {cls} | {why} |")
    lines += ["", "## How to read this", "",
              "- experiment: a fault experiment marked the namespace (start → end + 30 min cooldown); expected / "
              "unexpected per its `expect`. Results files count only for the kubectl context they name "
              "(`--results-any-context`: all).",
              "- incident: an independent signal in the namespace (restart, running pod not ready, target down, failed "
              "job, new ReplicaSet = rollout) from 15 min before the start to 15 min after. A coincidence (e.g. a routine rollout) "
              "counts too: read the reason.",
              "- noise: no experiment, no signal, and a noise signature (short, flapping, low traffic, absent right after "
              "Prometheus started). unknown: the data cannot tell — label it in `reports/alerts/labels.yaml`.",
              "- Notified: routing and inhibition of the overlay at the commit in force; Alertmanager's own state "
              "(silences, delivery failures, repeat_interval) is not modelled.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def load_labels(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    for x in yaml.safe_load(read(path)) or []:
        x = dict(x)
        for k in ("from", "to"):
            if k in x:
                v = x[k]
                x[k] = (v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))).timestamp()
        out.append(x)
    return out


def experiment_windows(results_files, tz, metric_runs, specs, marks=(), context=None):
    """Windows from the results files (only runs on `context`; None: all), the exported run metrics, and the
    namespaces' current marks (in that order of trust; a later source adds only runs the earlier ones do not cover).
    specs: {experiment name: file}."""
    xs = [x for p in results_files for x in parse_results(p, tz, specs, context)]
    for name, ns, t, took in metric_runs:   # slo_experiment_last_run_timestamp_seconds: when a run ended
        spec = load_spec(specs, name)
        if any(x["name"] == name and x["start"] <= t <= x["end"] for x in xs):
            continue
        took = took or dur(spec.get("timeout", "15m")) + dur(spec.get("recover", "0s"))
        xs.append({"name": name, "file": "metric slo_experiment_last_run_timestamp_seconds", "namespace": ns or spec.get("namespace"),
                   "start": t - took, "end": t + COOLDOWN_S, "fault": bool(spec.get("fault")),
                   "expect": spec.get("expect") or {}, "verdict": "?"})
    for name, ns, s, e in marks:   # slo-platform/experiment(-cooldown) on a namespace now: a run not recorded above
        if any(x["namespace"] == ns and x["start"] <= e and x["end"] >= s for x in xs):
            continue
        spec = load_spec(specs, name)
        xs.append({"name": name or "?", "file": "namespace annotation" + ("" if name else ", start approximate"),
                   "namespace": ns, "start": s, "end": e, "fault": bool(spec.get("fault")) if name else None,
                   "expect": spec.get("expect") or {}, "verdict": "?"})
    return xs


def namespace_info(kube, now):
    """From the namespaces: the runner's marks [(name, namespace, start, end)] (scripts/experiment.py: a running
    experiment's mark has its start; a cooldown only its end — the fault ended 30 min before, start assumed 1h
    earlier) and {namespace: application} (label slo-platform/app)."""
    p = subprocess.run(["kubectl", "--request-timeout=30s", *kube, "get", "namespaces", "-o", "json"],
                       capture_output=True, text=True)
    marks, apps = [], {}
    if p.returncode:
        return marks, apps
    for n in json.loads(p.stdout)["items"]:
        a = n["metadata"].get("annotations") or {}
        ns = n["metadata"]["name"]
        app = (n["metadata"].get("labels") or {}).get("slo-platform/app")
        if app:
            apps[ns] = app
        try:
            if experiment.MARK in a:
                name, _, started = a[experiment.MARK].split()
                marks.append((name, ns, float(started), now + COOLDOWN_S))
            elif experiment.COOLDOWN in a:
                e = float(a[experiment.COOLDOWN])
                marks.append((None, ns, e - COOLDOWN_S - 3600, e))
        except ValueError:
            continue
    return marks, apps


def metric_runs_of(prom, start, end, step):
    """Runs recorded as metrics (platform/experiments' exporter): (experiment, namespace, ended, took or None)."""
    try:
        res = prom.query_range("max by (experiment, namespace) (slo_experiment_last_run_timestamp_seconds)",
                               start - 6 * 3600, end, max(step, 60))
    except RuntimeError:
        return []
    first = {}
    for r in res:
        for t, v in r["values"]:
            k = (r["metric"].get("experiment", ""), r["metric"].get("namespace"), float(v))
            first.setdefault(k, float(t))
    out = []
    for (name, ns, v), t in sorted(first.items()):
        try:
            d = prom.query(f'max(slo_experiment_last_duration_seconds{{experiment="{name}"}})', t)
        except RuntimeError:
            d = []
        out.append((name, ns, v, float(d[0]["value"][1]) if d else None))
    return out


def results_files(dirs, given=None):
    """--results (a glob) or results/*.md of every experiment directory."""
    globs = [given] if given else [os.path.join(d, "results", "*.md") for d in dirs]
    return sorted({f for g in globs for f in glob.glob(g)})


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kubeconfig", help="default: kubectl's (KUBECONFIG, ~/.kube/config)")
    p.add_argument("--context", help="default: the kubeconfig's current context")
    p.add_argument("--prometheus", default=experiment.PROMETHEUS, help="<namespace>/<service>:<port> (default: %(default)s)")
    p.add_argument("--alertmanager", default=ALERTMANAGER,
                   help="<namespace>/<service>:<port>, its running config when --routing is not given (default: %(default)s)")
    p.add_argument("--routing", help="an Alertmanager config file in git (plain, Helm values or kustomization patch): "
                   "the version in force at each episode")
    p.add_argument("--since", default="30d")
    p.add_argument("--from", dest="from_", help="ISO time (default: now - since)")
    p.add_argument("--to", help="ISO time (default: now)")
    p.add_argument("--step", type=float, default=30)
    p.add_argument("--gap", default="5m", help="firing runs closer than this are one episode")
    p.add_argument("--short", default="3m", help="a notified episode shorter than this is noise")
    p.add_argument("--min-bad", type=float, default=5, help="fewer bad events than this paging = low traffic")
    p.add_argument("--experiments", nargs="*", default=[EXPERIMENTS], help="experiment directories, globs allowed "
                   "(default: examples/*/experiments)")
    p.add_argument("--results", help="results files, a glob (default: results/*.md in every experiment directory)")
    p.add_argument("--results-any-context", action="store_true",
                   help="use every results file, not only the runs recorded on this context")
    p.add_argument("--labels", default=os.path.join(ROOT, "reports/alerts/labels.yaml"))
    p.add_argument("--prod-env", default="prod", help="the environment whose untested, never-fired pages are listed")
    p.add_argument("--tz", help="time zone of the report (default: the system's)")
    p.add_argument("-o", "--output", help="Markdown file (default: stdout)")
    p.add_argument("--json", help="also write the episodes as JSON")
    p.add_argument("--workers", type=int, default=12)
    a = p.parse_args(argv)
    tz = load_tz(a.tz)
    kube = experiment.kube_args(a.kubeconfig, a.context)
    context = experiment.context_name(["kubectl", *kube])
    prom = Prom(a.kubeconfig, a.context, a.prometheus)
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    end = parse_time(a.to, tz) if a.to else now
    start = parse_time(a.from_, tz) if a.from_ else end - dur(a.since)
    warnings = []
    try:
        lowest = prom.query("min(prometheus_tsdb_lowest_timestamp_seconds)")
    except RuntimeError as e:
        sys.exit(f"{context}: Prometheus unreachable ({e})")
    tsdb_start = float(lowest[0]["value"][1]) if lowest else None
    requested = start
    if tsdb_start and start < tsdb_start:
        start = tsdb_start
        warnings.append(f"Prometheus keeps data only from {dt.datetime.fromtimestamp(tsdb_start, tz):%Y-%m-%d %H:%M}: "
                        f"the report covers {fmt_dur(end - start)}, not the {fmt_dur(end - requested)} asked for.")
    start = math.floor(start / a.step) * a.step + a.step
    if a.routing:
        versions = load_routing(a.routing)
        routing_src = f"{os.path.relpath(os.path.abspath(a.routing))}, {len(versions)} version(s)"
    else:
        versions = live_routing(kube, a.alertmanager)
        routing_src = f"the running Alertmanager's config ({a.alertmanager}), one version for the whole range"
        if versions is None:
            versions = [(0.0, DEFAULT_AM, "default")]
            routing_src = f"Alertmanager {a.alertmanager} unreachable"
            warnings.append(f"Alertmanager {a.alertmanager} not readable: routing assumed the chart default (nobody "
                            "notified); give --routing or --alertmanager.")
    specs = spec_files(a.experiments)
    files = results_files(a.experiments, a.results)
    with cf.ThreadPoolExecutor(a.workers) as pool:
        fut_rules = pool.submit(prom.rules)
        fut_runs = pool.submit(metric_runs_of, prom, start, end, a.step)
        fut_ns = pool.submit(namespace_info, kube, now)
        series = fetch_alerts(prom, start, end, a.step, pool)
        alerts, records = rule_info(fut_rules.result())
        metric_runs, (marks, apps) = fut_runs.result(), fut_ns.result()
        episodes = build_episodes(series, dur(a.gap), start, end, a.step)
        apply_routing(episodes, series, versions)
        experiments = experiment_windows(files, tz, metric_runs, specs, marks,
                                         None if a.results_any_context else context)
        ev_keys = {evidence_key(e): e for e in episodes}
        tr_keys = {traffic_key(e): e for e in episodes}
        ev = dict(zip(ev_keys, pool.map(lambda e: gather_evidence(prom, e), ev_keys.values())))
        tr = dict(zip(tr_keys, pool.map(lambda e: gather_traffic(prom, e, alerts, records), tr_keys.values())))
    ctx = {"circular": circular_evidence(alerts), "experiments": experiments, "evidence": ev, "traffic": tr, "tsdb_start": tsdb_start, "rules": alerts,
           "labels": load_labels(a.labels), "short": dur(a.short), "min_bad": a.min_bad}
    for e in episodes:
        e["class"], e["detail"], e["reasons"] = classify(e, ctx)
    never = never_fired(alerts, episodes, specs, a.prod_env)
    v = versions[-1]
    info = {"generated": dt.datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M %Z"), "requested_start": requested,
            "tsdb_start": tsdb_start, "step": a.step, "gap": dur(a.gap), "warnings": warnings, "apps": apps,
            "never_fired": never,
            "routing": (f"{routing_src}, latest {v[2]}"
                        + (" (no Alertmanager config: chart default, nobody is notified)" if v[1] is DEFAULT_AM else "")),
            "experiments": (f"{sum(x['start'] < end and x['end'] > start for x in experiments)} in range "
                            f"(of {len(experiments)} known: {len(files)} results files"
                            + ("" if a.results_any_context else f", runs on context {context} only")
                            + f"; {len(metric_runs)} runs from slo_experiment_* metrics; {len(marks)} namespace marks now)")}
    md = render(context, (start, end), episodes, info, tz)
    if a.output:
        os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
        with open(a.output, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
        print(f"wrote {a.output}: {len(episodes)} episodes", file=sys.stderr)
    else:
        print(md)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump([{k: v for k, v in e.items() if k != "id"} | {"identity": dict(zip(IDENTITY, e["id"]))}
                       for e in episodes], fh, indent=1, ensure_ascii=False)


def never_fired(alerts, episodes, specs, env="prod"):
    """Page rules (per SLO) of the `env` namespaces (<app>-<env>) with no episode in the range and no experiment that
    expects them to fire. specs: {experiment name: file}."""
    fired = {(e["labels"].get("alertname"), e["labels"].get("slo", "")) for e in episodes}
    tested = set()
    for f in specs.values():
        for w in ((yaml.safe_load(read(f)) or {}).get("expect") or {}).get("fire") or []:
            tested.add((w.get("alert"), (w.get("labels") or {}).get("slo", "")))
    return sorted({f"{k[0]} {k[2]} ({k[1]})" for k in alerts
                   if k[4] == "page" and (k[1] or "").endswith(f"-{env}") and (k[0], k[2]) not in fired | tested})


def load_tz(name=None):
    """A time zone by name (None: the system's)."""
    if not name:
        return dt.datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # no tz database
        if name.upper() == "UTC":
            return dt.timezone.utc
        sys.exit(f"unknown time zone {name!r} (no tz database? use UTC or the system's)")


def parse_time(s, tz):
    t = dt.datetime.fromisoformat(s)
    return (t if t.tzinfo else t.replace(tzinfo=tz)).timestamp()


if __name__ == "__main__":
    main()
