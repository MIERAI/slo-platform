"""Unit tests of scripts/experiment.py's judgement and checks (no cluster): python3 -I scripts/test_experiment.py"""
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import experiment as e  # noqa: E402


def alert(slo, ns="shop-prod", severity="page", long="1h", name="ErrorBudgetBurn"):
    return {"alertname": name, "namespace": ns, "slo": slo, "severity": severity, "long": long}


def want(slo, ns="shop-prod"):
    return {"alert": "ErrorBudgetBurn", "labels": {"namespace": ns, "slo": slo, "severity": "page", "long": "1h"}}


class Classify(unittest.TestCase):
    fire = [want("health-probe")]
    quiet = [{"alert": "ErrorBudgetBurn", "labels": {"slo": "base-backup"}}]
    may = [want("rpc-availability"), want("rpc-latency")]

    def c(self, a, marked=()):
        return e.classify(a, self.fire, self.quiet, self.may, "shop-prod", set(marked))

    def test_expected_is_not_judged_here(self):
        self.assertIsNone(self.c(alert("health-probe")))

    def test_may_fire_is_allowed_not_unexpected(self):
        self.assertEqual(self.c(alert("rpc-availability")), "allowed")
        self.assertEqual(self.c(alert("rpc-latency")), "allowed")

    def test_may_fire_matches_its_labels_only(self):
        self.assertEqual(self.c(alert("rpc-latency", long="6h")), "unexpected")   # a 6h page was not allowed
        self.assertEqual(self.c(alert("rpc-latency-severe")), "unexpected")

    def test_quiet_wins_over_may_fire(self):
        self.assertEqual(e.classify(alert("x"), [], [want("x")], [want("x")], "shop-prod", set()), "quiet")

    def test_quiet_without_severity_means_no_page(self):
        self.assertEqual(self.c(alert("base-backup")), "quiet")
        self.assertIsNone(self.c(alert("base-backup", severity="ticket", long="1d")))   # a slow burn of a lab namespace

    def test_quiet_with_severity_holds_tickets_too(self):
        q = [{"alert": "ErrorBudgetBurn", "labels": {"slo": "base-backup", "severity": "ticket"}}]
        self.assertEqual(e.classify(alert("base-backup", severity="ticket", long="1d"), [], q, [], "shop-prod", set()), "quiet")

    def test_other_namespaces(self):
        self.assertEqual(self.c(alert("web", ns="storefront-dev")), "unexpected")
        self.assertIsNone(self.c(alert("web", ns="storefront-dev"), marked={"storefront-dev"}))   # the neighbour's
        self.assertIsNone(self.c(alert("web", severity="ticket")))


class Deadlines(unittest.TestCase):
    def test_refused(self):
        x = {"timeout": "15m", "fault": [
            {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "PodChaos", "metadata": {"name": "a"}, "spec": {"duration": "10m"}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "b"}}],
            "load": [{"kind": "Job", "metadata": {"name": "j"}, "spec": {}}]}
        self.assertEqual(len(e.check_deadlines(x)), 3)

    def test_accepted(self):
        x = {"timeout": "15m", "fault": [
            {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "PodChaos", "metadata": {"name": "a"}, "spec": {"duration": "30m"}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "storefront-faults"}}]}
        self.assertEqual(e.check_deadlines(x), [])

    def test_dur(self):
        self.assertEqual(e.dur("1h30m"), 5400)
        self.assertRaises(ValueError, e.dur, "15")


class Settings(unittest.TestCase):
    def test_prometheus_proxy_path(self):
        self.assertEqual(e.prom_path("monitoring/kps-prometheus:9090"),
                         "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/")
        self.assertEqual(e.prom_path("obs/prom:80"), "/api/v1/namespaces/obs/services/prom:80/proxy/api/v1/")
        self.assertRaises(ValueError, e.prom_path, "kps-prometheus:9090")

    def test_kube_args_only_when_given(self):
        self.assertEqual(e.kube_args(), [])
        self.assertEqual(e.kube_args(None, "lab"), ["--context", "lab"])
        self.assertEqual(e.kube_args("/k", "lab"), ["--kubeconfig", "/k", "--context", "lab"])
        self.assertEqual(e.context_name(["kubectl", "--context", "lab"]), "lab")

    def test_marks_are_the_products(self):
        self.assertEqual((e.MARK, e.COOLDOWN), ("slo-platform/experiment", "slo-platform/experiment-cooldown"))


def fake_run(d, status, log, started):
    """A finished Run without a cluster: what report() needs."""
    path = os.path.join(d, "shop-down.yaml")
    with open(path, "w") as f:
        f.write("hypothesis: every pod fails\nnamespace: shop-dev\n")
    r = e.Run(path, recover=True, context="lab")
    r.started, r.log, r.ok = started, log, status == "pass"
    return r


class Report(unittest.TestCase):
    def test_a_rerun_on_the_same_day_appends(self):
        """A second run of one experiment on one day must not overwrite the first run's findings."""
        d = tempfile.mkdtemp()
        day = datetime.datetime(2026, 11, 20, 10, 0).timestamp()
        first = fake_run(d, "fail", ["10:00:00 [shop-down] hypothesis: x", "10:12:00 [shop-down] ✗ did not fire",
                                     "10:12:01 [shop-down] FAIL"], day)
        f1 = first.report()
        second = fake_run(d, "pass", ["14:00:00 [shop-down] hypothesis: x", "14:09:00 [shop-down] PASS"], day + 4 * 3600)
        f2 = second.report()
        self.assertEqual(f1, f2)
        self.assertEqual(os.path.basename(f1), "2026-11-20-shop-down.md")
        self.assertEqual(os.path.dirname(f1), os.path.join(d, "results"))   # next to the experiment
        text = open(f1).read()
        self.assertIn("✗ did not fire", text)                               # the first run is still there
        self.assertEqual(text.count("## Run 2026-11-20T"), 2)
        self.assertEqual(text.count("# shop-down\n"), 1)
        runs = e.parse_report(f1, text, datetime.timezone.utc)
        self.assertEqual([r["verdict"] for r in runs], ["FAIL", "PASS"])
        self.assertEqual([r["context"] for r in runs], ["lab", "lab"])
        self.assertLess(runs[0]["end"], runs[1]["start"])

    def test_results_dir_given(self):
        d, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        r = fake_run(d, "pass", ["10:00:00 [shop-down] PASS"], datetime.datetime(2026, 11, 20, 10, 0).timestamp())
        self.assertEqual(os.path.dirname(r.report(out)), out)


class ParseReport(unittest.TestCase):
    tz9 = datetime.timezone(datetime.timedelta(hours=9))

    def test_sections_carry_their_offset_and_cross_midnight(self):
        text = ("# x\n\nh\n\n## Run 2026-11-20T23:58:00+02:00: PASS\n\nContext: `lab`.\n\n```\n"
                "23:58:00 [x] hypothesis: h\n00:10:00 [x] PASS\n```\n")
        [r] = e.parse_report("2026-11-20-x.md", text, self.tz9)
        tz2 = datetime.timezone(datetime.timedelta(hours=2))
        self.assertEqual(r["end"], datetime.datetime(2026, 11, 21, 0, 10, tzinfo=tz2).timestamp())
        self.assertEqual((r["experiment"], r["verdict"], r["context"]), ("x", "PASS", "lab"))

    def test_older_format_dated_by_file_name(self):
        text = "# x: FAIL\n```\n10:00:00 [x] fault created in a: P\n```\n"
        [r] = e.parse_report("results/2026-11-20-x-night.md", text, self.tz9)
        self.assertEqual(r["start"], datetime.datetime(2026, 11, 20, 10, 0, tzinfo=self.tz9).timestamp())
        self.assertEqual(r["verdict"], "FAIL")   # died before its verdict
        self.assertIsNone(r["context"])
        self.assertEqual(e.parse_report("notes.md", "10:00:00 [x] PASS\n", self.tz9), [])
        self.assertEqual(e.parse_report("2026-11-20-findings.md", "# Findings\n", self.tz9), [])


if __name__ == "__main__":
    unittest.main()
