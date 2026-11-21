"""Unit tests of scripts/capacity.py: what needs no cluster (settings, rates, PromQL, judging, the knee)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capacity as cap

APP = {
    "name": "shop",
    "environments": [{"name": "dev"}, {"name": "prod"}],
    "traffic": {"requests": [{"weight": 1, "method": "GET", "service": "web", "port": 8080, "path": "/"}]},
    "slos": [
        {"name": "avail", "objective": 99.5, "availability": {"metric": "http_requests_total", "matchers": 'app="web"', "errors": 'result!="ok"'}},
        {"name": "lat", "objective": 99, "latency": {"metric": "http_request_duration_seconds", "matchers": 'app="web"', "threshold": "0.5"}},
        {"name": "fresh", "objective": 99, "age": {"metric": "x", "max": "60s"}},
        {"name": "prod-only", "objective": 99, "environments": ["prod"], "probe": {"service": "web", "port": 8080, "path": "/"}},
    ],
}


def step(rate, slis, sent=None, dropped=0, reqs=1000):
    return {"rate": rate, "client": {"rps": rate if sent is None else sent, "dropped": dropped, "reqs": reqs},
            "server": {"slis": slis, "workloads": {"web": {"cpu": rate / 100, "pods": 2}}, "node_cpu": rate / 1000}}


class Config(unittest.TestCase):
    def test_defaults_and_slos(self):
        c = cap.config(APP, "dev")
        self.assertEqual([s["name"] for s in c["slos"]], ["avail", "lat"])   # no age (no per-step meaning), no prod-only
        self.assertEqual(c["workloads"], ["web"])
        self.assertEqual(c["ramp"]["step_s"], 90)
        self.assertEqual([s["name"] for s in cap.config(APP, "prod")["slos"]], ["avail", "lat", "prod-only"])

    def test_rejects(self):
        with self.assertRaisesRegex(ValueError, "no environment"):
            cap.config(APP, "staging")
        with self.assertRaisesRegex(ValueError, "not one of"):
            cap.config({**APP, "capacity": {"slos": ["fresh"]}}, "dev")
        with self.assertRaisesRegex(ValueError, "no slo"):
            cap.config({**APP, "capacity": {"slos": ["nope"]}}, "dev")
        with self.assertRaisesRegex(ValueError, "factor"):
            cap.config({**APP, "capacity": {"ramp": {"factor": 1}}}, "dev")
        with self.assertRaisesRegex(ValueError, "45s"):
            cap.config({**APP, "capacity": {"ramp": {"step": "60s", "warmup": "30s"}}}, "dev")
        with self.assertRaisesRegex(ValueError, "no requests"):
            cap.config({k: v for k, v in APP.items() if k != "traffic"}, "dev")

    def test_autoscaling_defaults(self):
        a = cap.config({**APP, "capacity": {"autoscaling": {"to": 2}}}, "dev")["autoscaling"]
        self.assertEqual((a["workload"], a["from"], a["to"], a["hold_s"]), ("web", 0.5, 2, 120))


class Rates(unittest.TestCase):
    def test_geometric_ends_at_max(self):
        self.assertEqual(cap.rates({"start": 10, "factor": 2, "max": 100}), [10, 20, 40, 80, 100])
        self.assertEqual(cap.rates({"start": 5, "factor": 2, "max": 5}), [5])

    def test_refine(self):
        self.assertEqual(cap.refine_rates(80, 160, 3), [100, 120, 140])
        self.assertEqual(cap.refine_rates(80, 81, 3), [])
        self.assertEqual(cap.refine_rates(0, 10, 0), [])


class Queries(unittest.TestCase):
    def test_availability(self):
        q = cap.sli_query(APP["slos"][0], "shop", "shop-dev", 60)
        self.assertIn('http_requests_total{namespace="shop-dev", app="web", result!="ok"}[60s]', q)
        self.assertIn("or vector(0)", q)   # no error counted yet is zero errors, not no data

    def test_latency_uses_the_threshold_bucket(self):
        q = cap.sli_query(APP["slos"][1], "shop", "shop-dev", 75)
        self.assertIn('http_request_duration_seconds_bucket{namespace="shop-dev", app="web", le="0.5"}[75s]', q)
        self.assertIn("http_request_duration_seconds_count", q)

    def test_probe_and_ratio(self):
        self.assertIn('slo_probe="shop-prod-only"', cap.sli_query(APP["slos"][3], "shop", "shop-prod", 60))
        r = {"name": "r", "objective": 99, "ratio": {"errors": {"metric": "bad_total"}, "total": {"metric": "all_total"}}}
        q = cap.sli_query(r, "shop", "shop-dev", 60)
        self.assertIn('bad_total{namespace="shop-dev"}', q)
        self.assertIn('all_total{namespace="shop-dev"}', q)


class Judging(unittest.TestCase):
    slos = APP["slos"][:2]

    def test_holds_breaks_limited(self):
        self.assertEqual(cap.judge(step(10, {"avail": 1, "lat": 0.999}), self.slos)[0], "holds")
        v, why = cap.judge(step(10, {"avail": 0.99, "lat": 1}), self.slos)
        self.assertEqual(v, "breaks")
        self.assertIn("avail", why)
        self.assertEqual(cap.judge(step(100, {"avail": 1, "lat": 1}, sent=70), self.slos)[0], "limited")
        self.assertEqual(cap.judge(step(100, {"avail": 1, "lat": 1}, dropped=50), self.slos)[0], "limited")

    def test_a_breaking_slo_wins_over_a_limited_generator(self):
        self.assertEqual(cap.judge(step(100, {"avail": 0.9, "lat": 1}, sent=50), self.slos)[0], "breaks")

    def test_what_the_in_pod_slis_cannot_see_breaks(self):
        s = step(1280, {"avail": 1, "lat": 1}, sent=500, dropped=60000)   # looks like a limited generator ...
        s["server"]["workloads"]["web"].update(min_ready=0, restarts=6, oom_killed=6)   # ... but web was gone
        v, why = cap.judge(s, self.slos, ["web"])
        self.assertEqual(v, "breaks")
        self.assertIn("no pod ready", why)
        self.assertIn("OOMKilled", why)
        s = step(100, {"avail": 1, "lat": 1})
        s["client"]["failed"] = 0.03   # timeouts: users saw them, the pods did not
        self.assertIn("k6 saw", cap.judge(s, self.slos, ["web"])[1])

    def test_no_data_breaks(self):
        v, why = cap.judge(step(10, {"avail": None, "lat": 1}), self.slos)
        self.assertEqual(v, "breaks")
        self.assertIn("no data", why)

    def test_knee(self):
        steps = [dict(step(r, {}), verdict=v) for r, v in [(10, "holds"), (20, "holds"), (40, "breaks"), (30, "holds"), (35, "breaks")]]
        self.assertEqual(cap.knee(steps), (30, 35, False))
        steps = [dict(step(r, {}), verdict=v) for r, v in [(10, "holds"), (20, "limited")]]
        self.assertEqual(cap.knee(steps), (10, None, True))

    def test_knee_needs_every_run_to_hold(self):
        steps = [dict(step(r, {}), verdict=v) for r, v in [(10, "holds"), (20, "holds"), (20, "breaks"), (40, "breaks")]]
        self.assertEqual(cap.knee(steps), (10, 20, False))
        self.assertEqual(cap.unstable(steps), [20])

    def test_bottleneck_says_when_the_app_cpu_did_not_grow(self):
        good, bad = step(20, {}), step(20, {})
        good["server"].update(used_cores=6, cores=8, host_cpu=1, neighbours=[])
        bad["server"].update(used_cores=7.5, cores=8, host_cpu=2.5, neighbours=[{"namespace": "argocd", "cpu": 1.0}], swap_out=50)
        out = cap.bottleneck(good, bad, ["web"])
        self.assertTrue(out[0].startswith("web:"))
        self.assertTrue(any("argocd" in l for l in out))
        self.assertTrue(any("swapping" in l for l in out))
        self.assertIn("not their CPU", out[-1])
        bad["server"]["workloads"]["web"]["cpu"] = 1.0
        self.assertNotIn("not their CPU", cap.bottleneck(good, bad, ["web"])[-1])


class K6(unittest.TestCase):
    def test_script_embeds_requests_and_scenarios(self):
        s = cap.k6_script([{"weight": 1, "method": "GET", "url": "http://x/"}], {"step": cap.constant(50, 90)})
        self.assertIn('"url": "http://x/"', s)
        self.assertIn('"rate": 50', s)
        self.assertIn("handleSummary", s)
        self.assertEqual(cap.constant(1000, 60)["maxVUs"], 2000)

    def test_resolve(self):
        reqs = APP["traffic"]["requests"]
        self.assertEqual(cap.resolve(reqs, "shop-dev", None)[0]["url"], "http://web.shop-dev.svc:8080/")
        self.assertEqual(cap.resolve(reqs, "shop-dev", {"web": {8080: "http://10.0.0.1:31000"}})[0]["url"], "http://10.0.0.1:31000/")


if __name__ == "__main__":
    unittest.main()
