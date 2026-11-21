"""The scheduled regression: every fault experiment (examples/*/experiments/*.yaml) on a lab cluster, like unit tests
for alerting. Run daily on the lab cluster by platform/experiments (a CronJob); by hand:

  python3 scripts/experiment-regression.py [--lab-label KEY=VALUE] [--kubeconfig F] [--context C]
      [--prometheus NS/SVC:PORT] [--record NS/CONFIGMAP] [--only a,b] [--dry-run] [DIR ...]

DIR: directories of experiment files (default: examples/*/experiments). Experiment names (file names) are unique
across them: a result is stored under its name.

Waves: one queue per namespace, the queues in parallel (they share nothing but the "no other page" judgement, which
the namespace marks of scripts/experiment.py handle). Within a namespace one experiment after the other: the
negative tests (nothing expected to fire) first, then the faults. Before each one the namespace must be free: no
experiment of anyone running (the lock), its cooldown over, no page firing there and none of the alerts the
experiment expects (a burn-rate alert of the previous fault would be counted for or against the next one). After each
fault the namespace stays marked for --gap (default 45 min, longer than experiment.py's 30: the 1h windows still hold
the fault). A namespace that is not free within --wait is skipped this time (a result, not a failure: said in the
summary). After the lab slept (Prometheus has a gap in the last hour: the 1h burn-rate windows would hold it) nothing
starts until the hour is clean; a run that the gap or a clock jump reaches anyway is INVALID (experiment.py),
recorded as such and not counted as pass or fail.

Not run, and said so: experiments with `skip: <reason>` (e.g. superseded, or cannot run on this lab), or whose
namespace does not exist on this cluster. The lab guard refuses to start unless every node carries --lab-label
(default slo-platform/lab=true): faults never reach another cluster by mistake, whatever kubectl's context is.

Exit 0 when every experiment was judged (failed experiments are results: --record and the metrics carry them);
exit 1 when the regression itself broke. --record keeps per experiment the last run (<name>.json, written by
experiment.py) and the regression's summary (_regression.json); platform/experiments exports both as metrics.
"""
import argparse
import datetime
import glob
import json
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import experiment  # noqa: E402
from experiment import COOLDOWN, MARK, STALE_S, dur, firing, kubectl  # noqa: E402

lock = threading.Lock()


def say(msg):
    with lock:
        print(f"{datetime.datetime.now():%H:%M:%S} [regression] {msg}", flush=True)


LAB_LABEL = "slo-platform/lab=true"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIRS = os.path.join(ROOT, "examples", "*", "experiments")


def parse_label(label):
    """'key=value' → (key, value); a bare key means value "true"."""
    k, sep, v = label.partition("=")
    if not k:
        raise ValueError(f"not a node label: {label!r}")
    return k, (v if sep else "true")


def is_lab(label, nodes):
    """Every node carries the lab label (set when the lab cluster is installed; a cluster that serves users never
    has it). No nodes: no lab."""
    k, v = parse_label(label)
    return bool(nodes) and all((n["metadata"].get("labels") or {}).get(k) == v for n in nodes)


def guard(label):
    """Faults only on the lab: refuse to start unless every node says it is one."""
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    if is_lab(label, nodes):
        return
    k, _ = parse_label(label)
    seen = [(n["metadata"]["name"], (n["metadata"].get("labels") or {}).get(k)) for n in nodes]
    sys.exit(f"refusing to inject faults: not every node has the label {label}; the nodes say {seen or 'nothing'}")


def experiment_files(dirs):
    """*.yaml of every directory (each may be a glob), sorted; two files with one name are an error."""
    files, seen = [], {}
    for d in dirs:
        for x in sorted(glob.glob(d)) or ([d] if os.path.isdir(d) else []):
            for f in sorted(glob.glob(os.path.join(x, "*.yaml"))):
                name = os.path.splitext(os.path.basename(f))[0]
                if name in seen:
                    raise ValueError(f"two experiments named {name}: {seen[name]} and {f} (results are stored by name)")
                seen[name] = f
                files.append(f)
    return files


def plan(files, only, existing=None):
    """(runnable experiments grouped by namespace, in order; skipped: name → (namespace, reason))"""
    if existing is None:
        existing = {n["metadata"]["name"] for n in json.loads(kubectl("get", "namespaces", "-o", "json"))["items"]}
    queues, skipped = {}, {}
    for f in files:
        name = os.path.splitext(os.path.basename(f))[0]
        if only and name not in only:
            continue
        x = experiment.load_yaml(f)
        ns = x["namespace"]
        if x.get("skip"):
            skipped[name] = (ns, f"skip: {x['skip']}")
        elif x.get("clusters"):   # an older format: a cluster name cannot be checked here
            skipped[name] = (ns, "`clusters:` is not supported: the lab label decides where faults run; use `skip:`")
        elif ns not in existing:
            skipped[name] = (ns, f"namespace {ns} does not exist here")
        else:
            queues.setdefault(ns, []).append((bool(x.get("expect", {}).get("fire")), name, f))
    return {ns: [(n, f) for _, n, f in sorted(q)] for ns, q in sorted(queues.items())}, skipped


def busy(ns, expect_fire):
    """Why the namespace cannot take the next experiment now (None: it can): locked, cooling down, a page firing there,
    or one of the alerts the experiment expects to fire is already firing (it could not be tested)."""
    a = json.loads(kubectl("get", "namespace", ns, "-o", "json"))["metadata"].get("annotations") or {}
    now = time.time()
    if MARK in a and now - float(a[MARK].split()[-1]) < STALE_S:
        return f"locked by {a[MARK]}"
    if COOLDOWN in a and now < float(a[COOLDOWN]):
        return f"cooling down for {(float(a[COOLDOWN]) - now) / 60:.0f} more min"
    loud = sorted({experiment.describe(x) for x in firing() if (x.get("namespace") == ns and x.get("severity") == "page")
                   or any(experiment.matches(x, w) for w in expect_fire)})
    if loud:
        return "still firing: " + ", ".join(loud)
    return experiment.prom_gap_reason(now - experiment.LOOKBACK_S, now)


class Regression:
    def __init__(self, a):
        self.a = a
        self.context = experiment.context_name()
        self.status = {}          # name → {namespace, status, reason?}
        self.started = time.time()
        self.broken = []

    def save(self, finished=None):
        if not self.a.record:
            return
        ns, cm = self.a.record.split("/")
        doc = {"context": self.context, "started": self.started, "finished": finished, "experiments": self.status}
        with lock:
            try:
                if experiment.subprocess.run(experiment.KUBE + ["get", "-n", ns, "configmap", cm], capture_output=True).returncode != 0:
                    kubectl("create", "-n", ns, "configmap", cm)
                kubectl("patch", "-n", ns, "configmap", cm, "--type", "merge", "-p",
                        json.dumps({"data": {"_regression.json": json.dumps(doc, ensure_ascii=False)}}))
            except RuntimeError as e:
                print(f"could not record the regression: {e}", flush=True)
                self.broken.append(str(e))

    def set(self, name, ns, status, reason=None):
        self.status[name] = {"namespace": ns, "status": status, **({"reason": reason} if reason else {})}
        self.save()

    def wait_free(self, ns, f):
        fire = experiment.load_yaml(f).get("expect", {}).get("fire", [])
        t, last = time.time(), None
        while not experiment.STOP.is_set():
            why = busy(ns, fire)
            if why is None:
                return None
            if why != last and not why.startswith("cooling"):
                say(f"{ns}: waiting ({why})")
            last = why
            if time.time() - t > dur(self.a.wait):
                return why
            time.sleep(30)
        return "stopped"

    def queue(self, ns, items):
        for name, f in items:
            try:
                for attempt in (1, 2):   # 2: someone took the lock between our check and ours
                    why = self.wait_free(ns, f)
                    if why:
                        self.set(name, ns, "skipped", "regression stopped (SIGTERM)" if why == "stopped"
                                 else f"namespace not free within {self.a.wait}: {why}")
                        say(f"skipped {name}: {self.status[name]['reason']}")
                        break
                    self.set(name, ns, "running")
                    r = experiment.Run(f, recover=True, context=self.context)
                    r.run()
                    if r.started is None and attempt == 1:
                        say(f"{name} not started ({r.not_started}): waiting again")
                        continue
                    if r.started is None:
                        self.set(name, ns, "skipped", f"not started: {r.not_started}")
                        break
                    if self.a.record:
                        try:
                            r.record(self.a.record)
                        except RuntimeError as e:
                            self.broken.append(f"{name}: could not record: {e}")
                    self.set(name, ns, r.status, r.invalid)
                    break
            except Exception as e:  # a crash of the scheduler around one experiment: that experiment is broken, go on
                self.broken.append(f"{name}: {e}")
                self.set(name, ns, "error", str(e))
            if experiment.STOP.is_set():
                break

    def run(self, queues, skipped):
        for name, (ns, why) in skipped.items():
            self.status[name] = {"namespace": ns, "status": "skipped", "reason": why}
        for ns, items in queues.items():
            for name, _ in items:
                self.status[name] = {"namespace": ns, "status": "pending"}
        self.save()
        threads = [threading.Thread(target=self.queue, args=(ns, items)) for ns, items in queues.items()]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):   # joined with a timeout: SIGTERM is handled in this thread
            for t in threads:
                t.join(timeout=1)
        if experiment.STOP.is_set():
            self.broken.append("stopped by SIGTERM")
            for s in self.status.values():
                if s["status"] in ("pending", "running"):
                    s.update(status="skipped", reason="regression stopped (SIGTERM)")
        self.save(finished=None if experiment.STOP.is_set() else time.time())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lab-label", default=LAB_LABEL, help="every node must carry this label (default: %(default)s)")
    p.add_argument("--kubeconfig", help="default: kubectl's (KUBECONFIG, ~/.kube/config, in-cluster)")
    p.add_argument("--context", help="default: the kubeconfig's current context (printed)")
    p.add_argument("--prometheus", default=experiment.PROMETHEUS, help="<namespace>/<service>:<port> (default: %(default)s)")
    p.add_argument("--record", metavar="NS/CONFIGMAP")
    p.add_argument("--gap", default="45m", help="a namespace stays marked this long after each experiment")
    p.add_argument("--wait", default="2h", help="skip an experiment whose namespace is not free within this")
    p.add_argument("--only", default="", help="comma-separated experiment names")
    p.add_argument("--dry-run", action="store_true", help="print the plan only")
    p.add_argument("dirs", nargs="*", default=[DEFAULT_DIRS], help="experiment directories, globs allowed "
                   "(default: examples/*/experiments)")
    a = p.parse_args()
    experiment.KUBE.extend(experiment.kube_args(a.kubeconfig, a.context))
    experiment.set_prometheus(a.prometheus)
    experiment.COOLDOWN_S = dur(a.gap)
    say(f"context: {experiment.context_name()}{'' if a.context else ' (current)'}; Prometheus: {experiment.PROMETHEUS}")
    guard(a.lab_label)
    try:
        files = experiment_files(a.dirs)
    except ValueError as e:
        sys.exit(str(e))
    queues, skipped = plan(files, set(filter(None, a.only.split(","))))
    for ns, items in queues.items():
        say(f"wave {ns}: " + " → ".join(n for n, _ in items))
    for name, (ns, why) in skipped.items():
        say(f"not run: {name} ({ns}): {why}")
    if a.dry_run:
        return
    signal.signal(signal.SIGTERM, lambda *_: experiment.STOP.set())
    experiment.keep_awake()
    reg = Regression(a)
    reg.run(queues, skipped)
    counts = {}
    for s in reg.status.values():
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    say("done: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    for name, s in sorted(reg.status.items()):
        print(f"{s['status'].upper():8} {name}  ({s['namespace']}){'  ' + s['reason'] if s.get('reason') else ''}")
    if reg.broken:
        print("the regression itself broke:\n  " + "\n  ".join(reg.broken))
        sys.exit(1)


if __name__ == "__main__":
    main()
