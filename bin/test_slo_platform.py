"""Tests of bin/slo-platform: init, check (a clean instance and each kind of problem), upgrade, the schema subset.
Temporary directories only; the --age tests use fake age-keygen / sops on PATH."""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("slo_platform", os.path.join(HERE, "slo-platform"))
_spec = importlib.util.spec_from_loader("slo_platform", _loader)
sp = importlib.util.module_from_spec(_spec)
_loader.exec_module(sp)

LAB = [{"name": "lab", "role": "lab", "runtime": "kind", "objectStorage": "none"}]
quiet = lambda *a, **k: None   # noqa: E731


def read(path):
    with open(path) as f:
        return f.read()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.product = os.path.join(self.tmp, "product")   # a product checkout without values.schema.json
        os.makedirs(self.product)
        shutil.copytree(os.path.join(sp.PRODUCT, "examples"), os.path.join(self.product, "examples"))

    def instance(self, examples=("sample-app", "storefront"), **kw):
        d = os.path.join(self.tmp, "inst")
        sp.init(d, "acme", "slo-instance", kw.pop("clusters", LAB), list(examples), "main", product=self.product,
                say=quiet, **kw)
        return d

    def edit(self, path, fn):
        with open(path) as f:
            data = yaml.safe_load(f)
        fn(data)
        with open(path, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)

    def check(self, d):
        orig = sp.PRODUCT
        sp.PRODUCT = self.product
        try:
            return sp.check(d)
        finally:
            sp.PRODUCT = orig


class Init(Tmp):
    def test_layout(self):
        d = self.instance()
        for f in ("platform.yaml", ".gitignore", "README.md", "apps/sample-app/app.yaml",
                  "apps/storefront/experiments/api-errors.yaml", "apps/storefront/worker/chart/Chart.yaml"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        p = yaml.safe_load(read(os.path.join(d, "platform.yaml")))
        self.assertEqual(p["apiVersion"], "slo-platform/v1")
        self.assertEqual(p["product"], {"repo": sp.PRODUCT_REPO, "version": "main"})
        self.assertEqual(p["instance"], {"repo": "https://github.com/acme/slo-instance.git", "revision": "main"})
        self.assertEqual(p["clusters"], {"lab": {"role": "lab", "runtime": "kind", "objectStorage": "none"}})
        gi = read(os.path.join(d, ".gitignore"))
        for g in (".tofu/", "*.tfstate*", ".secrets/", "*.decrypted.*"):
            self.assertIn(g, gi.splitlines())
        self.assertIn("cluster=lab", read(os.path.join(d, "README.md")))
        self.assertEqual(self.check(d), [])

    def test_state_and_secret_dirs_owner_only(self):
        d = self.instance()
        for x in (".tofu", ".secrets"):
            self.assertEqual(os.stat(os.path.join(d, x)).st_mode & 0o777, 0o700, x)

    def test_results_not_copied(self):
        os.makedirs(os.path.join(self.product, "examples", "sample-app", "experiments", "results"))
        open(os.path.join(self.product, "examples", "sample-app", "experiments", "results", "x.md"), "w").close()
        d = self.instance(["sample-app"])
        self.assertFalse(os.path.exists(os.path.join(d, "apps", "sample-app", "experiments", "results")))

    def test_refuses_non_empty_dir(self):
        d = os.path.join(self.tmp, "inst")
        os.makedirs(d)
        open(os.path.join(d, "keep"), "w").close()
        with self.assertRaises(RuntimeError):
            sp.init(d, "acme", "x", LAB, [], "main", product=self.product, say=quiet)
        self.assertEqual(os.listdir(d), ["keep"])

    def test_empty_dir_ok_no_examples(self):
        d = os.path.join(self.tmp, "inst")
        os.makedirs(d)
        sp.init(d, "acme", "x", LAB, [], "v1.2.3", product=self.product, say=quiet)
        self.assertEqual(yaml.safe_load(read(os.path.join(d, "platform.yaml")))["product"]["version"], "v1.2.3")
        self.assertEqual(self.check(d), [])

    def test_unknown_example_leaves_nothing(self):
        with self.assertRaises(RuntimeError):
            self.instance(["nope"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "inst")))

    def test_parse_cluster(self):
        self.assertEqual(sp.parse_cluster("home:prod:k3s:in-cluster"),
                         {"name": "home", "role": "prod", "runtime": "k3s", "objectStorage": "in-cluster"})
        self.assertEqual(sp.parse_cluster("lab:kind")["role"], "lab")
        for bad in ("lab", "x:kind", "lab:lab:docker", "Lab:lab:kind", "a:staging:kind", "a:lab:kind:s3"):
            with self.assertRaises(ValueError, msg=bad):
                sp.parse_cluster(bad)

    def test_cli_default_cluster_and_example(self):
        d = os.path.join(self.tmp, "cli")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sp.main(["init", d, "--owner", "acme", "--repo", "x"]), 0)
        p = yaml.safe_load(read(os.path.join(d, "platform.yaml")))
        self.assertEqual(list(p["clusters"]), ["lab"])
        self.assertEqual(os.listdir(os.path.join(d, "apps")), ["sample-app"])

    def fake_tools(self, sops=True):
        bindir = os.path.join(self.tmp, "fakebin")
        os.makedirs(bindir, exist_ok=True)
        tools = {"age-keygen": '#!/bin/sh\nif [ "$1" = -o ]; then printf "# created: now\\n# public key: age1fake\\n'
                               'AGE-SECRET-KEY-1FAKE\\n" > "$2"; else echo age1fake; fi\n'}
        if sops:   # "encrypts" by appending a sops section
            tools["sops"] = '#!/bin/sh\nfor f; do :; done\nprintf "sops:\\n  age: [{recipient: age1fake}]\\n" >> "$f"\n'
        for n, body in tools.items():
            with open(os.path.join(bindir, n), "w") as f:
                f.write(body)
            os.chmod(os.path.join(bindir, n), stat.S_IRWXU)
        old = os.environ["PATH"]
        keep = [p for p in old.split(":") if sops or not os.path.exists(os.path.join(p, "sops"))]   # hide a real sops
        os.environ["PATH"] = ":".join([bindir] + keep)
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def test_age_with_sops(self):
        self.fake_tools()
        key = os.path.join(self.tmp, "keys", "keys.txt")
        d = self.instance([], age=True, age_key_file=key,
                          clusters=LAB + [{"name": "prod", "role": "prod", "runtime": "k3s", "objectStorage": "none"}])
        self.assertTrue(os.path.exists(key))
        self.assertFalse(key.startswith(d))
        self.assertIn("age: age1fake", read(os.path.join(d, ".sops.yaml")))
        for c in ("lab", "prod"):
            self.assertIn("sops:", read(os.path.join(d, "secrets", f"{c}.sops.yaml")))
        self.assertFalse(os.path.exists(os.path.join(d, "secrets", "example.yaml")))

    def test_age_existing_key_not_overwritten(self):
        self.fake_tools()
        key = os.path.join(self.tmp, "keys.txt")
        with open(key, "w") as f:
            f.write("# public key: age1mine\nAGE-SECRET-KEY-1MINE\n")
        d = self.instance([], age=True, age_key_file=key)
        self.assertEqual(read(key), "# public key: age1mine\nAGE-SECRET-KEY-1MINE\n")
        self.assertIn("age: age1mine", read(os.path.join(d, ".sops.yaml")))

    def test_age_without_sops(self):
        self.fake_tools(sops=False)
        out = []
        d = os.path.join(self.tmp, "inst")
        sp.init(d, "acme", "x", LAB, [], "main", age=True, age_key_file=os.path.join(self.tmp, "k.txt"),
                product=self.product, say=out.append)
        self.assertTrue(os.path.exists(os.path.join(d, "secrets", "example.yaml")))
        self.assertFalse(os.path.exists(os.path.join(d, "secrets", "lab.sops.yaml")))
        self.assertTrue(any("warning" in o for o in out))
        self.assertIn("secrets/example.yaml", read(os.path.join(d, ".gitignore")))


class LatestRelease(unittest.TestCase):
    def test_newest_tag_else_main(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.assertEqual(sp.latest_release(d), "main")                     # not a git checkout
        g = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "a"], check=True)
        self.assertEqual(sp.latest_release(d), "main")                     # no tags
        subprocess.run(g + ["tag", "v0.1.0"], check=True)
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "b"], check=True)
        subprocess.run(g + ["tag", "v0.2.0"], check=True)
        subprocess.run(g + ["tag", "not-a-release"], check=True)
        self.assertEqual(sp.latest_release(d), "v0.2.0")


class Check(Tmp):
    def setUp(self):
        super().setUp()
        self.d = self.instance()
        self.p = os.path.join(self.d, "platform.yaml")
        self.sf = os.path.join(self.d, "apps", "storefront", "app.yaml")
        self.x = os.path.join(self.d, "apps", "storefront", "experiments", "api-errors.yaml")

    def assertProblem(self, needle):
        problems = self.check(self.d)
        self.assertTrue(any(needle in p for p in problems), f"{needle!r} not in {problems}")

    def test_clean(self):
        self.assertEqual(self.check(self.d), [])

    def test_example_platform_yaml(self):
        shutil.copy(os.path.join(sp.PRODUCT, "platform.example.yaml"), self.p)
        self.assertEqual(self.check(self.d), [])

    def test_no_platform_yaml(self):
        os.remove(self.p)
        self.assertProblem("missing")

    def test_platform_schema(self):
        for change, needle in [
            (lambda p: p.update(apiVersion="v2"), ".apiVersion: must be"),
            (lambda p: p.pop("instance"), "instance is required"),
            (lambda p: p.update(colour="blue"), ".colour: unknown key"),
            (lambda p: p["clusters"]["lab"].update(role="staging"), ".clusters.lab.role"),
            (lambda p: p["clusters"]["lab"].update(runtime="docker"), ".clusters.lab.runtime"),
            (lambda p: p["clusters"].update({"Big_Name": {"role": "lab"}}), ".clusters.Big_Name: invalid name"),
            (lambda p: p.update(clusters={}), "at least 1"),
            (lambda p: p.update(timezone="Mars/Base"), "unknown time zone"),
            (lambda p: p.update(language="fr"), ".language"),
            (lambda p: p["traffic"].update(dayMinutes=0), ".traffic.dayMinutes"),
            (lambda p: p["regression"].update(schedule="daily"), ".regression.schedule"),
            (lambda p: p["optional"].update(policies="yes"), ".optional.policies: must be boolean"),
        ]:
            with self.subTest(needle):
                shutil.copy(self.p, self.p + ".bak")
                self.edit(self.p, change)
                self.assertProblem(needle)
                shutil.move(self.p + ".bak", self.p)

    def test_schema_file_used_when_present(self):
        root = os.path.join(self.product, "platform", "root")
        os.makedirs(root)
        schema = json.loads(json.dumps(sp.SCHEMA))
        schema["properties"]["language"] = {"enum": ["en"]}   # stricter than the built-in mirror: proves it is read
        with open(os.path.join(root, "values.schema.json"), "w") as f:
            json.dump(schema, f)
        with open(os.path.join(root, "values.yaml"), "w") as f:
            f.write("cluster: ''\nlanguage: en\n")
        self.assertEqual(self.check(self.d), [])
        self.edit(self.p, lambda p: p.update(language="ja"))
        self.assertProblem(".language: must be one of en")

    def test_app_name_and_envs(self):
        self.edit(self.sf, lambda d: d.update(name="shop"))
        self.assertProblem("must be the directory name")

    def test_app_missing_environments(self):
        self.edit(self.sf, lambda d: d.pop("environments"))
        self.assertProblem("environments: a non-empty list")

    def test_app_promotion_from(self):
        self.edit(self.sf, lambda d: d["environments"][1]["promotion"].update({"from": "qa"}))
        self.assertProblem("promotion.from: 'qa'")

    def test_component_path_missing(self):
        shutil.rmtree(os.path.join(self.d, "apps", "storefront", "overlays", "staging"))
        self.assertProblem("apps/storefront/overlays/staging/kustomization.yaml does not exist")

    def test_helm_values_missing(self):
        os.remove(os.path.join(self.d, "apps", "storefront", "worker", "values-prod.yaml"))
        self.assertProblem("worker/values-prod.yaml does not exist")

    def test_component_kind(self):
        self.edit(self.sf, lambda d: d["components"][0].update(helm="x"))
        self.assertProblem("exactly one of kustomize, helm")

    def test_slo_two_types(self):
        self.edit(self.sf, lambda d: d["slos"][0].update(latency={"metric": "m", "threshold": "0.1"}))
        self.assertProblem("exactly one SLI type")

    def test_slo_no_type(self):
        self.edit(self.sf, lambda d: d["slos"][0].pop("availability"))
        self.assertProblem("found none")

    def test_slo_fields(self):
        self.edit(self.sf, lambda d: d["slos"][7]["ratio"].pop("total"))
        self.assertProblem("ratio: total.metric required")

    def test_slo_objective(self):
        self.edit(self.sf, lambda d: d["slos"][0].update(objective=100))
        self.assertProblem("objective")

    def test_traffic_fields(self):
        self.edit(self.sf, lambda d: d["traffic"]["requests"][0].pop("port"))
        self.assertProblem("traffic.requests[0]: port required")

    def test_traffic_bad_rate(self):
        self.edit(self.sf, lambda d: d["traffic"]["requests"][1].update(badRate=2))
        self.assertProblem("badRate")

    def test_experiment_namespace(self):
        self.edit(self.x, lambda x: x.update(namespace="storefront-qa"))
        self.assertProblem("namespace 'storefront-qa' is not <app>-<env>")

    def test_experiment_unknown_slo(self):
        self.edit(self.x, lambda x: x["expect"]["fire"][0]["labels"].update(slo="storefront-checkout"))
        self.assertProblem("slo 'storefront-checkout' is not <app>-<slo name>")

    def test_experiment_slo_of_other_app(self):
        self.edit(self.x, lambda x: x["expect"]["quiet"][0]["labels"].update(slo="sample-app-hello-probe"))
        self.assertProblem("belongs to sample-app")

    def test_experiment_slo_environment(self):
        self.edit(self.sf, lambda d: d["slos"][0].update(environments=["prod"]))   # web-availability: prod only
        self.assertProblem("not declared for environment staging")

    def test_experiment_duplicate_name(self):
        shutil.copy(self.x, os.path.join(self.d, "apps", "sample-app", "experiments", "api-errors.yaml"))
        self.assertProblem("names are unique across applications")

    def test_experiment_removed_slo(self):
        self.edit(self.sf, lambda d: d.update(slos=[s for s in d["slos"] if s["name"] != "api-availability"]))
        self.assertProblem("slo 'storefront-api-availability'")

    def test_cli_exit_code(self):
        self.edit(self.x, lambda x: x.update(namespace="nowhere"))
        orig, sp.PRODUCT = sp.PRODUCT, self.product
        try:
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(sp.main(["check", self.d]), 1)
        finally:
            sp.PRODUCT = orig
        self.assertIn("1 problem(s)", err.getvalue())


class ExistingRepository(Tmp):
    """instance.apps, regression.experiments, clusters.*.extras, components, namespace metadata: an existing GitOps
    repository with its applications at the root, against the built-in schema and the product's values.schema.json."""

    def setUp(self):
        super().setUp()
        d = self.instance()
        self.d = os.path.join(self.tmp, "existing")      # the same instance, applications moved to the root
        os.makedirs(os.path.join(self.d, "experiments"))
        os.makedirs(os.path.join(self.d, "clusters", "lab"))
        for app in ("sample-app", "storefront"):
            shutil.move(os.path.join(d, "apps", app), os.path.join(self.d, app))
            for x in os.listdir(os.path.join(self.d, app, "experiments")):
                shutil.move(os.path.join(self.d, app, "experiments", x), os.path.join(self.d, "experiments", x))
            os.rmdir(os.path.join(self.d, app, "experiments"))
        self.p = os.path.join(self.d, "platform.yaml")
        shutil.move(os.path.join(d, "platform.yaml"), self.p)
        self.sf = os.path.join(self.d, "storefront", "app.yaml")
        self.edit(self.p, lambda p: (p["instance"].update(apps="."), p["regression"].update(experiments="experiments"),
                                     p["clusters"]["lab"].update(extras=[{"name": "lab", "path": "clusters/lab"}])))

    def problems(self, real_schema=False):
        if not real_schema:
            return self.check(self.d)
        return sp.check(self.d)     # this checkout: platform/root/values.schema.json

    def assertProblem(self, needle, real_schema=None):
        for real in ((False, True) if real_schema is None else (real_schema,)):
            problems = self.problems(real)
            self.assertTrue(any(needle in p for p in problems), f"{needle!r} not in {problems} (schema file: {real})")

    def test_clean(self):
        for real in (False, True):
            self.assertEqual(self.problems(real), [])
        out = io.StringIO()
        with redirect_stdout(out):
            orig, sp.PRODUCT = sp.PRODUCT, self.product
            try:
                self.assertEqual(sp.main(["check", self.d]), 0)
            finally:
                sp.PRODUCT = orig
        self.assertIn("2 application(s), 14 experiment(s)", out.getvalue())

    def test_default_layout_finds_nothing(self):
        """Without instance.apps the root's applications are not found: their experiments name unknown namespaces."""
        self.edit(self.p, lambda p: p["instance"].pop("apps"))
        self.assertProblem("is not <app>-<env> of an application here (none)")

    def test_apps_and_experiments_paths(self):
        self.edit(self.sf, lambda d: d.update(name="shop"))
        self.assertProblem("storefront/app.yaml: name 'shop'")     # named relative to the instance, no apps/
        for apps in ("../elsewhere", "/abs", "a/../b"):
            self.edit(self.p, lambda p: p["instance"].update(apps=apps))
            self.assertProblem(".instance.apps")
        self.edit(self.p, lambda p: (p["instance"].update(apps="."), p["regression"].update(experiments=["../x"])))
        self.assertProblem(".regression.experiments")

    def test_experiment_globs(self):
        self.edit(self.p, lambda p: p["regression"].update(experiments=["experiments", "*/experiments"]))
        self.assertEqual(self.problems(), [])
        with open(os.path.join(self.d, "experiments", "broken.yaml"), "w") as f:
            f.write("namespace: nowhere\nfault: []\nexpect: {}\n")
        self.assertProblem("broken.yaml: namespace 'nowhere'")
        self.edit(self.p, lambda p: p["regression"].update(experiments="elsewhere"))
        self.assertEqual(self.problems(), [])      # nothing there: no experiments, no problem

    def test_extras(self):
        self.edit(self.p, lambda p: p["clusters"]["lab"].update(extras=[{"name": "lab", "path": "clusters/nowhere"}]))
        self.assertProblem(".clusters.lab.extras[0].path: clusters/nowhere is not a directory of the instance")
        self.edit(self.p, lambda p: p["clusters"]["lab"].update(extras=[{"name": "a", "path": "clusters/lab"}] * 2))
        self.assertProblem(".clusters.lab.extras: a twice")
        for bad in ({"name": "a"}, {"name": "Big", "path": "x"}, {"name": "a", "path": "../x"}, {"name": "a", "path": "x", "y": 1}):
            self.edit(self.p, lambda p: p["clusters"]["lab"].update(extras=[bad]))
            self.assertProblem(".clusters.lab.extras[0]")

    def test_applications_exclude(self):
        self.edit(self.p, lambda p: p["clusters"]["lab"].update(applications={"exclude": ["storefront"]}))
        for real in (False, True):
            self.assertEqual(self.problems(real), [])
        self.edit(self.p, lambda p: p["clusters"]["lab"].update(applications={"exclude": ["shop"]}))
        self.assertProblem(".clusters.lab.applications.exclude: shop is not an application here (sample-app, storefront)")
        self.edit(self.p, lambda p: p["clusters"]["lab"].update(applications={"exclude": ["storefront"] * 2}))
        self.assertProblem(".clusters.lab.applications.exclude: storefront twice")
        for bad in ({"exclude": ["Big"]}, {"exclude": "storefront"}, {"include": ["storefront"]}):
            self.edit(self.p, lambda p: p["clusters"]["lab"].update(applications=bad))
            self.assertProblem(".clusters.lab.applications")

    def test_components(self):
        self.edit(self.p, lambda p: p.update(components={
            "monitoring": {"values": {"grafana": {"enabled": False}}}, "pyrra": {"enabled": False},
            "chaos-mesh": {"enabled": False, "values": {}}}))
        for real in (False, True):
            self.assertEqual(self.problems(real), [])
        self.edit(self.p, lambda p: p.update(components={"cnpg": {"values": {"x": 1}}}))
        self.assertProblem(".components.cnpg.values: cnpg is installed from kustomize manifests, not a Helm chart")
        self.edit(self.p, lambda p: p.update(components={"grafana": {"enabled": False}}))
        self.assertProblem(".components.grafana: unknown key")
        self.edit(self.p, lambda p: p.update(components={"monitoring": {"enabled": "no"}}))
        self.assertProblem(".components.monitoring.enabled: must be boolean")

    def test_namespace_metadata(self):
        self.edit(self.p, lambda p: p.update(applications={"namespaceLabels": {"example.com/policies": "enforced"},
                                                           "namespaceAnnotations": {"example.com/note": "any text"}}))
        self.edit(self.sf, lambda d: d.update(namespace={"labels": {"example.com/signed": "required"},
                                                         "annotations": {"note": "x y"}}))
        for real in (False, True):
            self.assertEqual(self.problems(real), [])
        for meta, needle in (({"namespaceLabels": {"slo-platform/app": "x"}}, "slo-platform/app is set by the platform"),
                             ({"namespaceAnnotations": {"chaos-mesh.org/inject": "x"}}, "chaos-mesh.org/inject is set"),
                             ({"namespaceLabels": {"a": "not valid"}}, ".applications.namespaceLabels.a"),
                             ({"namespaceLabels": {"a": True}}, ".applications.namespaceLabels.a"),
                             ({"labels": {}}, ".applications.labels: unknown key")):
            self.edit(self.p, lambda p: p.update(applications=meta))
            self.assertProblem(needle)
        self.edit(self.p, lambda p: p.pop("applications"))
        for ns, needle in (({"labels": {"slo-platform/env": "x"}}, "namespace.labels: slo-platform/env is set by the platform"),
                           ({"annotations": {"chaos-mesh.org/inject": "no"}}, "namespace.annotations: chaos-mesh.org/inject"),
                           ({"labels": {"a": True}}, "namespace.labels.a: a string"),
                           ({"labels": {"a": "b c"}}, "namespace.labels.a: 'b c' is not a valid label value"),
                           ({"labels": {"-a": "b"}}, "namespace.labels: '-a' is not a valid key"),
                           ({"label": {}}, "namespace: a mapping with labels and / or annotations"),
                           ({"labels": ["a"]}, "namespace.labels: a mapping")):
            self.edit(self.sf, lambda d: d.update(namespace=ns))
            self.assertProblem(needle, real_schema=False)


class Notify(Tmp):
    """notify: the product's real values.schema.json, the secret keys receivers need, the warnings of `check`."""
    PROD = [{"name": "home", "role": "prod", "runtime": "k3s", "objectStorage": "none"}] + LAB

    def setUp(self):
        super().setUp()
        self.d = self.instance(examples=(), clusters=self.PROD)
        self.p = os.path.join(self.d, "platform.yaml")

    def set_notify(self, notify):
        with open(self.p) as f:
            data = yaml.safe_load(f)
        data["notify"] = notify
        with open(self.p, "w") as f:
            yaml.safe_dump(data, f)

    def secrets(self, cluster, notify):
        os.makedirs(os.path.join(self.d, "secrets"), exist_ok=True)
        with open(os.path.join(self.d, "secrets", f"{cluster}.sops.yaml"), "w") as f:   # as SOPS leaves it: keys readable
            yaml.safe_dump({"notify": {k: "ENC[AES256_GCM,data:x,type:str]" for k in notify}, "sops": {"age": []}}, f)

    def test_schema(self):
        ok = {"language": "ja", "page": [{"type": "telegram", "chatId": "-100123"}, {"type": "ntfy", "topic": "x"}],
              "ticket": [{"type": "email", "to": "a@b.c", "from": "d@e.f", "smarthost": "smtp:587"}],
              "deadman": {"url": "https://hc-ping.com/x", "interval": "1m"}}
        self.set_notify(ok)
        self.assertEqual(sp.check(self.d), [])   # the product's values.schema.json (sp.PRODUCT)
        for notify, needle in [
                ({"page": [{"type": "sms"}]}, ".notify.page[0].type"),
                ({"page": [{"type": "telegram"}]}, "chatId is required"),
                ({"page": [{"type": "telegram", "chatId": 1, "token": "x"}]}, ".notify.page[0].token: unknown key"),
                ({"page": [{"type": "slack", "url": "https://hooks.slack.com/x"}]}, ".notify.page[0].url: unknown key"),
                ({"ticket": [{"type": "ntfy", "topic": "a b"}]}, ".notify.ticket[0].topic"),
                ({"deadman": {}}, ".notify.deadman"),
                ({"pager": []}, ".notify.pager: unknown key")]:
            with self.subTest(needle):
                self.set_notify(notify)
                problems = sp.check(self.d)
                self.assertTrue(any(needle in p for p in problems), f"{needle!r} not in {problems}")

    def test_secret_keys(self):
        self.assertEqual(sp.notify_secret_keys({}), {})
        keys = sp.notify_secret_keys({
            "page": [{"type": "telegram", "chatId": 1}, {"type": "ntfy", "topic": "t"},
                     {"type": "ntfy", "topic": "t", "secretKey": "ntfyToken"}, {"type": "pushover"}],
            "ticket": [{"type": "email", "to": "a", "from": "b", "smarthost": "c:1"},
                       {"type": "email", "to": "a", "from": "b", "smarthost": "c:1", "username": "u", "secretKey": "smtp2"},
                       {"type": "webhook", "url": "http://x"}, {"type": "webhook"}, {"type": "slack", "secretKey": "s2"}],
            "deadman": {"interval": "1m"}})
        self.assertEqual(sorted(keys), ["deadmanUrl", "ntfyToken", "pushoverToken", "pushoverUserKey", "s2", "smtp2",
                                        "telegramBotToken", "webhookUrl"])
        self.assertEqual(keys["telegramBotToken"], "notify.page[0] (telegram)")
        self.assertEqual(sp.notify_secret_keys({"deadman": {"url": "https://x"}}), {})

    def test_warnings(self):
        self.set_notify({"page": [{"type": "telegram", "chatId": 1}], "ticket": [{"type": "discord"}]})
        w = sp.notify_warnings(self.d)
        self.assertEqual(len(w), 1)
        self.assertIn("secrets/home.sops.yaml: missing", w[0])          # only the prod cluster
        self.secrets("home", ["telegramBotToken"])
        self.assertEqual(sp.notify_warnings(self.d), ["secrets/home.sops.yaml: notify.discordWebhookUrl missing "
                                                      "(used by notify.ticket[0] (discord))"])
        self.secrets("home", ["telegramBotToken", "discordWebhookUrl"])
        self.assertEqual(sp.notify_warnings(self.d), [])
        with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            self.secrets("home", ["telegramBotToken"])
            self.assertEqual(sp.main(["check", self.d]), 0)               # a warning, not a problem
        self.assertIn("! secrets/home.sops.yaml: notify.discordWebhookUrl missing", err.getvalue())

    def test_skeleton_lists_every_default_key(self):
        text = sp.SECRETS_SKELETON.format(cluster="c", path="p")
        self.assertEqual(yaml.safe_load(text), {"secrets": {}})           # everything else commented out
        for keys in sp.NOTIFY_SECRETS.values():
            for _, default, *_ in keys:
                if default:
                    self.assertIn(f"#   {default}: ", text)
        self.assertIn("#   deadmanUrl: ", text)


class Upgrade(Tmp):
    def test_block_style_keeps_comments(self):
        d = self.instance([])
        p = os.path.join(d, "platform.yaml")
        before = read(p)
        sp.upgrade(d, "v0.2.0", say=quiet)
        after = read(p)
        self.assertEqual(yaml.safe_load(after)["product"]["version"], "v0.2.0")
        diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0][1], "  version: v0.2.0")
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))
        self.assertIn("# the platform: this repository", after)

    def test_inline_comment_and_flow_style(self):
        t = "apiVersion: slo-platform/v1\nproduct:   # where\n  repo: r\n  # the ref\n  version: main   # pinned\nx: 1\n"
        self.assertEqual(sp.set_version(t, "v1"), t.replace("version: main", "version: v1"))
        t = "product: { repo: r, version: main }   # c\ninstance: { repo: i, revision: main }\n"
        self.assertEqual(sp.set_version(t, "v1"), t.replace("version: main", "version: v1"))
        t = 'product:\n  repo: r\n  version: "main"\n'
        self.assertEqual(sp.set_version(t, "v1"), 'product:\n  repo: r\n  version: v1\n')

    def test_not_other_versions(self):
        t = "other:\n  version: a\nproduct:\n  repo: r\n  version: b\n"
        self.assertEqual(sp.set_version(t, "v1"), "other:\n  version: a\nproduct:\n  repo: r\n  version: v1\n")
        with self.assertRaises(RuntimeError):
            sp.set_version("product:\n  repo: r\nother:\n  version: a\n", "v1")

    def test_bad_ref(self):
        d = self.instance([])
        with self.assertRaises(RuntimeError):
            sp.upgrade(d, "v1 ; rm", say=quiet)


class Schema(unittest.TestCase):
    def test_subset(self):
        s = {"$defs": {"n": {"type": "integer", "minimum": 1}},
             "type": "object", "properties": {"a": {"$ref": "#/$defs/n"},
                                              "b": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                              "c": {"type": "array", "items": {"enum": [1, 2]}, "uniqueItems": True}},
             "patternProperties": {"^x-": {"type": "boolean"}}, "additionalProperties": False}
        self.assertEqual(sp.validate({"a": 2, "b": None, "c": [1, 2], "x-y": True}, s), [])
        errs = sp.validate({"a": 0, "b": 3, "c": [1, 1, 3], "x-y": 1, "z": 1}, s)
        for needle in (".a: 0 violates minimum", ".b: matches none", ".c: items must be unique", ".c[2]", ".x-y",
                       ".z: unknown key"):
            self.assertTrue(any(needle in e for e in errs), (needle, errs))
        self.assertEqual(sp.validate(True, {"type": "integer"}), [".: must be integer, not bool True"])


if __name__ == "__main__":
    unittest.main()
