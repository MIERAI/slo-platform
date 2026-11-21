"""The experiments' results as Prometheus metrics: reads the ConfigMap experiment-results (mounted at RESULTS, written
by scripts/experiment.py --record and scripts/experiment-regression.py) at every scrape. No state of its own.

  <name>.json        the last run of one experiment (scheduled or by hand)
  _regression.json   the last scheduled regression: when, and what happened to each experiment

Labels join to the SLOs: namespace + slo (from each expected alert's labels).
"""
import glob
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

RESULTS = os.environ.get("RESULTS", "/results")
EXP = ("experiment", "namespace")
ALERT = EXP + ("kind", "alert", "slo", "severity", "long")

HELP = {
    "slo_experiment_last_success": "1 if the last valid run of the experiment passed (every expectation met, nothing else paged)",
    "slo_experiment_last_status": "the status of the last run, valid or not: pass / fail / invalid (time did not flow: sleep, Prometheus gap)",
    "slo_experiment_last_invalid_timestamp_seconds": "when the last invalid run ended (its result counts neither as pass nor fail)",
    "slo_experiment_last_run_timestamp_seconds": "when the last run of the experiment ended",
    "slo_experiment_last_success_timestamp_seconds": "when the experiment last passed (as far as the recorded runs go)",
    "slo_experiment_last_duration_seconds": "how long the last run took (fault, hold, recovery)",
    "slo_experiment_expectation_met": "1 if the expectation held in the last run (kind fire: fired in time; quiet: did not fire)",
    "slo_experiment_detection_seconds": "fault start to the expected alert firing, last run",
    "slo_experiment_recovery_seconds": "fault removed to the expected alert resolved, last run",
    "slo_experiment_regression_status": "the experiment's state in the last scheduled regression (status, reason)",
    "slo_experiment_regression_last_start_timestamp_seconds": "when the last scheduled regression started",
    "slo_experiment_regression_last_finished_timestamp_seconds": "when the last scheduled regression finished (absent: none finished)",
    "slo_experiment_regression_experiments": "experiments per status in the last scheduled regression",
}


def esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def metrics():
    out = {k: [] for k in HELP}

    def add(name, labels, value):
        if value is not None:
            out[name].append("%s{%s} %s" % (name, ",".join(f'{k}="{esc(v)}"' for k, v in labels.items()), float(value)))

    for f in sorted(glob.glob(os.path.join(RESULTS, "*.json"))):
        try:
            d = json.load(open(f))
        except (OSError, ValueError) as e:
            print(f"{f}: {e}", file=sys.stderr)
            continue
        if os.path.basename(f) == "_regression.json":
            add("slo_experiment_regression_last_start_timestamp_seconds", {"cluster": d.get("cluster", "")}, d.get("started"))
            add("slo_experiment_regression_last_finished_timestamp_seconds", {"cluster": d.get("cluster", "")}, d.get("finished"))
            counts = {}
            for name, s in sorted(d.get("experiments", {}).items()):
                add("slo_experiment_regression_status", {"experiment": name, "namespace": s["namespace"], "status": s["status"],
                                                          "reason": s.get("reason", "")}, 1)
                counts[s["status"]] = counts.get(s["status"], 0) + 1
            for status in ("pass", "fail", "invalid", "error", "skipped", "pending", "running"):
                add("slo_experiment_regression_experiments", {"status": status}, counts.get(status, 0))
            continue
        e = {"experiment": d["experiment"], "namespace": d["namespace"]}
        status = d.get("last_status") or ("pass" if d.get("success") else "fail")
        add("slo_experiment_last_status", {**e, "status": status}, 1)
        add("slo_experiment_last_invalid_timestamp_seconds", e, (d.get("last_invalid") or {}).get("finished"))
        if "success" not in d:   # only invalid runs so far: nothing verified, nothing broken
            continue
        add("slo_experiment_last_success", e, 1 if d["success"] else 0)
        add("slo_experiment_last_run_timestamp_seconds", e, d.get("finished"))
        add("slo_experiment_last_success_timestamp_seconds", e, d.get("last_success") or (d.get("finished") if d["success"] else None))
        if d.get("finished") and d.get("started"):
            add("slo_experiment_last_duration_seconds", e, d["finished"] - d["started"])
        for x in d.get("expectations", []):
            lab = x.get("labels", {})
            a = {**e, "kind": x["kind"], "alert": x["alert"], "slo": lab.get("slo", ""), "severity": lab.get("severity", ""),
                 "long": lab.get("long", "")}
            add("slo_experiment_expectation_met", a, 1 if x["met"] else 0)
            if x["kind"] == "fire":
                add("slo_experiment_detection_seconds", a, x.get("detection_seconds"))
                add("slo_experiment_recovery_seconds", a, x.get("recovery_seconds"))
    lines = []
    for name, rows in out.items():
        lines += [f"# HELP {name} {HELP[name]}", f"# TYPE {name} gauge", *rows]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body, ctype = b"ok\n", "text/plain"
        elif self.path == "/metrics":
            body, ctype = metrics().encode(), "text/plain; version=0.0.4"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("", int(sys.argv[1]) if len(sys.argv) > 1 else 8080), Handler).serve_forever()
