"""Capacity test: raise the load step by step until an SLO stops holding — the knee — and say how much one pod takes,
what ran out first, and (optional) how long the autoscaler takes to catch a jump. A capacity number not measured
against the SLOs is a guess about the wrong thing: "the CPU is at 80%" is not "users are fine".

  python3 scripts/capacity.py [--kubeconfig F] [--context C] [--prometheus NS/SVC:PORT] [--results DIR]
                              [--k6 CMD] [--address HOST] [--dry-run] <apps>/<app>/app.yaml <env>

What it does (docs/capacity.md):
  1. Lab guard: every node must carry slo-platform/lab=true (the load is meant to break things), and the namespace is
     marked like a fault experiment (slo-platform/experiment): one test or experiment per namespace at a time, and
     alerts that fire there belong to the test, not to an incident.
  2. The load is app.yaml's traffic mix (or capacity.requests), sent by k6 at a constant arrival rate (open loop: a
     slow service does not slow the load down). k6 runs where --k6 says: "k6" here, or e.g. "ssh loadgen k6" on a
     machine outside the cluster — the generator must not share CPU with what it measures. Outside the cluster the
     requests need an address: a temporary NodePort Service per target service (deleted at the end), reached at
     --address (default: the first node's InternalIP).
  3. Steps: start, start×factor, … (geometric) until a step fails, then `refine` linear steps between the last good
     and the first bad rate. Each step runs `step` long; its SLIs are computed from Prometheus over the step minus a
     warm-up (`warmup`), with the SLO's own definition (availability / latency / probe / ratio) and objective.
  4. A step is judged: holds (every SLI at or above its objective), breaks (one is not), or generator-limited (the
     server held, but k6 could not send what was asked: dropped iterations or < 90% of the rate) — the test cannot
     say anything above that rate, and says so instead of reporting a knee that is really the network's.
  5. Knee = the highest rate that held. Per pod = knee / ready pods of the first workload. Recommended = knee ×
     headroom. What ran out: CPU of each workload and of the nodes at the first breaking step against the last good.
  6. Optional (capacity.autoscaling): hold from×knee, jump to to×knee, and time the autoscaler: the HPA's decision,
     the new pods ready, the SLI back above its objective.

Report: <results>/<date>-<app>-<env>-capacity.md and .json (default: capacity/results/ next to app.yaml).
Exit 1 if it could not run, 2 if it ran but found no knee (generator-limited or max reached: capacity >= max).
"""
import argparse
import datetime
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import experiment as ex   # kubectl, Prometheus through the API server's proxy, the namespace mark, durations

LAB_LABEL = ("slo-platform", "lab", "true")
DEFAULTS = {"start": 5, "factor": 2, "max": 2000, "step": "90s", "warmup": "30s", "refine": 3, "confirm": 1, "headroom": 0.7}
KINDS = ("availability", "latency", "probe", "ratio")   # SLIs a step can be judged on (age / bool: no per-step meaning)
SCRAPE_WAIT_S = 20     # after a step: Prometheus scrapes every 15s, the step's last samples arrive this long after
LIMITED_SHARE = 0.9    # k6 sent less than this share of the rate: the generator (or the path to the target) is the limit


# --- configuration ---------------------------------------------------------------------------------------------

def config(app, env):
    """The capacity settings of app.yaml for env, defaults filled in; ValueError if they cannot work."""
    c = dict(app.get("capacity") or {})
    if env not in [e["name"] for e in app.get("environments", [])]:
        raise ValueError(f"{app['name']} has no environment {env}")
    ramp = {**DEFAULTS, **(c.get("ramp") or {})}
    for k in ("start", "factor", "max", "refine", "confirm", "headroom"):
        ramp[k] = float(ramp[k]) if k in ("factor", "headroom") else int(ramp[k])
    ramp["step_s"], ramp["warmup_s"] = ex.dur(str(ramp["step"])), ex.dur(str(ramp["warmup"]))
    if ramp["factor"] <= 1 or ramp["start"] < 1 or ramp["max"] < ramp["start"]:
        raise ValueError("capacity.ramp: need start >= 1, factor > 1, max >= start")
    if ramp["step_s"] - ramp["warmup_s"] < 45:
        raise ValueError("capacity.ramp: step - warmup must be at least 45s (three scrapes)")
    requests = c.get("requests") or (app.get("traffic") or {}).get("requests")
    if not requests:
        raise ValueError("no requests: capacity.requests, or traffic.requests")
    slos = {s["name"]: s for s in app.get("slos", [])}
    names = c.get("slos") or [n for n, s in slos.items() if any(k in s for k in KINDS) and env in s.get("environments", [env])]
    for n in names:
        if n not in slos:
            raise ValueError(f"capacity.slos: no slo {n}")
        if not any(k in slos[n] for k in KINDS):
            raise ValueError(f"capacity.slos: {n} is not one of {', '.join(KINDS)} (no per-step meaning)")
    workloads = c.get("workloads") or sorted({r["service"] for r in requests})
    auto = c.get("autoscaling")
    if auto:
        auto = {"workload": workloads[0], "from": 0.5, "to": 1.5, "hold": "2m", "watch": "8m", **auto}
        auto["hold_s"], auto["watch_s"] = ex.dur(str(auto["hold"])), ex.dur(str(auto["watch"]))
    return {"ramp": ramp, "requests": requests, "slos": [slos[n] for n in names], "workloads": workloads, "autoscaling": auto}


def rates(ramp):
    """The geometric rates of the first pass: start, start×factor, … up to max (max itself last)."""
    out, r = [], float(ramp["start"])
    while r < ramp["max"]:
        out.append(int(round(r)))
        r *= ramp["factor"]
    out.append(ramp["max"])
    return sorted(set(out))


def refine_rates(good, bad, n):
    """n rates evenly between the last good and the first bad rate (exclusive), rounded, distinct."""
    if n <= 0 or bad - good < 2:
        return []
    return sorted({int(round(good + (bad - good) * i / (n + 1))) for i in range(1, n + 1)} - {good, bad})


# --- SLIs ------------------------------------------------------------------------------------------------------

def sel(ns, matchers, extra=None):
    s = f'namespace="{ns}"' + (f", {matchers}" if matchers else "")
    return s + (f", {extra}" if extra else "")


def sli_query(slo, app, ns, w):
    """PromQL for the share of good events (0..1) over the last w seconds, the SLO's own definition
    (platform/slo/templates/slos.yaml)."""
    w = f"{int(w)}s"
    if "availability" in slo:
        a = slo["availability"]
        total = f'sum(increase({a["metric"]}{{{sel(ns, a.get("matchers"))}}}[{w}]))'
        bad = f'(sum(increase({a["metric"]}{{{sel(ns, a.get("matchers"), a["errors"])}}}[{w}])) or vector(0))'
        return f"1 - {bad} / {total}"
    if "latency" in slo:
        l = slo["latency"]
        good = f'sum(increase({l["metric"]}_bucket{{{sel(ns, l.get("matchers"), "le=" + json.dumps(str(l["threshold"])))}}}[{w}]))'
        return f'{good} / sum(increase({l["metric"]}_count{{{sel(ns, l.get("matchers"))}}}[{w}]))'
    if "probe" in slo:
        return f'avg(avg_over_time(probe_success{{namespace="{ns}", slo_probe="{app}-{slo["name"]}"}}[{w}]))'
    r = slo["ratio"]
    bad = f'(sum(increase({r["errors"]["metric"]}{{{sel(ns, r["errors"].get("matchers"))}}}[{w}])) or vector(0))'
    total = f'sum(increase({r["total"]["metric"]}{{{sel(ns, r["total"].get("matchers"))}}}[{w}]))'
    return f"1 - {bad} / {total}"


def query(q, at=None):
    """An instant query: {label-tuple: value}; one unlabelled result → {(): value}."""
    t = f"&time={at:.3f}" if at else ""
    out = json.loads(ex.kubectl("get", "--raw", f"{ex.PROM}query?query={urllib.parse.quote(q)}{t}"))
    res = {}
    for r in out["data"]["result"]:
        v = float(r["value"][1])
        res[tuple(sorted(r["metric"].items()))] = v
    return res


def scalar(q, at=None):
    v = list(query(q, at).values())
    return None if not v or math.isnan(v[0]) else v[0]


def server_side(cfg, app, ns, end, w):
    """What the cluster saw during a step: SLIs, request rate and CPU per workload, ready pods, node CPU."""
    s = {"slis": {}, "workloads": {}}
    for slo in cfg["slos"]:
        s["slis"][slo["name"]] = scalar(sli_query(slo, app, ns, w), end)
    for wl in cfg["workloads"]:
        pods = query(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{ns}", pod=~"{wl}-[a-z0-9]+(-[a-z0-9]+)?", container!=""}}[{int(w)}s]))', end)
        ready = scalar(f'sum(kube_deployment_status_replicas_available{{namespace="{ns}", deployment="{wl}"}})', end)
        cpu = sorted(pods.values())
        podsel = f'namespace="{ns}", pod=~"{wl}-[a-z0-9]+(-[a-z0-9]+)?"'
        restarts = scalar(f'sum(increase(kube_pod_container_status_restarts_total{{{podsel}}}[{int(w) + 30}s]))', end)
        oom = scalar(f'sum(kube_pod_container_status_last_terminated_reason{{{podsel}, reason="OOMKilled"}} '
                     f'* on (namespace, pod, container) (increase(kube_pod_container_status_restarts_total{{{podsel}}}[{int(w) + 30}s]) > 0))', end)
        min_ready = scalar(f'min_over_time(sum(kube_deployment_status_replicas_available{{namespace="{ns}", deployment="{wl}"}})[{int(w)}s:15s])', end)
        s["workloads"][wl] = {
            "rps": scalar(f'sum(rate(http_requests_total{{namespace="{ns}", app="{wl}"}}[{int(w)}s]))', end),
            "pods": int(ready) if ready is not None else len(cpu), "min_ready": min_ready,
            "cpu": sum(cpu), "cpu_max_pod": cpu[-1] if cpu else None,
            "restarts": round(restarts or 0), "oom_killed": round(oom or 0),
        }
    s["node_cpu"] = scalar(f'1 - avg(rate(node_cpu_seconds_total{{mode="idle"}}[{int(w)}s]))', end)
    # the neighbours: CPU used on the nodes besides the workloads under test — other namespaces' containers, and the
    # host itself (kubelet / k3s, the container runtime, swapping: outside every container). A knee where these grew
    # is the node's, not the application's.
    used = scalar(f'sum(rate(node_cpu_seconds_total{{mode!~"idle|iowait|steal"}}[{int(w)}s]))', end)
    per_ns = query(f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{container!="", namespace!=""}}[{int(w)}s]))', end)
    containers = sum(per_ns.values())
    others = sorted(((v, dict(k).get("namespace")) for k, v in per_ns.items() if dict(k).get("namespace") != ns), reverse=True)
    s["cores"] = scalar('count(node_cpu_seconds_total{mode="idle"})', end)
    s["used_cores"] = used
    s["host_cpu"] = max(0.0, used - containers) if used is not None else None   # outside every container
    s["neighbours"] = [{"namespace": n, "cpu": v} for v, n in others[:3]]
    s["swap_out"] = scalar(f'sum(rate(node_vmstat_pswpout[{int(w)}s]))', end)   # pages/s swapped out: memory pressure
    return s


# --- k6 --------------------------------------------------------------------------------------------------------

K6 = """import http from 'k6/http';
const REQUESTS = %(requests)s;
const TOTAL = REQUESTS.reduce((t, r) => t + r.weight, 0);
const rand = (s) => s.replace(/\\{rand\\}/g, () => String(Math.floor(Math.random() * 1000)));
export const options = {
  discardResponseBodies: true, summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
  scenarios: %(scenarios)s,
};
export default function () {
  let x = Math.random() * TOTAL;
  const r = REQUESTS.find((q) => (x -= q.weight) < 0) || REQUESTS[0];
  const params = { timeout: '10s', tags: { name: `${r.method} ${r.url}` } };
  let body = r.body;
  if (r.badBody !== undefined && Math.random() < (r.badRate || 0.05)) body = r.badBody;
  if (body !== undefined) {
    body = rand(typeof body === 'string' ? body : JSON.stringify(body));
    params.headers = { 'Content-Type': 'application/json' };
  }
  http.request(r.method, rand(r.url), body === undefined ? null : body, params);
}
export function handleSummary(d) {
  const m = (k, f) => (d.metrics[k] && d.metrics[k].values[f] !== undefined ? d.metrics[k].values[f] : null);
  return { stdout: '\\nSUMMARY ' + JSON.stringify({
    rps: m('http_reqs', 'rate'), reqs: m('http_reqs', 'count'), failed: m('http_req_failed', 'rate'),
    dropped: m('dropped_iterations', 'count'), p50: m('http_req_duration', 'p(50)'), p95: m('http_req_duration', 'p(95)'),
    p99: m('http_req_duration', 'p(99)'), vus_max: m('vus_max', 'max'),
  }) + '\\n' };
}
"""


def vus(rps):
    """VUs enough for the rate even when every request takes the 10s timeout would be too many: 2s of rate, 20..3000."""
    return max(20, min(3000, int(rps * 2)))


def k6_script(requests, scenarios):
    return K6 % {"requests": json.dumps(requests), "scenarios": json.dumps(scenarios)}


def constant(rps, seconds, start=0):
    return {"executor": "constant-arrival-rate", "rate": int(rps), "timeUnit": "1s", "duration": f"{int(seconds)}s",
            "startTime": f"{int(start)}s", "preAllocatedVUs": min(vus(rps), 200), "maxVUs": vus(rps)}


def run_k6(cmd, script):
    """Run k6 (cmd: a shell command line, e.g. "k6" or "ssh loadgen k6") with the script on stdin; its summary."""
    r = subprocess.run(cmd + " run --quiet --no-color -", shell=True, input=script, capture_output=True, text=True)
    for line in r.stdout.splitlines()[::-1]:
        if line.startswith("SUMMARY "):
            return json.loads(line[8:])
    raise RuntimeError(f"k6 gave no summary (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-600:]}")


# --- the target ------------------------------------------------------------------------------------------------

def expose(ns, services, address):
    """A temporary NodePort Service <svc>-capacity per target service: {service: base URL}."""
    urls = {}
    for svc in services:
        s = json.loads(ex.kubectl("get", "service", svc, "-n", ns, "-o", "json"))
        tmp = {"apiVersion": "v1", "kind": "Service",
               "metadata": {"name": f"{svc}-capacity", "namespace": ns, "labels": {"slo-platform/capacity": "true"}},
               "spec": {"type": "NodePort", "selector": s["spec"]["selector"],
                        "ports": [{"name": p.get("name", str(p["port"])), "port": p["port"], "targetPort": p.get("targetPort", p["port"])} for p in s["spec"]["ports"]]}}
        ex.kubectl("apply", "-f", "-", stdin=json.dumps(tmp))
        got = json.loads(ex.kubectl("get", "service", f"{svc}-capacity", "-n", ns, "-o", "json"))
        urls[svc] = {p["port"]: f"http://{address}:{p['nodePort']}" for p in got["spec"]["ports"]}
    return urls


def unexpose(ns):
    try:
        ex.kubectl("delete", "service", "-n", ns, "-l", "slo-platform/capacity=true", "--ignore-not-found")
    except RuntimeError as e:
        print(f"warning: temporary Services in {ns} not deleted: {e}", flush=True)


def node_address():
    nodes = json.loads(ex.kubectl("get", "nodes", "-o", "json"))["items"]
    for a in nodes[0]["status"]["addresses"]:
        if a["type"] == "InternalIP":
            return a["address"]
    raise RuntimeError("no InternalIP on the first node: give --address")


def lab_guard():
    k, v = f"{LAB_LABEL[0]}/{LAB_LABEL[1]}", LAB_LABEL[2]
    nodes = json.loads(ex.kubectl("get", "nodes", "-o", "json"))["items"]
    missing = [n["metadata"]["name"] for n in nodes if n["metadata"].get("labels", {}).get(k) != v]
    if missing:
        raise RuntimeError(f"not a lab: nodes without {k}={v}: {', '.join(missing)} (a capacity test breaks things on purpose)")


def resolve(requests, ns, urls):
    """app.yaml requests (service/port/path) → k6 requests with full URLs (urls: from expose, or None: in-cluster DNS)."""
    out = []
    for r in requests:
        base = urls[r["service"]][int(r["port"])] if urls else f"http://{r['service']}.{ns}.svc:{r['port']}"
        q = {k: v for k, v in r.items() if k in ("weight", "method", "body", "badBody", "badRate")}
        q["url"] = base + r.get("path", "/")
        out.append(q)
    return out


# --- judging ---------------------------------------------------------------------------------------------------

def judge(step, slos, workloads=()):
    """holds / breaks / limited, and why. Besides the SLIs: what the in-pod SLIs cannot see — every pod of a workload
    gone (OOMKilled, crash-looping: nothing left to count the failures), restarts, and the users' own view (k6's
    failed requests, which include timeouts and refused connections)."""
    c = step["client"]
    down = [wl for wl in workloads if (step["server"]["workloads"].get(wl, {}).get("min_ready") is not None
                                       and step["server"]["workloads"][wl]["min_ready"] < 1)]
    crashed = [f'{wl} {w["restarts"]} restarts' + (f' ({w["oom_killed"]} OOMKilled)' if w.get("oom_killed") else "")
               for wl in workloads for w in [step["server"]["workloads"].get(wl, {})] if w.get("restarts")]
    if down or crashed:
        return "breaks", "; ".join([f"{wl}: no pod ready (an outage the in-pod SLIs cannot count)" for wl in down] + crashed)
    worst = min((objective(s) for s in slos), default=0.99)
    if (c.get("failed") or 0) > 1 - worst:
        return "breaks", f'k6 saw {c["failed"] * 100:.2f}% of requests fail (timeouts, refused connections: users\' view)'
    broken = [f'{s["name"]} {step["server"]["slis"].get(s["name"]):.4f} < {objective(s):.4f}'
              for s in slos if step["server"]["slis"].get(s["name"]) is not None and step["server"]["slis"][s["name"]] < objective(s)]
    if broken:
        return "breaks", "; ".join(broken)
    if (c.get("dropped") or 0) > 0.01 * max(1, c.get("reqs") or 0) or (c.get("rps") or 0) < LIMITED_SHARE * step["rate"]:
        return "limited", f'k6 sent {c.get("rps") or 0:.1f}/s of {step["rate"]}/s ({int(c.get("dropped") or 0)} dropped)'
    missing = [s["name"] for s in slos if step["server"]["slis"].get(s["name"]) is None]
    if missing:
        return "breaks", f"no data for {', '.join(missing)} (nothing counted the requests)"
    return "holds", ""


def objective(slo):
    return float(slo["objective"]) / 100


def knee(steps):
    """The highest rate that held every time it ran, the lowest above it that broke (None: none broke), whether the
    generator limited. A rate run several times (confirm) holds only if every run held."""
    by = {}
    for s in steps:
        by.setdefault(s["rate"], []).append(s["verdict"])
    good = [r for r, v in by.items() if all(x == "holds" for x in v)]
    k = max(good) if good else None
    above = sorted(r for r, v in by.items() if "breaks" in v and (k is None or r > k))
    limited = any("limited" in v for v in by.values())
    return k, (above[0] if above else None), limited


def unstable(steps):
    """Rates whose runs disagreed (held once, broke once): the result depends on what else ran at the time."""
    by = {}
    for s in steps:
        by.setdefault(s["rate"], set()).add(s["verdict"])
    return sorted(r for r, v in by.items() if len(v) > 1)


def bottleneck(good, bad, workloads):
    """What changed between the last good and the first breaking step. The workloads' CPU first; when it did not grow
    (by 10% or more), the application's CPU was not what ran out: then the neighbours, the host, memory — or a
    resource CPU does not show (a lock, a pool, a downstream, the network): the question traces answer."""
    if not good or not bad:
        return []
    out, grew = [], False
    for wl in workloads:
        g, b = good["server"]["workloads"].get(wl, {}), bad["server"]["workloads"].get(wl, {})
        gc, bc = g.get("cpu") or 0, b.get("cpu") or 0
        grew = grew or (bc >= 1.1 * gc and bc - gc > 0.01)
        out.append(f"{wl}: CPU {fmt(gc, 3)} → {fmt(bc, 3)} cores ({b.get('pods')} pods)")
    gs, bs = good["server"], bad["server"]
    out.append(f"nodes: {fmt(gs.get('used_cores'), 2)} → {fmt(bs.get('used_cores'), 2)} of {fmt(bs.get('cores'), 0)} cores busy; "
               f"outside the containers (host, kubelet, swapping) {fmt(gs.get('host_cpu'), 2)} → {fmt(bs.get('host_cpu'), 2)}")
    if bs.get("neighbours"):
        out.append("busiest other namespaces at the break: " + ", ".join(f"{n['namespace']} {fmt(n['cpu'], 2)}" for n in bs["neighbours"]))
    if (bs.get("swap_out") or 0) > 1:
        out.append(f"the node was swapping: {fmt(bs['swap_out'], 0)} pages/s out")
    if not grew:
        out.append("→ the workloads' CPU did not grow: not their CPU. Suspect the neighbours / the host above, or a "
                   "resource CPU does not show (a pool, a lock, a downstream, the network).")
    return out


def fmt(v, d=1, pct=False):
    if v is None:
        return "—"
    return f"{v * 100:.{d - 1 if d > 1 else 0}f}%" if pct else f"{v:.{d}f}"


# --- autoscaling -----------------------------------------------------------------------------------------------

def hpa_for(ns, workload):
    items = json.loads(ex.kubectl("get", "hpa", "-n", ns, "-o", "json"))["items"]
    for h in items:
        ref = h["spec"]["scaleTargetRef"]
        if ref["name"] == workload and ref["kind"] == "Deployment":
            return h["metadata"]["name"]
    return None


def autoscaling(cfg, app, ns, k6, requests, k, say):
    """Hold from×knee, jump to to×knee; poll the HPA and the Deployment every 5s; then the SLI every 15s from
    Prometheus. Times are seconds after the jump."""
    a = cfg["autoscaling"]
    h = hpa_for(ns, a["workload"])
    if not h:
        return {"skipped": f"no HorizontalPodAutoscaler targets Deployment {a['workload']} in {ns}"}
    lo, hi = max(1, int(k * a["from"])), max(2, int(k * a["to"]))
    say(f"autoscaling: {lo}/s for {a['hold']}, then {hi}/s for {a['watch']} (HPA {h})")
    script = k6_script(requests, {"hold": constant(lo, a["hold_s"]), "jump": constant(hi, a["watch_s"], a["hold_s"])})
    box = {}
    t = threading.Thread(target=lambda: box.update(summary=run_k6(k6, script)))
    t0 = time.time()
    t.start()
    jump = t0 + a["hold_s"]
    timeline, first = [], {}
    while t.is_alive():
        try:
            hj = json.loads(ex.kubectl("get", "hpa", h, "-n", ns, "-o", "json"))
            dj = json.loads(ex.kubectl("get", "deployment", a["workload"], "-n", ns, "-o", "json"))
            row = {"t": round(time.time() - jump), "desired": hj["status"].get("desiredReplicas"),
                   "current": hj["status"].get("currentReplicas"), "ready": dj["status"].get("readyReplicas", 0)}
            timeline.append(row)
            base = timeline[0]["desired"] if timeline else None
            if row["t"] >= 0 and base is not None:
                if "decided" not in first and (row["desired"] or 0) > base:
                    first["decided"] = row["t"]
                if "decided" in first and "ready" not in first and row["ready"] >= (row["desired"] or 0):
                    first["ready"] = row["t"]
        except RuntimeError as e:
            say(f"autoscaling: poll failed: {e}")
        time.sleep(5)
    t.join()
    time.sleep(SCRAPE_WAIT_S)
    end = time.time()
    sli = {}
    for slo in cfg["slos"]:
        q = urllib.parse.quote(sli_query(slo, app, ns, 60))
        out = json.loads(ex.kubectl("get", "--raw", f"{ex.PROM}query_range?query={q}&start={jump:.0f}&end={end:.0f}&step=15"))
        pts = [(round(float(ts) - jump), float(v)) for r in out["data"]["result"] for ts, v in r["values"] if v != "NaN"]
        below = [ts for ts, v in pts if v < objective(slo)]
        back = next((ts for ts, v in pts if below and ts > below[0] and v >= objective(slo)), None)
        sli[slo["name"]] = {"breached_at": below[0] if below else None, "back_at": back, "worst": min((v for _, v in pts), default=None)}
    return {"hpa": h, "from_rps": lo, "to_rps": hi, "decided_s": first.get("decided"), "ready_s": first.get("ready"),
            "replicas": [timeline[0]["desired"], max((r["desired"] or 0) for r in timeline)] if timeline else None,
            "sli": sli, "client": box.get("summary"), "timeline": timeline}


# --- the run ---------------------------------------------------------------------------------------------------

class Test:
    def __init__(self, path, env, k6, address, results, dry):
        self.path, self.env, self.k6, self.address, self.dry = path, env, k6, address, dry
        self.app = ex.load_yaml(path)
        self.name, self.ns = self.app["name"], f'{self.app["name"]}-{env}'
        self.cfg = config(self.app, env)
        self.results = results or os.path.join(os.path.dirname(os.path.abspath(path)), "capacity", "results")
        self.steps, self.log, self.auto, self.unstable = [], [], None, []
        self.started = None

    def say(self, msg):
        line = f"{datetime.datetime.now():%H:%M:%S} [{self.ns}] {msg}"
        self.log.append(line)
        print(line, flush=True)

    def plan(self):
        r = self.cfg["ramp"]
        return rates(r)

    def step(self, rate, requests):
        r = self.cfg["ramp"]
        script = k6_script(requests, {"step": constant(rate, r["step_s"])})
        t0 = time.time()
        client = run_k6(self.k6, script)
        end = time.time()
        time.sleep(SCRAPE_WAIT_S)
        w = min(r["step_s"] - r["warmup_s"], end - t0 - r["warmup_s"])
        s = {"rate": rate, "start": t0, "end": end, "client": client, "server": server_side(self.cfg, self.name, self.ns, end, w)}
        s["verdict"], s["why"] = judge(s, self.cfg["slos"], self.cfg["workloads"])
        wl = self.cfg["workloads"][0]
        self.say(f'{rate:>5}/s  {s["verdict"]:<8} k6 {client.get("rps") or 0:.1f}/s p99 {(client.get("p99") or 0):.0f}ms  '
                 f'{wl} {s["server"]["workloads"][wl]["pods"]} pods CPU {fmt(s["server"]["workloads"][wl]["cpu"], 3)}  '
                 f'node {fmt(s["server"]["node_cpu"], 2, pct=True)} host {fmt(s["server"].get("host_cpu"), 2)}  {s["why"]}')
        self.steps.append(s)
        return s

    def run(self):
        ex.keep_awake()
        lab_guard()
        plan = self.plan()
        self.say(f"plan: {', '.join(map(str, plan))} req/s, {self.cfg['ramp']['step']} each; judged on {', '.join(s['name'] for s in self.cfg['slos'])}")
        if self.dry:
            return 0
        run = ex.Run.__new__(ex.Run)            # the experiments' namespace mark (lock + cool-down), nothing else of Run
        run.ns, run.name, run.say = self.ns, "capacity", self.say
        ex.Run.lock(run)
        self.started = time.time()
        try:
            services = sorted({q["service"] for q in self.cfg["requests"]})
            urls = expose(self.ns, services, self.address or node_address())
            self.say(f"target: {', '.join(f'{s} {u}' for s, ps in urls.items() for u in ps.values())}")
            requests = resolve(self.cfg["requests"], self.ns, urls)
            bad = None
            for rate in plan:
                s = self.step(rate, requests)
                if s["verdict"] != "holds":
                    bad = s
                    break
            k, _, _ = knee(self.steps)
            if bad and bad["verdict"] == "breaks":
                self.say("breaking step: cooling down 60s, then refining")
                time.sleep(60)
                for rate in refine_rates(k or 0, bad["rate"], self.cfg["ramp"]["refine"]):
                    s = self.step(rate, requests)
                    if s["verdict"] != "holds":
                        break
            k, first_bad, limited = knee(self.steps)
            # confirm: the knee and the first break once more each — one noisy minute must not make the result
            for _ in range(self.cfg["ramp"]["confirm"]):
                for rate in [r for r in (k, first_bad) if r]:
                    self.say(f"confirming {rate}/s")
                    self.step(rate, requests)
            self.unstable = unstable(self.steps)
            if self.unstable:
                self.say(f"unstable: runs at {', '.join(map(str, self.unstable))} req/s disagreed")
            k, first_bad, limited = knee(self.steps)
            if self.cfg["autoscaling"] and k:
                time.sleep(60)
                self.auto = autoscaling(self.cfg, self.name, self.ns, self.k6, requests, k, self.say)
        finally:
            unexpose(self.ns)
            ex.Run.unlock(run)
        return 0 if knee(self.steps)[1] else 2

    def summary(self):
        k, first_bad, limited = knee(self.steps)
        wl = self.cfg["workloads"][0]
        at = next((s for s in self.steps if s["rate"] == k), None)
        bad = next((s for s in self.steps if s["rate"] == first_bad), None)
        pods = at["server"]["workloads"][wl]["pods"] if at else None
        return {
            "app": self.name, "env": self.env, "namespace": self.ns, "started": self.started,
            "knee_rps": k, "first_breaking_rps": first_bad, "generator_limited": limited and not first_bad,
            "per_pod_rps": (k / pods) if k and pods else None, "pods": pods, "workload": wl,
            "recommended_rps": int(k * self.cfg["ramp"]["headroom"]) if k else None, "headroom": self.cfg["ramp"]["headroom"],
            "cpu_per_pod_at_knee": (at["server"]["workloads"][wl]["cpu"] / pods) if at and pods else None,
            "broke_on": bad["why"] if bad else None, "bottleneck": bottleneck(at, bad, self.cfg["workloads"]),
            "unstable": self.unstable,
        }

    def report(self):
        os.makedirs(self.results, exist_ok=True)
        day = datetime.datetime.fromtimestamp(self.started or time.time()).strftime("%Y-%m-%d")
        base = os.path.join(self.results, f"{day}-{self.ns}-capacity")
        sm = self.summary()
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump({"summary": sm, "config": {k: v for k, v in self.cfg.items() if k != "slos"},
                       "slos": [s["name"] for s in self.cfg["slos"]], "steps": self.steps, "autoscaling": self.auto}, f, indent=1, default=str)
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write(markdown(self, sm))
        return base + ".md"


def markdown(t, sm):
    wl, L = sm["workload"], []
    L.append(f"# Capacity: {t.ns} ({datetime.datetime.fromtimestamp(t.started or time.time()):%Y-%m-%d %H:%M})\n")
    if sm["knee_rps"] and sm["first_breaking_rps"]:
        L.append(f"**Knee: {sm['knee_rps']} req/s** (held), first SLO break at {sm['first_breaking_rps']} req/s: {sm['broke_on']}.  ")
        L.append(f"Per pod: **{sm['per_pod_rps']:.0f} req/s** ({sm['pods']} × {wl}, {fmt(sm['cpu_per_pod_at_knee'], 3)} cores each at the knee). "
                 f"Recommended ceiling ({sm['headroom']:.0%} of the knee): **{sm['recommended_rps']} req/s**.\n")
    elif sm["generator_limited"]:
        L.append(f"**No knee found: the generator was the limit.** Every SLO held up to {sm['knee_rps']} req/s; above that k6 could not "
                 f"send the rate. Capacity ≥ {sm['knee_rps']} req/s — measure further with a stronger generator or closer to the target.\n")
    elif sm["knee_rps"]:
        L.append(f"**No knee up to the maximum**: every SLO held at {sm['knee_rps']} req/s (capacity.ramp.max). Capacity ≥ that.\n")
    else:
        L.append(f"**Broke at the first step** ({t.steps[0]['rate'] if t.steps else '?'} req/s): {sm['broke_on']}. Lower capacity.ramp.start.\n")
    if sm.get("unstable"):
        L.append(f"**Unstable**: runs at {', '.join(map(str, sm['unstable']))} req/s disagreed (held once, broke once) — the "
                 "result depends on what else ran on the nodes at the time; read the knee as a range, not a number.\n")
    if sm["bottleneck"]:
        L.append("What ran out (last good step → first breaking step):\n")
        L.extend(f"- {b}" for b in sm["bottleneck"])
        L.append("")
    L.append(f"Judged on: {', '.join(s['name'] + ' ≥ ' + str(s['objective']) + '%' for s in t.cfg['slos'])} — each SLI computed over "
             f"the step minus {t.cfg['ramp']['warmup']} of warm-up. Load: the app's request mix, open loop, from `{t.k6}`.\n")
    L.append("## Steps\n")
    names = [s["name"] for s in t.cfg["slos"]]
    L.append("| req/s | verdict | k6 sent/s | k6 p99 ms | " + " | ".join(names) + f" | k6 failed | {wl} pods (min ready) | {wl} CPU | restarts | node CPU | host CPU | busiest neighbour |")
    L.append("|" + "---|" * (11 + len(names)))
    for s in sorted(t.steps, key=lambda x: (x["rate"], x["start"])):
        c, sv = s["client"], s["server"]
        L.append(f"| {s['rate']} | {s['verdict']} | {fmt(c.get('rps'))} | {fmt(c.get('p99'), 0)} | "
                 + " | ".join(fmt(sv['slis'].get(n), 4) for n in names)
                 + f" | {fmt(c.get('failed'), 2, pct=True)} | {sv['workloads'][wl]['pods']} ({fmt(sv['workloads'][wl].get('min_ready'), 0)})"
                 + f" | {fmt(sv['workloads'][wl]['cpu'], 3)} | {sum(w.get('restarts', 0) for w in sv['workloads'].values())}"
                 + f" | {fmt(sv['node_cpu'], 2, pct=True)}"
                 + f" | {fmt(sv.get('host_cpu'), 2)} | {(sv.get('neighbours') or [{}])[0].get('namespace', '—')} {fmt((sv.get('neighbours') or [{}])[0].get('cpu'), 2)} |")
    L.append("")
    if t.auto:
        L.append("## Autoscaling\n")
        a = t.auto
        if "skipped" in a:
            L.append(f"Not measured: {a['skipped']}.\n")
        else:
            L.append(f"Held {a['from_rps']} req/s, then jumped to {a['to_rps']} req/s (HPA `{a['hpa']}`, replicas {a['replicas']}).\n")
            L.append(f"- HPA decided to scale: {secs(a['decided_s'])} after the jump")
            L.append(f"- new pods ready: {secs(a['ready_s'])} after the jump")
            for n, v in a["sli"].items():
                if v["breached_at"] is None:
                    L.append(f"- {n}: held throughout (worst {fmt(v['worst'], 4)})")
                else:
                    L.append(f"- {n}: below objective from {secs(v['breached_at'])}, back {secs(v['back_at'])} (worst {fmt(v['worst'], 4)})")
            L.append("")
    L.append("## Log\n\n```")
    L.extend(t.log)
    L.append("```\n")
    return "\n".join(L)


def secs(v):
    return "never" if v is None else f"{v}s"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kubeconfig")
    p.add_argument("--context")
    p.add_argument("--prometheus", default=ex.PROMETHEUS)
    p.add_argument("--results", metavar="DIR")
    p.add_argument("--k6", default="k6", help='how to run k6, a shell command line (default: %(default)s; e.g. "ssh loadgen k6")')
    p.add_argument("--address", help="where the generator reaches the nodes (default: the first node's InternalIP)")
    p.add_argument("--dry-run", action="store_true", help="check the settings and the lab guard, print the plan")
    p.add_argument("app")
    p.add_argument("env")
    a = p.parse_args()
    ex.KUBE.extend(ex.kube_args(a.kubeconfig, a.context))
    ex.set_prometheus(a.prometheus)
    print(f"context: {ex.context_name()}; Prometheus: {ex.PROMETHEUS}", flush=True)
    try:
        t = Test(a.app, a.env, a.k6, a.address, a.results, a.dry_run)
        code = t.run()
    except (ValueError, RuntimeError) as e:
        sys.exit(f"capacity: {e}")
    if not a.dry_run:
        path = t.report()
        try:
            path = os.path.relpath(path)
        except ValueError:   # Windows: the report on another drive than the working directory
            pass
        print(f"\nreport: {path}")
    sys.exit(code)


if __name__ == "__main__":
    main()
