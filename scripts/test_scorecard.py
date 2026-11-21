"""Tests of scripts/scorecard.py's pure parts on synthetic fixtures (no cluster): SLO selection, runbook anchors,
experiments and their results, budgets, alert episodes, gaps, and the rendered report.

  python3 -I scripts/test_scorecard.py
"""
import datetime as dt
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("scorecard", os.path.join(HERE, "scorecard.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

TZ9 = dt.timezone(dt.timedelta(hours=9))   # any fixed offset: the parsers must not assume UTC
NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=TZ9).timestamp()
DAY = 86400
DEFAULTS = {"severities": {"fastBurn": "page", "mediumBurn": "page", "slowBurn": "ticket", "longTermBurn": "ticket",
                           "absent": "ticket"}}


def slo(name, kind="availability", objective=99.5, **kw):
    return {"name": name, "objective": objective, kind: {"metric": "m"}, **kw}


def instance(app, env, slos, ns=None):
    return {"app": app, "env": env, "namespace": ns or f"{app}-{env}", "slos": slos}


def fire(slo_id, ns="shop-prod", sev="page"):
    return {"alert": "ErrorBudgetBurn", "labels": {"namespace": ns, "slo": slo_id, "severity": sev, "long": "1h"},
            "within": "10m"}


class Declarations(unittest.TestCase):
    def test_runbook_anchor_mirrors_the_chart(self):
        self.assertEqual(sc.runbook_anchor(slo("a")), ("error-budget-burn", False))
        self.assertEqual(sc.runbook_anchor(slo("l", "latency")), ("latency", False))
        self.assertEqual(sc.runbook_anchor(slo("p", "probe")), ("error-budget-burn", False))
        self.assertEqual(sc.runbook_anchor(slo("b", "age", runbook="database-backups")), ("database-backups", True))

    def test_pages_unless_every_page_severity_overridden(self):
        self.assertTrue(sc.pages(slo("a"), DEFAULTS))
        self.assertFalse(sc.pages(slo("b", severities={"fastBurn": "ticket", "mediumBurn": "ticket"}), DEFAULTS))
        self.assertTrue(sc.pages(slo("c", severities={"fastBurn": "ticket"}), DEFAULTS))   # medium burn still pages

    def test_select_env_or_the_only_instances(self):
        xs = [instance("shop", "dev", []), instance("shop", "prod", []), instance("platform", "ops", [], "platform-ops")]
        got = sorted((i["app"], i["env"]) for i in sc.select_instances(xs, "prod"))
        self.assertEqual(got, [("platform", "ops"), ("shop", "prod")])

    def test_slo_rows(self):
        rows = sc.slo_rows([instance("shop", "prod", [slo("a"), slo("only-dev", environments=["dev"]),
                                                      slo("lat", "latency", 99)])], DEFAULTS)
        self.assertEqual([r["id"] for r in rows], ["shop-a", "shop-lat"])
        self.assertEqual(rows[1]["objective"], 99.0)
        self.assertEqual(rows[1]["anchor"], "latency")
        self.assertEqual(rows[0]["namespace"], "shop-prod")

    @unittest.skipUnless(sc.render is not None and os.path.exists(os.path.join(sc.ROOT, "platform/slo/values.yaml"))
                         and os.path.isdir(os.path.join(sc.ROOT, "examples", "storefront")),
                         "needs scripts/render.py, platform/slo (the platform) and the examples")
    def test_repository_slos_load(self):
        """This repository: every example's prod SLOs (no cluster needed)."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)   # render.py / appdesc.py read with open().read()
            rows = sc.slo_rows(sc.select_instances(sc.load_instances(sc.ROOT), "prod"), sc.chart_defaults(sc.ROOT))
        apps = {r["app"] for r in rows}
        self.assertTrue({"sample-app", "storefront"} <= apps, apps)
        self.assertTrue(all(r["env"] == "prod" for r in rows if r["app"] != "platform"))
        self.assertTrue(all(r["kind"] != "?" for r in rows))


class Runbook(unittest.TestCase):
    def test_anchors(self):
        text = "# Runbook\n\n## error-budget-burn\n\n## First five minutes (any alert)\n```\n## not-a-heading\n```\n" \
               "## Canary aborted\n### Canary aborted\n## `latency` step\n<a id=\"custom\"></a>\n"
        a = sc.md_anchors(text)
        for want in ("runbook", "error-budget-burn", "first-five-minutes-any-alert", "canary-aborted",
                     "canary-aborted-1", "latency-step", "custom"):
            self.assertIn(want, a)
        self.assertNotIn("not-a-heading", a)

    def test_runbook_from_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("## latency\n")
        try:
            text, src = sc.runbook_source({}, f.name)
            self.assertIn("latency", sc.md_anchors(text))
        finally:
            os.unlink(f.name)
        self.assertIsNone(sc.runbook_source({}, "/nonexistent/runbook.md")[0])


class Experiments(unittest.TestCase):
    specs = {
        "shop-down": {"namespace": "shop-prod", "expect": {"fire": [fire("shop-a"), fire("shop-a", sev="ticket")]}},
        "shop-down-old": {"namespace": "shop-prod", "skip": "superseded", "expect": {"fire": [fire("shop-lat")]}},
        "shop-quiet": {"namespace": "shop-prod", "expect": {"fire": [], "quiet": [fire("shop-lat")]}},
        "shop-maybe": {"namespace": "shop-dev", "expect": {"may_fire": [fire("shop-lat")], "fire": [fire("shop-b", "shop-dev")]}},
    }

    def test_verifiers_skip_and_fire_only(self):
        v = sc.verifiers(self.specs)
        self.assertEqual(sorted(v), ["shop-a", "shop-b"])
        self.assertEqual([x["experiment"] for x in v["shop-a"]], ["shop-down"])   # two entries, one experiment
        self.assertEqual(v["shop-b"][0]["namespace"], "shop-dev")

    def test_parse_result(self):
        text = "# x: FAIL\n```\n23:58:00 [shop-down] hypothesis: …\n23:59:10 [shop-down] fault created in shop-prod: X\n" \
               "00:10:00 [shop-down] PASS\n```\n"
        r = sc.parse_result("examples/shop/experiments/results/2026-11-20-shop-down-night.md", text, TZ9)
        self.assertEqual((r["experiment"], r["verdict"]), ("shop-down", "PASS"))
        self.assertEqual(r["t"], dt.datetime(2026, 11, 21, 0, 10, tzinfo=TZ9).timestamp())   # past midnight
        died = sc.parse_result("2026-11-20-shop-down.md", "10:00:00 [shop-down] fault created in shop-prod: X\n", TZ9)
        self.assertEqual(died["verdict"], "FAIL")
        inv = sc.parse_result("2026-11-20-shop-down.md", "10:00:00 [shop-down] INVALID\n", TZ9)
        self.assertEqual(inv["verdict"], "INVALID")
        self.assertIsNone(sc.parse_result("2026-11-20-findings.md", "# Findings\nno log lines\n", TZ9))
        self.assertIsNone(sc.parse_result("notes.md", "10:00:00 [x] PASS\n", TZ9))

    def test_every_run_of_a_day_counts(self):
        text = ("# shop-down\n\nh\n\n## Run 2026-11-20T10:00:00+09:00: FAIL\n\n```\n10:00:00 [shop-down] FAIL\n```\n\n"
                "## Run 2026-11-20T14:00:00+09:00: PASS\n\n```\n14:00:00 [shop-down] hypothesis: h\n"
                "14:09:00 [shop-down] PASS\n```\n")
        rs = sc.parse_results("2026-11-20-shop-down.md", text, dt.timezone.utc)
        self.assertEqual([r["verdict"] for r in rs], ["FAIL", "PASS"])
        self.assertEqual(sc.parse_result("2026-11-20-shop-down.md", text, dt.timezone.utc)["t"],
                         dt.datetime(2026, 11, 20, 14, 9, tzinfo=TZ9).timestamp())
        self.assertEqual(sc.newest_pass(rs, []), {"shop-down": rs[1]["t"]})

    def test_specs_from_experiment_directories(self):
        import tempfile
        root = tempfile.mkdtemp()
        for app in ("a", "b"):
            os.makedirs(os.path.join(root, app, "experiments"))
            with open(os.path.join(root, app, "experiments", f"{app}-down.yaml"), "w") as f:
                f.write(f"namespace: {app}-prod\n")
        specs = sc.load_specs([os.path.join(root, "*", "experiments")])
        self.assertEqual(sorted(specs), ["a-down", "b-down"])
        self.assertEqual(specs["b-down"]["namespace"], "b-prod")

    def test_newest_result_and_pass(self):
        files = [{"experiment": "e", "verdict": "PASS", "t": NOW - 3 * DAY, "source": "f"}]
        lab = [{"experiment": "e", "verdict": "FAIL", "t": NOW - DAY, "pass_t": NOW - 2 * DAY, "source": "lab"},
               {"experiment": "g", "verdict": "FAIL", "t": NOW - DAY, "pass_t": None, "source": "lab"}]
        self.assertEqual(sc.newest_results(files, lab)["e"]["source"], "lab")
        self.assertEqual(sc.newest_pass(files, lab), {"e": NOW - 2 * DAY})


class BudgetsAndAlerts(unittest.TestCase):
    def test_budget_left(self):
        self.assertAlmostEqual(sc.budget_left(1.0, 0.995), 1.0)
        self.assertAlmostEqual(sc.budget_left(0.995, 0.995), 0.0)
        self.assertAlmostEqual(sc.budget_left(0.9975, 0.995), 0.5)
        self.assertAlmostEqual(sc.budget_left(0.98, 0.99), -1.0)
        self.assertIsNone(sc.budget_left(None, 0.99))

    def test_episodes_merge_windows_and_gap(self):
        L = {"alertname": "ErrorBudgetBurn", "namespace": "shop-prod", "slo": "shop-a", "severity": "page"}
        series = [({**L, "long": "1h"}, [(0, 600), (900, 1000)]),        # 300s gap: one episode
                  ({**L, "long": "6h"}, [(300, 1200)]),                  # overlaps: same episode
                  ({**L, "long": "1h"}, [(5000, 5100)]),                 # far: a second one
                  ({**L, "severity": "ticket", "long": "1d"}, [(0, 100)]),
                  ({"alertname": "KubePodCrashLooping", "namespace": "shop-prod", "severity": "ticket"}, [(0, 10)])]
        ep = sc.episodes_by_slo(series, 300)
        self.assertEqual(ep[("shop-prod", "shop-a", "page")], [0, 5000])
        self.assertEqual(ep[("shop-prod", "shop-a", "ticket")], [0])
        self.assertEqual(len(ep), 2)
        self.assertEqual(sc.count_since([0, 5000], 1000), 1)


def row(**kw):
    r = {"app": "shop", "env": "prod", "namespace": "shop-prod", "name": "a", "id": "shop-a", "objective": 99.5,
         "pages": True, "anchor": "error-budget-burn", "anchor_explicit": False, "anchor_ok": True, "budget": 0.9,
         "verifiers": [{"experiment": "e", "namespace": "shop-prod",
                        "last": {"verdict": "PASS", "t": NOW - DAY, "source": "lab"}, "last_pass": NOW - DAY}],
         "pages_7d": 0, "pages_28d": 0, "tickets_7d": 0, "tickets_28d": 0}
    r.update(kw)
    return r


class Gaps(unittest.TestCase):
    def kinds(self, r, **kw):
        return [k for k, _ in sc.gaps_of(r, NOW, 7 * DAY, **kw)]

    def test_healthy(self):
        self.assertEqual(self.kinds(row()), [])

    def test_untested_page_and_ticket(self):
        self.assertEqual(self.kinds(row(verifiers=[])), ["untested"])
        self.assertIn("ticket only", sc.gaps_of(row(verifiers=[], pages=False), NOW, 7 * DAY)[0][1])

    def test_unverified_failing_stale(self):
        never = [{"experiment": "e", "last": None, "last_pass": None}]
        self.assertEqual(self.kinds(row(verifiers=never)), ["unverified"])
        failed = [{"experiment": "e", "last": {"verdict": "FAIL", "t": NOW, "source": "f"}, "last_pass": NOW - DAY}]
        self.assertEqual(self.kinds(row(verifiers=failed)), ["failing"])
        old = [{"experiment": "e", "last": {"verdict": "PASS", "t": NOW - 10 * DAY, "source": "f"}, "last_pass": NOW - 10 * DAY}]
        self.assertEqual(self.kinds(row(verifiers=old)), ["stale"])

    def test_runbook_and_budget(self):
        self.assertEqual(self.kinds(row(anchor_ok=False)), ["runbook"])
        self.assertIn("page without a runbook", sc.gaps_of(row(anchor_ok=False), NOW, 7 * DAY)[0][1])
        self.assertEqual(self.kinds(row(budget=0.25)), [])
        self.assertEqual(self.kinds(row(budget=0.2)), ["budget"])
        g = sc.gaps_of(row(budget=-1.05), NOW, 7 * DAY, coverage=9 * 3600)
        self.assertIn("exhausted", g[0][1])
        self.assertIn("over 9h00m of data", g[0][1])
        self.assertEqual(self.kinds(row(budget=None)), ["no-data"])

    def test_pages_per_week(self):
        self.assertEqual(sc.app_gaps([], 9, 28 * DAY), [("pages", sc.app_gaps([], 9, 28 * DAY)[0][1])])
        self.assertEqual(sc.app_gaps([], 8, 28 * DAY), [])        # 2/week: at the target
        self.assertEqual(sc.app_gaps([], 5, 3600), [])            # less than a day: no rate


class EndToEnd(unittest.TestCase):
    def test_build_and_render(self):
        rows = sc.slo_rows([instance("shop", "prod", [slo("a"), slo("lat", "latency", 99)]),
                            instance("platform", "ops", [slo("offsite", "age", 99, runbook="off-site-backup",
                                                             severities={"fastBurn": "ticket", "mediumBurn": "ticket"})],
                                     "platform-ops")], DEFAULTS)
        budgets = ({("shop-a", "prod"): 1.0, ("shop-lat", "prod"): 0.98, ("platform-offsite", "ops"): 1.0},
                   {("shop-a", "prod"): 0.995, ("shop-lat", "prod"): 0.99, ("platform-offsite", "ops"): 0.99})
        specs = {"shop-down": {"namespace": "shop-prod", "expect": {"fire": [fire("shop-a")]}}}
        results = [{"experiment": "shop-down", "verdict": "PASS", "t": NOW - DAY, "source": "2026-11-20-shop-down.md"}]
        episodes = {("shop-prod", "shop-lat", "page"): [NOW - 3600, NOW - 10 * DAY]}
        apps = sc.build(rows, budgets=budgets, anchors={"error-budget-burn", "off-site-backup"}, specs=specs,
                        results=results, lab=[], episodes=episodes, other={"shop-prod": "KubeJobFailed (ticket)"},
                        now=NOW, coverage=28 * DAY, stale_s=7 * DAY)
        self.assertEqual(list(apps), ["shop", "platform"])
        lat = next(r for r in rows if r["name"] == "lat")
        self.assertAlmostEqual(lat["budget"], -1.0)
        self.assertEqual((lat["pages_7d"], lat["pages_28d"]), (1, 2))
        kinds = sorted(k for k, _ in apps["shop"]["gaps"])
        self.assertEqual(kinds, ["budget", "runbook", "untested"])   # lat: no experiment, #latency missing, exhausted
        info = {"date": "2026-11-21", "generated": "x", "env": "prod", "budget_src": "b", "runbook_src": "r",
                "exp_src": "e", "alerts_src": "a", "coverage": 28 * DAY, "warnings": ["w"], "gap_counts": {"budget": 1}}
        md = sc.render_md(rows, apps, info, TZ9)
        self.assertIn("## shop", md)
        self.assertIn("## platform (platform-level SLOs)", md)
        self.assertIn("| lat | 99% | -100% ⛔ | page | #latency (default) ✗ missing | — | 1 / 2 | 0 / 0 |", md)
        self.assertIn("shop-down: PASS 11-20 (2026-11-20-shop-down.md)", md)
        self.assertIn("KubeJobFailed (ticket)", md)
        self.assertIn("> ⚠ w", md)


if __name__ == "__main__":
    unittest.main(verbosity=1)
