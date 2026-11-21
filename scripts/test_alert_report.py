"""Tests of scripts/alert-report.py on synthetic fixtures (no cluster): episodes, routing / inhibition, experiment
windows, classification, and one end-to-end run with a fake Prometheus.

  python3 -I scripts/test_alert_report.py
"""
import datetime as dt
import importlib.util
import os
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("alert_report", os.path.join(HERE, "alert-report.py"))
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)

TZ9 = dt.timezone(dt.timedelta(hours=9))   # any fixed offset: the parsers must not assume UTC
T0 = dt.datetime(2026, 11, 20, 12, 0, tzinfo=TZ9).timestamp()
STEP = 30


def put(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def burn(ns, slo, sev="page", long="1h", **kw):
    return {"alertname": "ErrorBudgetBurn", "namespace": ns, "slo": slo, "severity": sev, "long": long, **kw}


def matrix(*series):
    """[(labels, [(from_min, to_min)])] → a query_range result, a sample every STEP while firing (to exclusive)."""
    out = []
    for labels, spans in series:
        vals = []
        for a, b in spans:
            t = T0 + a * 60
            while t < T0 + b * 60:
                vals.append([t, "1"])
                t += STEP
        out.append({"metric": {"__name__": "ALERTS", "alertstate": "firing", **labels}, "values": vals})
    return out


def episodes(*series, gap=300, end_min=600):
    s = ar.series_runs(matrix(*series), STEP)
    return ar.build_episodes(s, gap, T0, T0 + end_min * 60, STEP), s


EXAMPLE_AM = {
    "route": {"receiver": "null", "routes": [
        {"receiver": "null", "matchers": ['namespace=~".+-(dev|staging)"']},
        {"receiver": "pager", "matchers": ['severity=~"page|ticket"']}]},
    "inhibit_rules": [
        {"source_matchers": ['alertname="ErrorBudgetBurn"', 'severity="page"', 'long="1h"'],
         "target_matchers": ['alertname="ErrorBudgetBurn"', 'severity="page"', 'long="6h"'], "equal": ["namespace", "slo"]},
        {"source_matchers": ['alertname="ErrorBudgetBurn"', 'severity="page"'],
         "target_matchers": ['alertname="ErrorBudgetBurn"', 'severity="ticket"'], "equal": ["namespace", "slo"]}],
    "receivers": [{"name": "null"}, {"name": "pager", "webhook_configs": [{"url": "http://x/"}]}],
}
VERSIONS = [(0.0, EXAMPLE_AM, "test")]


class Episodes(unittest.TestCase):
    def test_flaps_within_gap_merge(self):
        eps, _ = episodes((burn("a-prod", "x"), [(10, 20), (22, 30), (33, 40)]))
        self.assertEqual(len(eps), 1)
        e = eps[0]
        self.assertEqual(len(e["runs"]), 3)
        self.assertEqual((e["start"], e["end"]), (T0 + 600, T0 + 2400))
        self.assertEqual(e["firing_s"], (10 + 8 + 7) * 60)   # gaps are not firing time

    def test_gap_beyond_tolerance_splits(self):
        eps, _ = episodes((burn("a-prod", "x"), [(10, 20), (26, 30)]))
        self.assertEqual(len(eps), 2)

    def test_identities_separate_and_label_variants_union(self):
        absent = {"alertname": "SLOMetricAbsent", "namespace": "a-prod", "slo": "x", "severity": "ticket"}
        eps, _ = episodes((burn("a-prod", "x"), [(0, 5)]), (burn("a-prod", "x", long="6h"), [(0, 5)]),
                          ({**absent, "le": "0.1"}, [(10, 20)]), (absent, [(15, 25)]))
        self.assertEqual(len(eps), 3)
        a = [e for e in eps if e["labels"]["alertname"] == "SLOMetricAbsent"][0]
        self.assertEqual((a["start"], a["end"]), (T0 + 600, T0 + 1500))

    def test_edges_and_always_on(self):
        eps, _ = episodes((burn("a-prod", "x"), [(0, 5), (590, 600)]), ({"alertname": "Watchdog"}, [(0, 600)]))
        self.assertEqual(len(eps), 2)
        self.assertTrue(eps[0]["before_range"])
        self.assertTrue(eps[1]["ongoing"])

    def test_chunks_merge(self):
        m = matrix((burn("a-prod", "x"), [(0, 10)]))
        first = {"metric": m[0]["metric"], "values": m[0]["values"][:10]}
        second = {"metric": m[0]["metric"], "values": m[0]["values"][10:]}
        s = ar.merge_series(ar.series_runs([first], STEP) + ar.series_runs([second], STEP))
        self.assertEqual(s[0][1], [(T0, T0 + 600)])


class Routing(unittest.TestCase):
    def test_matchers(self):
        self.assertTrue(ar.matches({"namespace": "x-dev"}, ['namespace=~".+-(dev|staging)"']))
        self.assertFalse(ar.matches({"namespace": "x-prod"}, ['namespace=~"prod"']))   # anchored
        self.assertTrue(ar.matches({}, ['team=""']))                                     # missing = empty
        self.assertTrue(ar.matches({"a": "b"}, ['a!="c"', 'a!~"c.*"']))
        self.assertFalse(ar.matches({"a": "b"}, ['a!~"b"']))

    def test_routes(self):
        self.assertTrue(ar.notifies(EXAMPLE_AM, burn("a-prod", "x")))
        self.assertFalse(ar.notifies(EXAMPLE_AM, burn("a-dev", "x")))
        self.assertFalse(ar.notifies(EXAMPLE_AM, {"alertname": "TargetDown", "namespace": "a-prod", "severity": "warning"}))
        self.assertFalse(ar.notifies(ar.DEFAULT_AM, burn("a-prod", "x")))

    def test_continue(self):
        cfg = {"route": {"receiver": "null", "routes": [
            {"receiver": "null", "matchers": ['a="1"'], "continue": True},
            {"receiver": "pager", "matchers": ['b="1"']}]},
            "receivers": [{"name": "null"}, {"name": "pager", "webhook_configs": [{"url": "u"}]}]}
        self.assertTrue(ar.notifies(cfg, {"a": "1", "b": "1"}))
        self.assertFalse(ar.notifies(cfg, {"a": "1"}))

    def test_fast_burn_inhibits_medium_burn_and_tickets_of_the_same_slo(self):
        eps, s = episodes((burn("a-prod", "x"), [(10, 40)]),
                          (burn("a-prod", "x", long="6h"), [(20, 35)]),             # inside the 1h page: silent
                          (burn("a-prod", "x", sev="ticket", long="1d"), [(30, 60)]),  # outlives it: notifies
                          (burn("a-prod", "y", sev="ticket", long="1d"), [(20, 30)]))  # another SLO: notifies
        ar.apply_routing(eps, s, VERSIONS)
        got = {(e["labels"]["slo"], e["labels"]["long"]): (e["notified"], e["inhibited_s"]) for e in eps}
        self.assertEqual(got[("x", "1h")], (True, 0))
        self.assertEqual(got[("x", "6h")], (False, 900))
        self.assertEqual(got[("x", "1d")], (True, 600))
        self.assertEqual(got[("y", "1d")], (True, 0))

    def test_dev_never_notifies(self):
        eps, s = episodes((burn("a-dev", "x"), [(10, 40)]))
        ar.apply_routing(eps, s, VERSIONS)
        self.assertFalse(eps[0]["notified"])
        self.assertFalse(eps[0]["routed"])

    def test_config_in_force_at_the_start(self):
        old = {**EXAMPLE_AM, "route": {"receiver": "null", "routes": [{"receiver": "pager", "matchers": ['severity="page"']}]}}
        versions = [(0.0, old, "old"), (T0 + 1200, EXAMPLE_AM, "new")]
        eps, s = episodes((burn("a-dev", "x"), [(10, 15)]), (burn("a-dev", "y"), [(30, 35)]))
        ar.apply_routing(eps, s, versions)
        self.assertEqual([(e["routing"], e["notified"]) for e in eps], [("old", True), ("new", False)])

    def test_config_from_a_file_in_three_forms(self):
        import yaml
        plain = yaml.safe_dump(EXAMPLE_AM)
        values = yaml.safe_dump({"alertmanager": {"enabled": True, "config": EXAMPLE_AM}})
        kustomization = yaml.safe_dump({"resources": ["../base"], "patches": [
            {"target": {"kind": "Application"}, "patch": yaml.safe_dump(
                [{"op": "add", "path": "/spec/source/helm/valuesObject/alertmanager/config", "value": EXAMPLE_AM}])},
            {"patch": "$patch: delete\nkind: Application\nmetadata: {name: x}\n"}]})
        for text in (plain, values, kustomization):
            cfg = ar.am_config_from_text(text)
            self.assertTrue(ar.notifies(cfg, burn("shop-prod", "x")))
            self.assertFalse(ar.notifies(cfg, burn("shop-dev", "x")))
        self.assertIsNone(ar.am_config_from_text("resources: [../base]\n"))

    def test_routing_versions_from_git(self):
        import subprocess
        import yaml
        d = tempfile.mkdtemp()
        f = os.path.join(d, "alertmanager.yaml")
        git = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", d], check=True)
        put(f, yaml.safe_dump(ar.DEFAULT_AM))
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-qm", "1"], check=True, env={**os.environ, "GIT_COMMITTER_DATE": "2026-11-01T00:00:00Z"})
        put(f, yaml.safe_dump(EXAMPLE_AM))
        subprocess.run(git + ["commit", "-qam", "2"], check=True, env={**os.environ, "GIT_COMMITTER_DATE": "2026-11-10T00:00:00Z"})
        v = ar.load_routing(f)
        self.assertEqual(len(v), 2)
        t = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc).timestamp()
        self.assertFalse(ar.notifies(ar.config_at(v, t)[1], burn("shop-prod", "x")))
        self.assertTrue(ar.notifies(ar.config_at(v, t + 10 * 86400)[1], burn("shop-prod", "x")))
        self.assertEqual(ar.load_routing(os.path.join(d, "missing.yaml"))[0][2], "worktree")


class Experiments(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "results"))
        self.specs = {"boom": os.path.join(self.d, "boom.yaml"), "calm": os.path.join(self.d, "calm.yaml")}
        put(os.path.join(self.d, "boom.yaml"), textwrap.dedent("""\
            hypothesis: h
            namespace: a-staging
            fault: [{apiVersion: chaos-mesh.org/v1alpha1, kind: PodChaos, metadata: {name: p}}]
            expect:
              fire: [{alert: ErrorBudgetBurn, labels: {namespace: a-staging, slo: x, severity: page, long: 1h}}]
              quiet: [{alert: ErrorBudgetBurn, labels: {namespace: a-staging, slo: z}}]
            """))
        put(os.path.join(self.d, "calm.yaml"), "hypothesis: h\nnamespace: a-prod\nfault: []\nexpect: {fire: []}\n")
        put(os.path.join(self.d, "results", "2026-11-20-boom-night.md"), textwrap.dedent("""\
            # boom: PASS

            ```
            23:50:00 [boom] hypothesis: h
            23:50:01 [boom] fault created in a-staging: PodChaos/p
            00:05:00 [boom] ✓ fired after 4.0 min: ErrorBudgetBurn {}
            00:10:00 [boom] PASS
            ```
            """))
        put(os.path.join(self.d, "results", "2026-11-20-calm.md"), 
            "```\n11:00:00 [calm] hypothesis: h\n12:00:00 [calm] PASS\n```\n")

    def windows(self, context=None):
        return ar.experiment_windows(ar.results_files([self.d]), TZ9, [], ar.spec_files([self.d]), context=context)

    def test_sections_and_contexts(self):
        put(os.path.join(self.d, "results", "2026-11-22-boom.md"), textwrap.dedent("""\
            # boom

            h

            ## Run 2026-11-22T10:00:00+09:00: PASS

            Context: `lab`.

            ```
            10:00:00 [boom] hypothesis: h
            10:20:00 [boom] PASS
            ```

            ## Run 2026-11-22T15:00:00+09:00: FAIL

            Context: `other`.

            ```
            15:00:00 [boom] hypothesis: h
            15:20:00 [boom] FAIL
            ```
            """))
        boom = [x for x in self.windows() if x["name"] == "boom"]
        self.assertEqual(len(boom), 3)   # one older-format file + two sections, each its own window
        lab = [x for x in self.windows("lab") if x["name"] == "boom"]
        self.assertEqual([(x["verdict"], x["start"]) for x in lab],
                         [("PASS", dt.datetime(2026, 11, 22, 10, 0, tzinfo=TZ9).timestamp())])

    def test_window_crosses_midnight_and_adds_cooldown(self):
        x = [x for x in self.windows() if x["name"] == "boom"][0]
        self.assertEqual(x["start"], dt.datetime(2026, 11, 20, 23, 50, tzinfo=TZ9).timestamp())
        self.assertEqual(x["end"], dt.datetime(2026, 11, 21, 0, 40, tzinfo=TZ9).timestamp())
        self.assertEqual((x["namespace"], x["fault"], x["verdict"]), ("a-staging", True, "PASS"))

    def test_expectations(self):
        xs = self.windows()
        t = dt.datetime(2026, 11, 21, 0, 0, tzinfo=TZ9).timestamp()
        ctx = {"experiments": xs}
        for labels, want in ((burn("a-staging", "x"), "expected"), (burn("a-staging", "z"), "must-stay-quiet"),
                             (burn("a-staging", "x", long="6h"), "unexpected")):
            ep = {"labels": labels, "start": t, "end": t + 60, "runs": [(t, t + 60)], "firing_s": 60, "ongoing": False}
            self.assertEqual(ar.classify(ep, ctx)[:2], ("experiment", want))

    def test_negative_test_and_other_namespace_explain_nothing(self):
        xs = self.windows()
        t = dt.datetime(2026, 11, 20, 11, 30, tzinfo=TZ9).timestamp()
        for labels in (burn("a-prod", "x"), burn("b-staging", "x")):
            ep = {"labels": labels, "start": t, "end": t + 600, "runs": [(t, t + 600)], "firing_s": 600, "ongoing": False}
            self.assertEqual(ar.classify(ep, {"experiments": xs})[0], "unknown")

    def test_metric_runs_and_marks_fill_gaps_only(self):
        t = dt.datetime(2026, 11, 21, 0, 10, tzinfo=TZ9).timestamp()
        xs = ar.experiment_windows(ar.results_files([self.d]), TZ9,
                                   [("boom", "a-staging", t, 1200.0), ("boom", "a-staging", t + 86400, 1200.0)], self.specs,
                                   [(None, "c-prod", t - 100, t + 100), (None, "a-staging", t - 100, t)])
        self.assertEqual(len([x for x in xs if x["name"] == "boom"]), 2)   # the recorded run is not doubled
        late = [x for x in xs if x["file"].startswith("metric")][0]
        self.assertEqual((late["start"], late["end"]), (t + 86400 - 1200, t + 86400 + 1800))
        mark = [x for x in xs if x["file"].startswith("namespace")]
        self.assertEqual([m["namespace"] for m in mark], ["c-prod"])
        ep = {"labels": burn("c-prod", "x"), "start": t, "end": t + 60, "runs": [(t, t + 60)], "firing_s": 60, "ongoing": False}
        self.assertEqual(ar.classify(ep, {"experiments": xs})[:2], ("experiment", "unverified"))


def ep_at(labels, a=10, b=40, runs=None):
    s, e = T0 + a * 60, T0 + b * 60
    runs = runs or [(s, e)]
    return {"labels": labels, "id": ar.identity(labels), "start": s, "end": e, "runs": runs,
            "firing_s": ar.total(runs), "ongoing": False, "before_range": False}


class Classify(unittest.TestCase):
    def test_label_overrides_everything(self):
        ep = ep_at(burn("a-prod", "x"))
        ctx = {"labels": [{"match": {"slo": "x"}, "from": T0, "to": T0 + 3600, "class": "noise", "note": "load test"}],
               "evidence": {ar.evidence_key(ep): ["restart: p ×1"]}}
        self.assertEqual(ar.classify(ep, ctx)[:2], ("noise", "labelled"))

    def test_evidence_makes_an_incident_and_rollouts_alone_a_change(self):
        ep = ep_at(burn("a-prod", "x"))
        self.assertEqual(ar.classify(ep, {"evidence": {ar.evidence_key(ep): ["restart: p ×2", "rollout: rs"]}})[:2],
                         ("incident", "fault"))
        self.assertEqual(ar.classify(ep, {"evidence": {ar.evidence_key(ep): ["rollout: rs"]}})[:2], ("incident", "change"))

    def test_an_alert_is_not_its_own_evidence(self):
        ep = ep_at({"alertname": "TargetDown", "namespace": "a-prod", "severity": "warning"})
        alerts = {("TargetDown", "", "", "", "warning"): {"query": "100 * (count by (job) (up == 0)) > 10"}}
        ctx = {"circular": ar.circular_evidence(alerts), "evidence": {ar.evidence_key(ep): ["target-down: a-prod/web"]}}
        self.assertEqual(ar.classify(ep, ctx)[0], "unknown")

    def test_noise_signatures(self):
        short = ep_at(burn("a-prod", "x"), 10, 12)
        self.assertEqual(ar.classify(short, {})[:2], ("noise", "short"))
        still = dict(short, ongoing=True)   # still firing at the end of the range: not "short"
        self.assertEqual(ar.classify(still, {})[0], "unknown")
        flap = ep_at(burn("a-prod", "x"), 10, 40, runs=[(T0 + 600, T0 + 900), (T0 + 1000, T0 + 1500), (T0 + 1600, T0 + 2400)])
        self.assertEqual(ar.classify(flap, {})[:2], ("noise", "flapping"))
        low = ep_at(burn("a-prod", "x"))
        self.assertEqual(ar.classify(low, {"traffic": {ar.traffic_key(low): {"events": 40, "bad_needed": 2.8}}})[:2],
                         ("noise", "low traffic"))
        busy = {"traffic": {ar.traffic_key(low): {"events": 4000, "bad_needed": 280}}}
        self.assertEqual(ar.classify(low, busy)[0], "unknown")

    def test_young_data_only_for_absent_alerts(self):
        absent = ep_at({"alertname": "SLOMetricAbsent", "namespace": "a-prod", "slo": "x", "severity": "ticket"}, 5, 40)
        ctx = {"tsdb_start": T0, "rules": {ar.rule_key(absent["labels"]): {"for": 180}}}
        self.assertEqual(ar.classify(absent, ctx)[:2], ("noise", "young data"))
        later = ep_at(absent["labels"], 60, 90)
        self.assertEqual(ar.classify(later, ctx)[0], "unknown")
        b = ep_at(burn("a-prod", "x", long="6h"), 30, 60)
        cls, _, why = ar.classify(b, ctx)
        self.assertEqual(cls, "unknown")       # a burn in a half-empty window is not called noise
        self.assertIn("not full", why[0])


class Prometheus(unittest.TestCase):
    def test_denominator(self):
        self.assertEqual(ar.denominator('sum(rate(a{c=~"5.."}[5m])) / sum(rate(a[5m]))'), "sum(rate(a[5m]))")
        self.assertEqual(ar.denominator("(sum(rate(c[1h])) - sum(rate(b{le=\"0.1\"}[1h]))) / sum(rate(c[1h]))"),
                         "sum(rate(c[1h]))")
        self.assertIsNone(ar.denominator("sum(x)"))

    def test_rule_info(self):
        groups = [{"rules": [
            {"type": "alerting", "name": "ErrorBudgetBurn", "duration": 120,
             "labels": burn("a-prod", "x", short="5m"),
             "query": 'r:burnrate5m{slo="x"} > (14 * (1 - 0.995)) and r:burnrate1h{slo="x"} > (14 * (1 - 0.995))'},
            {"type": "recording", "name": "r:burnrate1h", "labels": {"namespace": "a-prod", "slo": "x"},
             "query": "sum(rate(e[1h])) / sum(rate(t[1h]))"}]}]
        alerts, records = ar.rule_info(groups)
        self.assertEqual(alerts[("ErrorBudgetBurn", "a-prod", "x", "1h", "page")]["factor"], 14)
        self.assertAlmostEqual(alerts[("ErrorBudgetBurn", "a-prod", "x", "1h", "page")]["objective"], 0.995)
        self.assertEqual(records[("a-prod", "x", "1h")], "sum(rate(e[1h])) / sum(rate(t[1h]))")


class Periodic(unittest.TestCase):
    def test_periodic(self):
        hourly = [T0 + h * 3600 + 30 * 60 + d for h, d in enumerate((0, 40, -60, 20))]
        self.assertEqual(ar.periodic(hourly, TZ9), 30)
        self.assertIsNone(ar.periodic([T0, T0 + 1300, T0 + 2900], TZ9))


class FakeProm:
    """ALERTS from fixtures; a restart in a-prod; low traffic for slo y; everything else empty."""
    def __init__(self, series):
        self.series = series

    def query(self, q, t=None):
        if "prometheus_tsdb_lowest" in q:
            return [{"metric": {}, "value": [0, str(T0 - 86400)]}]
        if "restarts_total" in q and 'namespace="a-prod"' in q:
            return [{"metric": {"pod": "web-1", "container": "web"}, "value": [t, "2"]}]
        if q == "sum(rate(t[1h]))":
            return [{"metric": {}, "value": [t, "0.01"]}]   # 36 events an hour
        return []

    def query_range(self, q, start, end, step):
        if q.startswith("ALERTS"):
            m = matrix(*self.series)
            for r in m:
                r["values"] = [v for v in r["values"] if start <= v[0] <= end]
            return [r for r in m if r["values"]]
        return []

    def rules(self):
        return [{"rules": [
            {"type": "alerting", "name": "ErrorBudgetBurn", "duration": 120, "labels": burn("b-prod", "y"),
             "query": "r:burnrate5m > (14 * (1 - 0.995)) and r:burnrate1h > (14 * (1 - 0.995))"},
            {"type": "recording", "name": "r:burnrate1h", "labels": {"namespace": "b-prod", "slo": "y"},
             "query": "sum(rate(e[1h])) / sum(rate(t[1h]))"}]}]


class EndToEnd(unittest.TestCase):
    def test_report(self):
        fake = FakeProm([(burn("a-prod", "x"), [(10, 40)]), (burn("b-prod", "y"), [(100, 130)]),
                         (burn("c-prod", "z"), [(200, 201)]), (burn("c-prod", "z", long="6h"), [(300, 330)])])
        old = ar.Prom, ar.namespace_info, ar.live_routing
        ar.Prom = lambda *a, **kw: fake
        ar.namespace_info = lambda kube, now: ([], {"a-prod": "a", "b-prod": "b", "c-prod": "c"})
        ar.live_routing = lambda kube, am: [(0.0, EXAMPLE_AM, "live")]
        out = os.path.join(tempfile.mkdtemp(), "r.md")
        to = dt.datetime.fromtimestamp(T0 + 600 * 60, TZ9).isoformat()
        try:
            ar.main(["--context", "test", "--from", dt.datetime.fromtimestamp(T0, TZ9).isoformat(), "--to", to,
                     "--experiments", tempfile.mkdtemp(), "--labels", "", "--tz", "UTC", "--workers", "4", "-o", out])
        finally:
            ar.Prom, ar.namespace_info, ar.live_routing = old
        md = ar.read(out)
        self.assertIn("4 episodes of 4 alerts", md)
        self.assertIn("incident 1, noise 2, unknown 1", md)
        self.assertIn("restart: web-1 ×2", md)
        self.assertIn("low traffic: 36 events in 1h", md)
        self.assertIn("short: resolved after 1m", md)
        self.assertIn("# Alert value report: test,", md)
        self.assertIn("the running Alertmanager's config", md)
        self.assertIn("| a | 1 |", md)   # pages per application, by the namespace's slo-platform/app label


if __name__ == "__main__":
    unittest.main(verbosity=1)
