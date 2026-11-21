"""The root chart (platform/root): what it renders per cluster role, runtime and option, the root Application exactly
as bootstrap creates it, the ApplicationSet's sources, and its refusals (no / unknown cluster, typos). Needs helm
(skipped without it); scripts/render.py on an instance."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHART = os.path.join(ROOT, "platform", "root")
sys.path.insert(0, HERE)
import render  # noqa: E402

with open(os.path.join(CHART, "ci", "platform.yaml")) as f:
    BASE = yaml.safe_load(f)

COMMON = {"platform-argo-rollouts", "platform-blackbox", "platform-cert-manager", "platform-chaos-mesh", "platform-cnpg",
          "platform-monitoring", "platform-pyrra", "slo-platform", "applications"}


def helm(values, cluster):
    """→ (returncode, {name: object}, stderr)"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
    try:
        cmd = ["helm", "template", "slo-platform", CHART, "--namespace", "argocd", "-f", f.name]
        if cluster is not None:
            cmd += ["--set", f"cluster={cluster}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    objs = {d["metadata"]["name"]: d for d in yaml.safe_load_all(r.stdout) if d} if r.returncode == 0 else {}
    return r.returncode, objs, r.stderr


def ok(values, cluster):
    rc, objs, err = helm(values, cluster)
    assert rc == 0, err
    return objs


def values(**changes):
    v = copy.deepcopy(BASE)
    for k, x in changes.items():
        v[k] = x
    return v


def helm_values(app):
    return app["spec"]["source"]["helm"]["valuesObject"]


@unittest.skipUnless(shutil.which("helm"), "needs helm")
class RootChart(unittest.TestCase):
    def test_sets_per_role(self):
        prod, lab = ok(BASE, "prod"), ok(BASE, "lab")
        # prod: in-cluster object storage, policies on; lab: kind, no object storage
        self.assertEqual(set(prod), COMMON | {"platform-kyverno", "platform-policies", "platform-object-storage"})
        self.assertEqual(set(lab), COMMON | {"platform-kyverno", "platform-policies", "platform-experiments"})
        minimal = ok(values(optional={"policies": False}, clusters={"lab": {"role": "lab"}}), "lab")
        self.assertEqual(set(minimal), COMMON | {"platform-experiments"})

    def test_alertmanager(self):
        def config(objs):
            return helm_values(objs["platform-monitoring"])["alertmanager"]["config"]
        prod, lab = config(ok(BASE, "prod")), config(ok(BASE, "lab"))
        self.assertIn({"receiver": "default", "matchers": ['severity=~"page|ticket"']}, prod["route"]["routes"])
        self.assertEqual([r["name"] for r in prod["receivers"]], ["null", "default"])
        self.assertEqual(lab["route"]["receiver"], "null")
        self.assertNotIn("routes", lab["route"])
        self.assertEqual(lab["receivers"], [{"name": "null"}])
        self.assertEqual(lab["inhibit_rules"], prod["inhibit_rules"])

    def test_grafana_admin_from_secret(self):
        g = helm_values(ok(values(grafana={"adminSecret": "my-admin"}), "prod")["platform-monitoring"])["grafana"]
        self.assertEqual(g["admin"], {"existingSecret": "my-admin", "userKey": "admin-user", "passwordKey": "admin-password"})
        self.assertNotIn("adminPassword", g)

    def test_chaos_mesh_socket(self):
        for runtime, sock in (("k3s", "/run/k3s/containerd/containerd.sock"), ("kind", "/run/containerd/containerd.sock"),
                              ("generic", "/run/containerd/containerd.sock"), (None, "/run/containerd/containerd.sock")):
            c = {"role": "prod"} if runtime is None else {"role": "prod", "runtime": runtime}
            objs = ok(values(clusters={"x": c}), "x")
            self.assertEqual(helm_values(objs["platform-chaos-mesh"])["chaosDaemon"]["socketPath"], sock, runtime)

    def test_experiments_values(self):
        v = values(timezone="Europe/Berlin", regression={"schedule": "30 2 * * *"},
                   product={"repo": "https://git.example/p.git", "version": "v0.1.0"},
                   instance={"repo": "https://git.example/i.git", "revision": "live"})
        self.assertEqual(helm_values(ok(v, "lab")["platform-experiments"]), {
            "schedule": "30 2 * * *", "timeZone": "Europe/Berlin",
            "product": {"repo": "https://git.example/p.git", "version": "v0.1.0"},
            "instance": {"repo": "https://git.example/i.git", "revision": "live"}})

    def test_product_version_everywhere(self):
        v = values(product={"repo": "https://git.example/p.git", "version": "v9.9.9"})
        for name, o in ok(v, "lab").items():
            for s in o["spec"].get("sources") or [o["spec"].get("source") or o["spec"]["template"]["spec"]["source"]]:
                if s["repoURL"] == "https://git.example/p.git":
                    self.assertEqual(s["targetRevision"], "v9.9.9", name)
                elif "chart" not in s and "ref" not in s:
                    self.fail(f"{name}: source {s['repoURL']} is neither the product nor a chart")

    def test_root_application_is_the_contract(self):
        v = values(product={"repo": "https://git.example/p.git", "version": "v0.1.0"},
                   instance={"repo": "https://git.example/i.git", "revision": "main"})
        self.assertEqual(ok(v, "lab")["slo-platform"], {
            "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": "slo-platform", "namespace": "argocd"},
            "spec": {
                "project": "default",
                "sources": [
                    {"repoURL": "https://git.example/p.git", "targetRevision": "v0.1.0", "path": "platform/root",
                     "helm": {"valueFiles": ["$instance/platform.yaml"], "parameters": [{"name": "cluster", "value": "lab"}]}},
                    {"repoURL": "https://git.example/i.git", "targetRevision": "main", "ref": "instance"}],
                "destination": {"server": "https://kubernetes.default.svc", "namespace": "argocd"},
                "syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}})

    def test_applicationset_sources(self):
        v = values(product={"repo": "https://git.example/p.git", "version": "v0.1.0"},
                   instance={"repo": "https://git.example/i.git", "revision": "live"}, traffic={"dayMinutes": 60})
        s = ok(v, "prod")["applications"]
        g = render._git_generator(s["spec"]["generators"])
        self.assertEqual(g, {"repoURL": "https://git.example/i.git", "revision": "live", "files": [{"path": "apps/*/app.yaml"}]})
        src = s["spec"]["template"]["spec"]["source"]
        self.assertEqual((src["repoURL"], src["targetRevision"]), ("https://git.example/p.git", "v0.1.0"))
        patch = s["spec"]["templatePatch"]
        self.assertEqual(patch.count('repoURL: "https://git.example/i.git"'), 2)    # helm and kustomize components
        self.assertEqual(patch.count('targetRevision: "live"'), 2)
        self.assertIn('(dict "minutes" 60 "tzOffsetHours" 5.5)', patch)            # Asia/Kolkata
        self.assertNotIn("${", json.dumps(s))

    def test_timezone_offsets(self):
        for tz, off in (("Etc/UTC", "0"), ("Asia/Tokyo", "9"), ("America/New_York", "-5"), ("Europe/Berlin", "1"),
                        ("Etc/GMT+3", "-3"), ("Asia/Kathmandu", "5.75")):
            patch = ok(values(timezone=tz), "prod")["applications"]["spec"]["templatePatch"]
            self.assertIn(f'"tzOffsetHours" {off})', patch, tz)

    def test_timezones_match_schema(self):
        with open(os.path.join(CHART, "files", "timezones.yaml")) as f:
            table = yaml.safe_load(f)
        with open(os.path.join(CHART, "values.schema.json")) as f:
            enum = json.load(f)["properties"]["timezone"]["enum"]
        self.assertEqual(sorted(table), sorted(enum))
        self.assertTrue(all(isinstance(x, (int, float)) and -12 <= x <= 14 for x in table.values()))

    def test_refusals(self):
        for v, cluster, want in (
                (BASE, None, "cluster: not set"),
                (BASE, "staging", 'cluster "staging": not in clusters (lab, prod)'),
                (values(clustres={}), "prod", "clustres"),                                  # typo at the top level
                (values(clusters={"prod": {"role": "prod", "runtim": "k3s"}}), "prod", "runtim"),   # in a cluster
                (values(clusters={"prod": {"role": "production"}}), "prod", "role"),
                (values(timezone="Asia/Tokio"), "prod", "timezone"),
                (values(apiVersion="slo-platform/v2"), "prod", "apiVersion"),
                (values(instance={"repo": "", "revision": "main"}), "prod", "instance")):
            rc, _, err = helm(v, cluster)
            self.assertNotEqual(rc, 0, want)
            self.assertIn(want, err)


@unittest.skipUnless(shutil.which("helm") and shutil.which("kubectl"), "needs helm and kubectl")
class RenderInstance(unittest.TestCase):
    def test_empty_instance(self):
        """An instance with no application: only the platform's Applications (from the product)."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        os.makedirs(os.path.join(d, "apps"))
        shutil.copy(os.path.join(CHART, "ci", "platform.yaml"), os.path.join(d, "platform.yaml"))
        apps = render.apps(ROOT, instance=d, cluster="lab")
        self.assertIn("platform-experiments", {a["name"] for a in apps})
        self.assertTrue(all(not a["enforced"] and a["root"] == ROOT for a in apps))

    def test_examples_instance(self):
        if not os.path.isdir(os.path.join(ROOT, "examples")):
            self.skipTest("no examples")
        apps = {a["name"]: a for a in render.apps(ROOT, cluster="prod")}
        inst = render.example_instance(ROOT)
        for a in apps.values():
            self.assertTrue(os.path.isdir(os.path.join(a["root"], a["path"])), a)
            if a["enforced"] and not a["path"].startswith("platform/"):
                self.assertEqual(a["root"], inst)
                self.assertTrue(a["path"].startswith("apps/"), a)
        self.assertNotIn("platform-experiments", apps)


if __name__ == "__main__":
    unittest.main()
