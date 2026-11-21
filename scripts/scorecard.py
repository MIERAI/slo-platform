#!/usr/bin/env python3
"""Reliability scorecard: per application, per prod SLO — objective, error budget left (28d), runbook, the experiments
that verify its page, pages and tickets — and the gaps (docs/concepts.md).

  python3 scripts/scorecard.py [--kubeconfig F] [--context C] [--lab-context L] -o reports/scorecard/$(date +%F).md

Read-only: Prometheus queries through the API server's service proxy (the scored cluster: budgets and ALERTS; the
lab, optional: the scheduled regression's slo_experiment_* metrics), this repository's files, and the runbook.

SLOs        what platform/slo renders (scripts/render.py: every app.yaml's `slos` per environment, and the platform's
            own SLO Applications): the --env instance of each, or every instance of an application without one.
Budget      Pyrra's pyrra_availability / pyrra_objective (labels app, env, slo): left = (availability − objective)
            / (1 − objective) over the SLO window (28d), or over the data Prometheus has when it holds less (said so).
Runbook     the anchor the chart puts in pyrra.dev/runbook (slo.runbook, else "latency" / "error-budget-burn"), checked
            against the headings of the runbook (--runbook; default: the chart's runbook URL, else docs/runbook.md).
Verified by experiments (examples/*/experiments/*.yaml) without `skip:` whose expect.fire names the SLO (label slo),
            with the newest result from their results/*.md and from the lab's slo_experiment_last_status /
            _last_run_timestamp_seconds.
Pages       ALERTS{alertstate="firing"} with a slo label in the SLO's namespace, per severity; firing runs less than
            --gap apart are one episode (the 1h and 6h windows of one burn count once). Not whether anyone was
            notified: that is scripts/alert-report.py's job.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import importlib.util
import os
import re
import sys
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required (apt install python3-yaml, or pip install pyyaml)")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)  # render / appdesc / experiment (python3 -I does not add the script's dir)
import experiment  # noqa: E402

try:
    import render  # noqa: E402   (the platform's renderer: render.apps(root))
except ImportError:  # pragma: no cover
    render = None

_spec = importlib.util.spec_from_file_location("alert_report", os.path.join(HERE, "alert-report.py"))
ar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ar)   # Prom, dur, fmt_dur, union, series_runs, merge_series, load_tz: one implementation

DAY = 86400
WINDOW = 28 * DAY
POLICY = 0.25                  # below 25% left, promotions to prod need a stated reason (docs/concepts.md, 5)
PAGES_PER_WEEK = 2             # fewer than ~2 pages per application per week
KIND_ORDER = ("availability", "latency", "probe", "age", "bool", "ratio")
EXPERIMENTS = os.path.join(ROOT, "examples", "*", "experiments")


# ---------------------------------------------------------------- SLO declarations (pure)

def chart_defaults(root):
    f = os.path.join(root, "platform/slo/values.yaml")
    return (yaml.safe_load(ar.read(f)) or {}) if os.path.exists(f) else {}


def sli_kind(slo):
    for k in KIND_ORDER:
        if k in slo:
            return k
    return "?"


def runbook_anchor(slo):
    """→ (anchor, explicit): what platform/slo/templates/slos.yaml puts after '#' in pyrra.dev/runbook."""
    if slo.get("runbook"):
        return slo["runbook"], True
    return ("latency" if sli_kind(slo) == "latency" else "error-budget-burn"), False


def severities(slo, defaults):
    return {**(defaults.get("severities") or {}), **(slo.get("severities") or {})}


def pages(slo, defaults):
    return any(v == "page" for v in severities(slo, defaults).values())


def select_instances(instances, env):
    """[{app, env, namespace, slos}] → the `env` instance of each app, or all of an app that has no such env."""
    by = {}
    for i in instances:
        by.setdefault(i["app"], []).append(i)
    out = []
    for app, xs in by.items():
        hit = [x for x in xs if x["env"] == env]
        out += hit or xs
    return out


def slo_rows(instances, defaults):
    rows = []
    for i in instances:
        for s in i["slos"]:
            if s.get("environments") and i["env"] not in s["environments"]:
                continue
            anchor, explicit = runbook_anchor(s)
            rows.append({"app": i["app"], "env": i["env"], "namespace": i["namespace"], "name": s["name"],
                         "id": f"{i['app']}-{s['name']}", "objective": float(s["objective"]), "kind": sli_kind(s),
                         "anchor": anchor, "anchor_explicit": explicit, "pages": pages(s, defaults),
                         "severities": severities(s, defaults)})
    return rows


def load_instances(root):
    """Every rendering of platform/slo the Applications make (render.apps), with its values."""
    if render is None:
        sys.exit("scripts/render.py is missing: it says which SLOs the platform renders")
    out = []
    for a in render.apps(root):
        if a["path"] != "platform/slo":
            continue
        v = dict(a.get("values_object") or {})
        for f in a["values"]:
            v.update(yaml.safe_load(ar.read(os.path.join(root, a["path"], f))) or {})
        out.append({"app": v.get("app", ""), "env": v.get("env", ""), "namespace": a["namespace"], "slos": v.get("slos") or []})
    return out


# ---------------------------------------------------------------- runbook (pure)

def slug(heading):
    """GitHub's heading anchor: lower case, punctuation dropped (not '-' / '_'), spaces → '-'."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def md_anchors(text):
    out, seen, fence = set(), {}, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            base = slug(re.sub(r"`|\*\*?|\[([^\]]*)\]\([^)]*\)", lambda x: x.group(1) or "", m.group(1)))
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.add(base if n == 0 else f"{base}-{n}")
        out.update(re.findall(r'<a\s+(?:id|name)="([^"]+)"', line))
    return out


def runbook_source(defaults, given):
    """The runbook text: --runbook (a file or URL), else the chart's runbook URL (a GitHub file: read raw), else
    docs/runbook.md of this repository."""
    if given:
        cands = [given]
    else:
        cands = []
        url = defaults.get("runbook", "") or ""
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/blob/([^#]+)", url)
        if m:
            cands.append(f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}")
        elif re.match(r"https?://", url):
            cands.append(url.split("#")[0])
        cands.append(os.path.join(ROOT, "docs/runbook.md"))
    for c in cands:
        if re.match(r"https?://", c):
            try:
                with urllib.request.urlopen(c, timeout=20) as r:
                    return r.read().decode("utf-8"), c
            except OSError:
                continue
        elif os.path.exists(c):
            return ar.read(c), os.path.relpath(c, ROOT) if c.startswith(ROOT) else c
    return None, " / ".join(cands)


# ---------------------------------------------------------------- experiments (pure)

def load_specs(dirs):
    """{name: experiment} of every experiment directory (globs allowed)."""
    out = {}
    for name, f in ar.spec_files(dirs).items():
        out[name] = yaml.safe_load(ar.read(f)) or {}
    return out


def verifiers(specs):
    """{slo id: [(experiment, namespace, severities expected)]} from non-skipped expect.fire entries."""
    out = {}
    for name, x in sorted(specs.items()):
        if x.get("skip"):
            continue
        for w in (x.get("expect") or {}).get("fire") or []:
            L = w.get("labels") or {}
            if L.get("slo"):
                ent = out.setdefault(L["slo"], {})
                ent.setdefault(name, {"experiment": name, "namespace": L.get("namespace") or x.get("namespace"),
                                      "alerts": set()})["alerts"].add(
                    w.get("alert", "") + (f" {L['severity']}" if L.get("severity") else ""))
    return {k: list(v.values()) for k, v in out.items()}


def parse_results(path, text, tz):
    """<results>/<date>-<name>[-suffix].md → [{experiment, verdict, t (end), source}], one per run section (a second
    run on one day appends a section); [] for a findings note without log lines."""
    return [{"experiment": r["experiment"], "verdict": r["verdict"], "t": r["end"], "source": os.path.basename(path)}
            for r in experiment.parse_report(path, text, tz)]


def parse_result(path, text, tz):
    """The newest run of a results file, or None."""
    rs = parse_results(path, text, tz)
    return max(rs, key=lambda r: r["t"]) if rs else None


def newest_results(results, lab):
    """{experiment: newest result} from results files and the lab's metrics ({experiment, verdict, t, source})."""
    out = {}
    for r in list(results) + list(lab):
        cur = out.get(r["experiment"])
        if cur is None or r["t"] > cur["t"]:
            out[r["experiment"]] = r
    return out


def newest_pass(results, lab):
    out = {}
    for r in list(results) + list(lab):
        t = r.get("pass_t") if "pass_t" in r else (r["t"] if r["verdict"] == "PASS" else None)
        if t and t > out.get(r["experiment"], 0):
            out[r["experiment"]] = t
    return out


# ---------------------------------------------------------------- budgets and alerts (pure)

def budget_left(availability, objective):
    """Share of the error budget left (1 = untouched, 0 = spent, < 0 = overspent)."""
    if availability is None or objective is None or objective >= 1:
        return None
    return (availability - objective) / (1 - objective)


def episodes_by_slo(series, gap):
    """[(labels, runs)] → {(namespace, slo, severity): [episode start]} — every window of a burn merged, runs closer than
    `gap` one episode."""
    by = {}
    for labels, runs in series:
        if not labels.get("slo"):
            continue
        by.setdefault((labels.get("namespace", ""), labels["slo"], labels.get("severity", "")), []).extend(runs)
    out = {}
    for k, runs in by.items():
        starts, end = [], None
        for s, e in ar.union(runs):
            if end is not None and s - end <= gap:
                end = max(end, e)
            else:
                starts.append(s)
                end = e
        out[k] = starts
    return out


def count_since(starts, t):
    return sum(s >= t for s in starts)


# ---------------------------------------------------------------- gaps (pure)

def gaps_of(row, now, stale_s, coverage=WINDOW):
    """[(kind, text)] for one SLO row (already joined with budget, experiments, results)."""
    g = []
    over = f", over {fmt_age(coverage)} of data" if coverage < WINDOW - 3600 else ""
    ver = row["verifiers"]
    if not ver:
        g.append(("untested", f"{row['name']}: no experiment expects it to fire"
                  + (" — its page is a hope" if row["pages"] else " (ticket only)")))
    else:
        passed = [v["last_pass"] for v in ver if v.get("last_pass")]
        never = [v["experiment"] for v in ver if not v.get("last")]
        failing = [v["experiment"] for v in ver if v.get("last") and v["last"]["verdict"] != "PASS"]
        if not passed:
            g.append(("unverified", f"{row['name']}: no verifying experiment has passed"
                      + (f" (last run failed/invalid: {', '.join(failing)})" if failing else "")
                      + (f" (never run: {', '.join(never)})" if never else "")))
        else:
            if now - max(passed) > stale_s:
                g.append(("stale", f"{row['name']}: last passing experiment {fmt_age(now - max(passed))} ago "
                          f"(> {ar.fmt_dur(stale_s)})"))
            if failing:
                g.append(("failing", f"{row['name']}: last run failed/invalid: {', '.join(failing)}"))
    if not row["anchor_ok"]:
        g.append(("runbook", f"{row['name']}: runbook anchor #{row['anchor']} not in the runbook"
                  + (" — a page without a runbook" if row["pages"] else "")))
    b = row["budget"]
    if b is None:
        g.append(("no-data", f"{row['name']}: no budget data (pyrra_availability absent: no events or SLI absent)"))
    elif b <= 0:
        g.append(("budget", f"{row['name']}: budget exhausted ({b:.0%}{over}) — feature promotions to prod stop"))
    elif b < POLICY:
        g.append(("budget", f"{row['name']}: budget {b:.0%} < 25%{over} — promotions need a stated reason"))
    return g


def app_gaps(rows, pages_28d, coverage_s):
    weeks = coverage_s / (7 * DAY)
    if coverage_s >= DAY and pages_28d / max(weeks, 1) > PAGES_PER_WEEK:
        return [("pages", f"{pages_28d} pages in {fmt_age(coverage_s)} ({pages_28d / max(weeks, 1):.1f}/week) > "
                 f"~{PAGES_PER_WEEK}/week")]
    return []


# ---------------------------------------------------------------- rendering

def fmt_age(s):
    d = s / DAY
    return f"{d:.0f}d" if d >= 2 else ar.fmt_dur(s)


def fmt_budget(b):
    if b is None:
        return "no data"
    mark = " ⛔" if b <= 0 else (" ⚠" if b < POLICY else "")
    return f"{b:.0%}{mark}"


def fmt_obj(o):
    return f"{o:g}%"


def fmt_exp(v, tz):
    last = v.get("last")
    if not last:
        return f"{v['experiment']}: never run"
    d = dt.datetime.fromtimestamp(last["t"], tz).strftime("%m-%d")
    s = f"{v['experiment']}: {last['verdict']} {d} ({last['source']})"
    if last["verdict"] != "PASS" and v.get("last_pass"):
        s += f", passed {dt.datetime.fromtimestamp(v['last_pass'], tz):%m-%d}"
    return s


def app_title(app):
    return "platform (platform-level SLOs)" if app == "platform" else app


def render_md(rows, apps, info, tz):
    L = [f"# Reliability scorecard: {info['date']}", "",
         f"Generated by `scripts/scorecard.py` ({info['generated']}). Principles: `docs/concepts.md` (2: alert "
         "layers, 3: alerts are tested, 5: error budget policy).", "",
         f"- SLOs: platform/slo as rendered for `{info['env']}` (platform-level: their only instance).",
         f"- Budget: {info['budget_src']}.",
         f"- Runbook: {info['runbook_src']}.",
         f"- Experiments: {info['exp_src']}.",
         f"- Pages / tickets: {info['alerts_src']}.", ""]
    for w in info.get("warnings") or []:
        L.append(f"> ⚠ {w}")
    if info.get("warnings"):
        L.append("")
    n = len(rows)
    low = [r for r in rows if r["budget"] is not None and 0 < r["budget"] < POLICY]
    out = [r for r in rows if r["budget"] is not None and r["budget"] <= 0]
    tested = [r for r in rows if r["verifiers"]]
    verified = [r for r in rows if any(v.get("last_pass") for v in r["verifiers"])]
    pg = [r for r in rows if r["pages"]]
    pg_tested = [r for r in pg if any(v.get("last_pass") for v in r["verifiers"])]
    rb = [r for r in rows if r["anchor_ok"]]
    L += ["## Headline", "",
          f"- {n} SLOs in {len(apps)} applications; {len(pg)} page, {n - len(pg)} ticket only.",
          f"- Error budget: {n - len(low) - len(out) - sum(r['budget'] is None for r in rows)} above 25%, "
          f"{len(low)} below 25%, {len(out)} exhausted, {sum(r['budget'] is None for r in rows)} no data"
          + (f" (over {fmt_age(info['coverage'])} of data, not 28d)" if info["coverage"] < WINDOW - 3600 else "") + ".",
          f"- Experiments: {len(tested)}/{n} SLOs have a non-skipped experiment that expects them to fire; "
          f"{len(verified)}/{n} have one whose last pass is on record; pages verified: {len(pg_tested)}/{len(pg)}.",
          f"- Runbook anchors found: {len(rb)}/{n}.",
          f"- Pages: {sum(r['pages_7d'] for r in rows)} (7d), {sum(r['pages_28d'] for r in rows)} (28d); "
          f"tickets: {sum(r['tickets_7d'] for r in rows)} (7d), {sum(r['tickets_28d'] for r in rows)} (28d)"
          + (f" — ALERTS only covers {fmt_age(info['coverage'])}" if info["coverage"] < WINDOW - 3600 else "") + ".",
          f"- Gaps: {sum(len(a['gaps']) for a in apps.values())} "
          f"({', '.join(f'{k} {v}' for k, v in sorted(info['gap_counts'].items()))}).", ""]
    L += ["| Application | SLOs | Budget < 25% | Exhausted | Verified by an experiment | Pages 7d / 28d | Gaps |",
          "|---|---|---|---|---|---|---|"]
    for app, a in apps.items():
        rs = a["rows"]
        L.append(f"| {app_title(app)} | {len(rs)} | {sum(r['budget'] is not None and 0 < r['budget'] < POLICY for r in rs)} | "
                 f"{sum(r['budget'] is not None and r['budget'] <= 0 for r in rs)} | "
                 f"{sum(any(v.get('last_pass') for v in r['verifiers']) for r in rs)}/{len(rs)} | "
                 f"{sum(r['pages_7d'] for r in rs)} / {sum(r['pages_28d'] for r in rs)} | {len(a['gaps'])} |")
    L.append("")
    for app, a in apps.items():
        rs = a["rows"]
        L += [f"## {app_title(app)}", "",
              f"Namespace `{rs[0]['namespace']}` ({rs[0]['env']}). "
              f"Pages {sum(r['pages_7d'] for r in rs)} (7d) / {sum(r['pages_28d'] for r in rs)} (28d), "
              f"tickets {sum(r['tickets_7d'] for r in rs)} / {sum(r['tickets_28d'] for r in rs)}"
              + (f"; other alerts in the namespace (no slo label): {a['other']}" if a.get("other") else "") + ".", "",
              "| SLO | Objective | Budget left | Alerts | Runbook | Verified by (last result) | Pages 7d / 28d | Tickets 7d / 28d |",
              "|---|---|---|---|---|---|---|---|"]
        for r in rs:
            rb = f"#{r['anchor']}" + ("" if r["anchor_explicit"] else " (default)") + (" ✓" if r["anchor_ok"] else " ✗ missing")
            ex = "<br>".join(fmt_exp(v, tz) for v in r["verifiers"]) or "—"
            L.append(f"| {r['name']} | {fmt_obj(r['objective'])} | {fmt_budget(r['budget'])} | "
                     f"{'page' if r['pages'] else 'ticket'} | {rb} | {ex} | {r['pages_7d']} / {r['pages_28d']} | "
                     f"{r['tickets_7d']} / {r['tickets_28d']} |")
        L += ["", "Gaps:", ""]
        L += [f"- **{k}** {t}" for k, t in a["gaps"]] or ["- none"]
        L.append("")
    L += ["## How to read this", "",
          "- Budget left = (availability − objective) / (1 − objective), Pyrra's own figures; ⚠ < 25% (promotions "
          "need a stated reason), ⛔ exhausted (feature promotions stop). While Prometheus holds less than 28 days, the "
          "window is what it holds: a young window makes a short blip a large share (a young SLO is oversensitive).",
          "- Verified by: experiments without `skip:` whose `expect.fire` names the SLO, in whichever namespace they run "
          "(the rules are the same per environment). Last result: the newer of the results file and the lab's "
          "`slo_experiment_last_status`; a FAIL may be another expectation of the same experiment.",
          "- Pages / tickets: firing episodes of the SLO's alerts (ErrorBudgetBurn, SLOMetricAbsent) by severity, not "
          "whether anyone was notified (`scripts/alert-report.py`); pages of experiments run on this cluster count too.",
          "- Gap kinds: untested (no experiment fires it), unverified (none passed), failing (the last run did not "
          "pass), stale (last pass older than `--stale`), runbook (anchor missing), budget, no-data, pages (> ~2/week).", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- cluster reads

def fetch_budgets(prom):
    av = {(r["metric"].get("slo"), r["metric"].get("env")): float(r["value"][1]) for r in prom.query("pyrra_availability")}
    ob = {(r["metric"].get("slo"), r["metric"].get("env")): float(r["value"][1]) for r in prom.query("pyrra_objective")}
    return av, ob


def fetch_alert_series(prom, start, end, step):
    q = 'ALERTS{alertstate="firing", severity=~"page|ticket"}'
    parts, t = [], start
    while t < end:
        e = min(end, t + step * 10000)
        parts += ar.series_runs(prom.query_range(q, t, e, step), step)
        t = e + step
    return ar.merge_series(parts)


def fetch_lab(prom):
    """the lab's scheduled regression: [{experiment, verdict, t, pass_t, source}]."""
    status = {r["metric"]["experiment"]: r["metric"].get("status", "?").upper()
              for r in prom.query("slo_experiment_last_status == 1")}
    ran = {r["metric"]["experiment"]: float(r["value"][1]) for r in prom.query("slo_experiment_last_run_timestamp_seconds")}
    ok = {r["metric"]["experiment"]: float(r["value"][1]) for r in prom.query("slo_experiment_last_success_timestamp_seconds")}
    out = []
    for name, t in ran.items():
        out.append({"experiment": name, "verdict": status.get(name, "?"), "t": t, "pass_t": ok.get(name), "source": "lab"})
    return out


# ---------------------------------------------------------------- main

def build(rows, *, budgets, anchors, specs, results, lab, episodes, other, now, coverage, stale_s):
    av, ob = budgets
    ver = verifiers(specs)
    last = newest_results(results, lab)
    passed = newest_pass(results, lab)
    for r in rows:
        k = (r["id"], r["env"])
        r["budget"] = budget_left(av.get(k), ob.get(k, r["objective"] / 100)) if k in av else None
        r["anchor_ok"] = anchors is None or r["anchor"] in anchors
        r["verifiers"] = [{**v, "last": last.get(v["experiment"]), "last_pass": passed.get(v["experiment"])}
                          for v in ver.get(r["id"], [])]
        for sev, key in (("page", "pages"), ("ticket", "tickets")):
            st = episodes.get((r["namespace"], r["id"], sev), [])
            r[f"{key}_7d"], r[f"{key}_28d"] = count_since(st, now - 7 * DAY), count_since(st, now - WINDOW)
    apps = {}
    for r in rows:
        apps.setdefault(r["app"], {"rows": [], "gaps": []})["rows"].append(r)
    apps = dict(sorted(apps.items(), key=lambda x: (x[0] == "platform", x[0])))
    for app, a in apps.items():
        for r in a["rows"]:
            a["gaps"] += gaps_of(r, now, stale_s, coverage)
        a["gaps"] += app_gaps(a["rows"], sum(r["pages_28d"] for r in a["rows"]), coverage)
        a["other"] = other.get(a["rows"][0]["namespace"], "")
    return apps


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kubeconfig", help="the cluster whose prod SLOs are scored (default: kubectl's)")
    p.add_argument("--context", help="default: the kubeconfig's current context")
    p.add_argument("--prometheus", default=experiment.PROMETHEUS, help="<namespace>/<service>:<port> (default: %(default)s)")
    p.add_argument("--lab-kubeconfig", help="the lab cluster's kubeconfig (scheduled regression's slo_experiment_* metrics)")
    p.add_argument("--lab-context", help="the lab cluster's context; neither given: the lab is not read; unreachable: skipped")
    p.add_argument("--env", default="prod")
    p.add_argument("--experiments", nargs="*", default=[EXPERIMENTS], help="experiment directories, globs allowed "
                   "(default: examples/*/experiments; results: their results/*.md)")
    p.add_argument("--runbook", help="the runbook: a file or URL (default: the SLO chart's runbook URL, else docs/runbook.md)")
    p.add_argument("--stale", default="7d", help="a last pass older than this is a gap")
    p.add_argument("--gap", default="5m", help="firing runs closer than this are one episode")
    p.add_argument("--step", type=float, default=60)
    p.add_argument("--tz", help="time zone of the report (default: the system's)")
    p.add_argument("-o", "--output", help="Markdown file (default: stdout)")
    a = p.parse_args(argv)
    tz = ar.load_tz(a.tz)
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    defaults = chart_defaults(ROOT)
    rows = slo_rows(select_instances(load_instances(ROOT), a.env), defaults)
    warnings = []

    prom = ar.Prom(a.kubeconfig, a.context, a.prometheus)
    context = experiment.context_name(["kubectl", *experiment.kube_args(a.kubeconfig, a.context)])
    try:
        lowest = prom.query("min(prometheus_tsdb_lowest_timestamp_seconds)")
    except RuntimeError as e:
        sys.exit(f"Prometheus unreachable ({e})")
    tsdb = float(lowest[0]["value"][1]) if lowest else now - WINDOW
    start = max(now - WINDOW, tsdb)
    start = (start // a.step + 1) * a.step
    coverage = now - start
    if coverage < WINDOW - 3600:
        warnings.append(f"Prometheus holds data only from {dt.datetime.fromtimestamp(tsdb, tz):%Y-%m-%d %H:%M} "
                        f"({fmt_age(now - tsdb)}): budgets and alert counts cover that, not 28 days.")
    lab_given = bool(a.lab_kubeconfig or a.lab_context)
    lab, lab_src = [], "lab not read (no --lab-context / --lab-kubeconfig)"
    with cf.ThreadPoolExecutor(4) as pool:
        f_b = pool.submit(fetch_budgets, prom)
        f_s = pool.submit(fetch_alert_series, prom, start, now, a.step)
        f_l = pool.submit(fetch_lab, ar.Prom(a.lab_kubeconfig, a.lab_context, a.prometheus)) if lab_given else None
        budgets, series = f_b.result(), f_s.result()
        if f_l:
            try:
                lab = f_l.result()
                lab_src = f"the lab's slo_experiment_* metrics ({len(lab)} experiments)"
            except RuntimeError as e:
                lab_src = "lab unreachable"
                warnings.append(f"lab not read ({str(e)[:120]}): results files only.")
    episodes = episodes_by_slo(series, ar.dur(a.gap))
    other = {}
    for labels, runs in series:
        if not labels.get("slo") and labels.get("alertname") not in ar.ALWAYS_ON:
            k = labels.get("namespace", "")
            other.setdefault(k, set()).add(f"{labels.get('alertname')} ({labels.get('severity')})")
    other = {k: ", ".join(sorted(v)) for k, v in other.items()}

    text, src = runbook_source(defaults, a.runbook)
    anchors = md_anchors(text) if text else None
    if anchors is None:
        warnings.append(f"runbook not readable ({src}): anchors not checked.")
    specs = load_specs(a.experiments)
    files = ar.results_files(a.experiments)
    results = [x for f in files for x in parse_results(f, ar.read(f), tz)]

    apps = build(rows, budgets=budgets, anchors=anchors, specs=specs, results=results, lab=lab, episodes=episodes,
                 other=other, now=now, coverage=coverage, stale_s=ar.dur(a.stale))
    counts = {}
    for x in apps.values():
        for k, _ in x["gaps"]:
            counts[k] = counts.get(k, 0) + 1
    info = {"date": dt.datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d"),
            "generated": dt.datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M %Z"), "env": a.env,
            "budget_src": f"pyrra_availability / pyrra_objective on {context}",
            "runbook_src": src if anchors is not None else f"not readable ({src})",
            "exp_src": f"{sum(not x.get('skip') for x in specs.values())} non-skipped of {len(specs)} experiments; "
                       f"{len(results)} results; {lab_src}",
            "alerts_src": f"ALERTS (step {a.step:g}s, episodes merged when < {a.gap} apart) from "
                          f"{dt.datetime.fromtimestamp(start, tz):%Y-%m-%d %H:%M}",
            "coverage": coverage, "warnings": warnings, "gap_counts": counts}
    md = render_md(rows, apps, info, tz)
    if a.output:
        os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
        with open(a.output, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
        print(f"wrote {a.output}: {len(rows)} SLOs, {sum(counts.values())} gaps", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
