"""Unit tests of scripts/generate-experiments.py (no cluster): python3 -I scripts/test_generate_experiments.py"""
import contextlib
import glob
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import experiment  # noqa: E402
import yaml  # noqa: E402

spec = importlib.util.spec_from_file_location("gen", os.path.join(HERE, "generate-experiments.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

POD = [{"app": "web", "tier": "front"}, {"app": "api"}]


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = fn(*a, **kw)
    return rc, out.getvalue()


class Pure(unittest.TestCase):
    def test_parse_matchers(self):
        self.assertEqual(g.parse_matchers('app="web", route=~"GET /{$}", code!="404"'),
                         [("app", "=", "web"), ("route", "=~", "GET /{$}"), ("code", "!=", "404")])
        self.assertEqual(g.parse_matchers(""), [])
        with self.assertRaises(ValueError):
            g.parse_matchers("app=web")

    def test_selector_from_matchers(self):
        self.assertEqual(g.selector_from_matchers('app="web", route="GET /"', POD), ({"app": "web"}, None))
        self.assertEqual(g.selector_from_matchers('app="web", tier="front"', POD)[0], {"app": "web", "tier": "front"})
        self.assertIsNone(g.selector_from_matchers('app=~"web|api"', POD)[0])        # not an equality: not derived
        self.assertIsNone(g.selector_from_matchers('job="cnpg"', POD)[0])
        self.assertIsNone(g.selector_from_matchers(None, POD)[0])
        self.assertIsNone(g.selector_from_matchers('app="web", tier="back"', [{"app": "web"}, {"tier": "back"}])[0])

    def test_probe_bound(self):
        self.assertEqual(g.probe_bound(99.5)[0], (4.2, 11))        # 14 × 0.5% of 60 min, + for 2m + margin 4m
        self.assertEqual(g.probe_bound(99.9)[0], (0.8, 8))         # never below 8 min
        self.assertEqual(g.probe_bound(99)[0], (8.4, 15))
        self.assertIsNone(g.probe_bound(95)[0])                    # 42 min of outage: too long
        self.assertIn("cannot fire", g.probe_bound(90)[1])        # 14 × 10% > the hour
        self.assertIsNone(g.probe_bound(100)[0])

    def test_environments(self):
        app = {"environments": [{"name": "dev"}, {"name": "staging"}, {"name": "prod"}]}
        self.assertEqual(g.environments(app, {}), ["dev"])
        self.assertEqual(g.environments(app, {"environments": ["staging", "prod"]}), ["staging"])
        self.assertEqual(g.environments(app, {"environments": ["prod"]}), [])
        self.assertEqual(g.environments(app, {"environments": ["prod"]}, ["prod", "dev"]), ["prod"])

    def test_flow_quotes_what_needs_it(self):
        self.assertEqual(g.flow({"app": "web"}), "{ app: web }")
        self.assertEqual(yaml.safe_load(g.flow({"on": "true", "app.kubernetes.io/name": "x y"})),
                         {"on": "true", "app.kubernetes.io/name": "x y"})


class ExperimentFormat(unittest.TestCase):
    def test_generated_field_accepted_unknown_refused(self):
        x = {"hypothesis": "h", "namespace": "a-dev", "fault": [], "expect": {"fire": []},
             "generated": {"from": "s", "template": "probe", "version": 1}}
        self.assertEqual(experiment.check_format(x), [])
        self.assertTrue(experiment.check_format({**x, "generated": {"from": "s"}}))
        self.assertTrue(experiment.check_format({**x, "expectations": {}}))
        self.assertTrue(experiment.check_format({**x, "timeout": "soon"}))

    def test_every_example_passes(self):
        for f in glob.glob(os.path.join(ROOT, "examples", "*", "experiments", "*.yaml")):
            x = experiment.load_yaml(f)
            self.assertEqual(experiment.check_format(x) + experiment.check_deadlines(x), [], f)

    def test_paths_and_results(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "generated"))
            for f in ("a.yaml", "generated/b.yaml"):
                open(os.path.join(d, f), "w").close()
            self.assertEqual([os.path.relpath(f, d) for f in experiment.experiment_paths(d)], ["a.yaml", "generated/b.yaml"])
            self.assertEqual(experiment.default_results_dir(os.path.join(d, "generated", "b.yaml")), os.path.join(d, "results"))
            self.assertEqual(experiment.default_results_dir(os.path.join(d, "a.yaml")), os.path.join(d, "results"))


class Generate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.apps = os.path.join(self.tmp, "apps")
        # the examples as written by hand (their committed generated/ files left out)
        shutil.copytree(os.path.join(ROOT, "examples"), self.apps,
                        ignore=shutil.ignore_patterns("results", "README.md", "generated"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def gen(self, *args):
        return quiet(g.main, ["--apps", self.apps, *args])

    def generated(self):
        return sorted(os.path.relpath(f, self.apps) for f in glob.glob(os.path.join(self.apps, "*", "experiments", "generated", "*.yaml")))

    def test_both_examples(self):
        rc, out = self.gen()
        self.assertEqual(rc, 0, out)
        files = self.generated()
        self.assertEqual(len(files), 11, out)   # 3 + 8 SLOs, each in dev
        self.assertIn("sample-app/experiments/generated/sample-app-hello-probe-dev.yaml", files)
        for f in files:
            path = os.path.join(self.apps, f)
            with open(path) as fh:
                text = fh.read()
            self.assertTrue(text.startswith("# generated by scripts/generate-experiments.py from apps/"), f)
            x = yaml.safe_load(text)
            self.assertEqual(experiment.check_format(x) + experiment.check_deadlines(x), [], f)
            self.assertEqual(x["generated"]["version"], g.VERSION)
            self.assertTrue(all(len(line) <= 160 for line in text.split("\n")), f)
            self.assertIn(x["fault"][0]["spec"]["selector"]["labelSelectors"]["app"], {"sample-app", "web", "api"})
        probe = yaml.safe_load(read(os.path.join(self.apps, "storefront/experiments/generated/storefront-web-probe-dev.yaml")))
        self.assertEqual(probe["expect"]["fire"][0]["labels"], {"namespace": "storefront-dev", "slo": "storefront-web-probe",
                                                                "severity": "page", "long": "1h"})
        self.assertEqual(probe["expect"]["fire"][0]["within"], "11m")
        self.assertEqual(len(probe["expect"]["may_fire"]), 7)
        self.assertNotIn("storefront-web-probe", [m["labels"]["slo"] for m in probe["expect"]["may_fire"]])
        lat = yaml.safe_load(read(os.path.join(self.apps, "storefront/experiments/generated/storefront-api-latency-dev.yaml")))
        self.assertEqual(lat["expect"]["fire"][0]["alert"], "SLOMetricAbsent")
        self.assertEqual(lat["expect"]["fire"][0]["within"], "18m")   # Pyrra: for 10m on latency + 8
        self.assertEqual(lat["effect"]["query"], 'absent(http_request_duration_seconds_count{namespace="storefront-dev", '
                                                 'app="api", route="GET /products"})')
        self.assertEqual(lat["fault"][0]["spec"]["selector"]["labelSelectors"], {"app": "api"})
        self.assertGreaterEqual(experiment.dur(lat["fault"][0]["spec"]["duration"]), experiment.fault_limit(lat))
        rc, out = quiet(g.main, ["--apps", self.apps, "--check"])
        self.assertEqual(rc, 0, out)

    def test_never_overwrites_without_force_never_hand_written(self):
        self.gen()
        f = os.path.join(self.apps, "sample-app/experiments/generated/sample-app-hello-probe-dev.yaml")
        with open(f, "a") as fh:
            fh.write("# my note\n")
        rc, out = self.gen()
        self.assertIn("exists (regenerate with --force)", out)
        self.assertIn("# my note", read(f))
        self.gen("--force")
        self.assertNotIn("# my note", read(f))
        with open(f, "w") as fh:   # someone rewrote it by hand, without the generated field
            fh.write(read(os.path.join(self.apps, "sample-app/experiments/sample-app-down.yaml")))
        rc, out = self.gen("--force")
        self.assertIn("hand-written, kept", out)
        self.assertNotIn("generated:", read(f))

    def test_hand_written_name_wins(self):
        src = os.path.join(self.apps, "sample-app/experiments/sample-app-down.yaml")
        shutil.copy(src, os.path.join(self.apps, "sample-app/experiments/sample-app-hello-probe-dev.yaml"))
        rc, out = self.gen()
        self.assertIn("an experiment named sample-app-hello-probe-dev exists already", out)
        self.assertNotIn("sample-app/experiments/generated/sample-app-hello-probe-dev.yaml", self.generated())

    def test_check_gate_and_missing(self):
        rc, out = quiet(g.main, ["--apps", self.apps, "--check"])
        self.assertEqual(rc, 1)
        self.assertIn("✗ storefront-web-probe", out)
        self.assertIn("✓ sample-app-hello-probe: sample-app-down", out)
        rc, out = self.gen("--missing")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.generated(), [
            "sample-app/experiments/generated/sample-app-hello-availability-dev.yaml",
            "sample-app/experiments/generated/sample-app-hello-latency-dev.yaml",
            "storefront/experiments/generated/storefront-api-latency-dev.yaml",
            "storefront/experiments/generated/storefront-web-probe-dev.yaml"])
        rc, out = quiet(g.main, ["--apps", self.apps, "--check"])
        self.assertEqual(rc, 0, out)

    def test_skipped_experiments_do_not_cover(self):
        f = os.path.join(self.apps, "sample-app/experiments/sample-app-down.yaml")
        x = experiment.load_yaml(f)
        x["skip"] = "testing"
        with open(f, "w") as fh:
            yaml.safe_dump(x, fh)
        rc, out = quiet(g.main, ["--apps", self.apps, "--check"])
        self.assertIn("✗ sample-app-hello-probe", out)

    def test_orphan(self):
        self.gen()
        app = os.path.join(self.apps, "sample-app/app.yaml")
        d = yaml.safe_load(read(app))
        d["slos"] = [s for s in d["slos"] if s["name"] != "hello-latency"]
        with open(app, "w") as fh:
            yaml.safe_dump(d, fh)
        rc, out = quiet(g.main, ["--apps", self.apps, "--check"])
        self.assertEqual(rc, 1)
        self.assertIn("generated from SLO 'hello-latency', which sample-app no longer declares", out)

    def test_what_cannot_be_derived_is_skipped(self):
        app = os.path.join(self.apps, "sample-app/app.yaml")
        d = yaml.safe_load(read(app))
        d["slos"] = [
            {"name": "outside", "objective": 99.5, "probe": {"url": "https://example.org/"}},
            {"name": "lax", "objective": 95, "probe": {"service": "sample-app", "port": 8080}},
            {"name": "ghost", "objective": 99.5, "probe": {"service": "nope", "port": 8080}},
            {"name": "wal", "objective": 99, "bool": {"metric": "cnpg:wal_archive_keeps_up:bool"}},
            {"name": "split", "objective": 99, "ratio": {"errors": {"metric": "e", "matchers": 'app="sample-app"'},
                                                         "total": {"metric": "t", "matchers": 'app="other"'}}},
            {"name": "prod-only", "objective": 99, "environments": ["prod"],
             "availability": {"metric": "m", "matchers": 'app="sample-app"', "errors": 'code="500"'}},
        ]
        with open(app, "w") as fh:
            yaml.safe_dump(d, fh)
        shutil.rmtree(os.path.join(self.apps, "storefront"))
        rc, out = self.gen()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.generated(), [], out)
        for why in ("outside (probe): probes a url", "lax (probe): objective 95%", "ghost (probe): Service nope",
                    "wal (bool): no matchers", "split (ratio): no equality matcher of 'app=\"other\"'",
                    "prod-only: no environment to test it in (only prod"):
            self.assertIn(why, out)
        rc, out = self.gen("--env", "prod")
        self.assertEqual(self.generated(), ["sample-app/experiments/generated/sample-app-prod-only-prod.yaml"], out)

    def test_cli_front(self):
        cli = [sys.executable, os.path.join(ROOT, "bin", "slo-platform"), "experiments"]
        r = subprocess.run(cli + ["check", "--apps", self.apps], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        r = subprocess.run(cli + ["generate", "--apps", self.apps, "--missing"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("4 written", r.stdout)
        r = subprocess.run(cli + ["check", "--apps", self.apps], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_regression_finds_generated(self):
        self.gen()
        rs = importlib.util.spec_from_file_location("regression", os.path.join(HERE, "experiment-regression.py"))
        r = importlib.util.module_from_spec(rs)
        rs.loader.exec_module(r)
        names = [os.path.basename(f) for f in r.experiment_files([os.path.join(self.apps, "*", "experiments")])]
        self.assertIn("sample-app-down.yaml", names)
        self.assertIn("storefront-web-probe-dev.yaml", names)


if __name__ == "__main__":
    unittest.main()
