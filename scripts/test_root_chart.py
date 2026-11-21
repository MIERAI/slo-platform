"""The root chart (platform/root): what it renders per cluster role, runtime and option, the root Application exactly
as bootstrap creates it, the ApplicationSet's sources, and its refusals (no / unknown cluster, typos). Needs helm
(skipped without it); scripts/render.py on an instance."""
import copy
import json
import os
import re
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


def alertmanager(objs):
    """platform-monitoring's Alertmanager: {config, templates (templateFiles), secrets (alertmanagerSpec.secrets)}"""
    am = helm_values(objs["platform-monitoring"])["alertmanager"]
    return {"config": am["config"], "templates": am.get("templateFiles") or {},
            "secrets": am["alertmanagerSpec"].get("secrets") or []}


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
        prod, lab = alertmanager(ok(BASE, "prod")), alertmanager(ok(BASE, "lab"))
        # notify: {} → as before notifications: page / ticket routed, to nobody
        self.assertEqual(prod["config"]["route"]["routes"], [
            {"receiver": "null", "matchers": ['alertname=~"Watchdog|InfoInhibitor"']},
            {"receiver": "null", "matchers": ['namespace=~".+-(dev|staging)"']},
            {"receiver": "null", "matchers": ['severity="page"']},
            {"receiver": "null", "matchers": ['severity="ticket"']}])
        self.assertEqual(prod["config"]["route"]["group_by"], ["alertname", "namespace"])
        self.assertEqual(prod["config"]["receivers"], [{"name": "null"}])
        self.assertEqual((prod["templates"], prod["secrets"]), ({}, []))
        self.assertNotIn("templates", prod["config"])
        self.assertEqual(lab["config"]["route"]["receiver"], "null")
        self.assertNotIn("routes", lab["config"]["route"])
        self.assertEqual(lab["config"]["receivers"], [{"name": "null"}])
        self.assertEqual(lab["config"]["inhibit_rules"], prod["config"]["inhibit_rules"])

    def test_notify_routing(self):
        n = {"page": [{"type": "webhook", "url": "http://p.example/x"}],
             "ticket": [{"type": "webhook", "url": "http://t.example/x"}, {"type": "ntfy", "topic": "t"}],
             "deadman": {"url": "https://hc-ping.com/abc", "interval": "2m"}}
        prod = alertmanager(ok(values(notify=n), "prod"))
        routes = prod["config"]["route"]["routes"]
        self.assertEqual(routes[0], {"receiver": "deadman", "matchers": ['alertname="Watchdog"'], "group_wait": "0s",
                                     "group_interval": "2m", "repeat_interval": "1s"})
        self.assertEqual(routes[1:], [
            {"receiver": "null", "matchers": ['alertname=~"Watchdog|InfoInhibitor"']},
            {"receiver": "null", "matchers": ['namespace=~".+-(dev|staging)"']},
            {"receiver": "page", "matchers": ['severity="page"']},
            {"receiver": "ticket", "matchers": ['severity="ticket"']}])
        rcv = {r["name"]: r for r in prod["config"]["receivers"]}
        self.assertEqual(rcv["deadman"], {"name": "deadman", "webhook_configs": [
            {"send_resolved": False, "url": "https://hc-ping.com/abc"}]})
        self.assertEqual([c["url"] for c in rcv["ticket"]["webhook_configs"]], ["http://t.example/x", "https://ntfy.sh/"])
        self.assertEqual(prod["secrets"], [])                              # no receiver here needs a secret
        self.assertEqual(prod["config"]["templates"], ["/etc/alertmanager/config/*.tmpl"])
        self.assertIn("slo.title", prod["templates"]["slo-platform.tmpl"])
        # only one severity configured: the other stays silent
        only = alertmanager(ok(values(notify={"ticket": n["ticket"]}), "prod"))["config"]
        self.assertEqual([r["receiver"] for r in only["route"]["routes"]], ["null", "null", "null", "ticket"])
        # a lab cluster notifies nobody, whatever notify says
        self.assertEqual(alertmanager(ok(values(notify=n), "lab")), alertmanager(ok(BASE, "lab")))

    def test_notify_receivers(self):
        f = "/etc/alertmanager/secrets/alertmanager-notify/"
        n = {"page": [{"type": "telegram", "chatId": "-1001234567890"},
                      {"type": "telegram", "chatId": 42, "threadId": 7, "secretKey": "opsBot"},
                      {"type": "slack", "channel": "#pages"}, {"type": "discord"}, {"type": "pushover"},
                      {"type": "ntfy", "url": "https://ntfy.example.com/", "topic": "pages", "secretKey": "ntfyToken"}],
             "ticket": [{"type": "email", "to": "ops@example.com", "from": "am@example.com", "smarthost": "smtp.example.com:587",
                         "username": "am"},
                        {"type": "email", "to": "x@example.com", "from": "am@example.com", "smarthost": "relay:25",
                         "requireTls": False},
                        {"type": "webhook"}, {"type": "msteams"}, {"type": "mattermost", "channel": "t"},
                        {"type": "pushover", "secretKey": "po", "userSecretKey": "pu"}],
             "deadman": {"interval": "1m"}}
        am = alertmanager(ok(values(notify=n), "prod"))
        self.assertEqual(am["secrets"], ["alertmanager-notify"])
        r = {x["name"]: x for x in am["config"]["receivers"]}
        p, t = r["page"], r["ticket"]
        self.assertEqual(p["telegram_configs"][0], {
            "send_resolved": True, "bot_token_file": f + "telegramBotToken", "chat_id": -1001234567890,
            "parse_mode": "HTML", "message": '{{ template "slo.telegram" . }}'})
        self.assertEqual((p["telegram_configs"][1]["chat_id"], p["telegram_configs"][1]["message_thread_id"],
                          p["telegram_configs"][1]["bot_token_file"]), (42, 7, f + "opsBot"))
        self.assertEqual({k: p["slack_configs"][0][k] for k in ("api_url_file", "channel", "send_resolved")},
                         {"api_url_file": f + "slackWebhookUrl", "channel": "#pages", "send_resolved": True})
        self.assertEqual(p["discord_configs"][0]["webhook_url_file"], f + "discordWebhookUrl")
        self.assertEqual(p["pushover_configs"][0]["token_file"], f + "pushoverToken")
        self.assertEqual(p["pushover_configs"][0]["user_key_file"], f + "pushoverUserKey")
        self.assertIn("1", p["pushover_configs"][0]["priority"])          # page: high priority while firing
        self.assertEqual(t["pushover_configs"][0]["priority"], "0")
        self.assertEqual((t["pushover_configs"][0]["token_file"], t["pushover_configs"][0]["user_key_file"]), (f + "po", f + "pu"))
        ntfy = p["webhook_configs"][0]
        self.assertEqual(ntfy["url"], "https://ntfy.example.com/")
        self.assertEqual(ntfy["http_config"], {"authorization": {"credentials_file": f + "ntfyToken"}})
        self.assertEqual(ntfy["payload"]["topic"], "pages")
        self.assertEqual(ntfy["payload"]["message"], '{{ template "slo.ntfy" . }}')
        e1, e2 = t["email_configs"]
        self.assertEqual((e1["auth_username"], e1["auth_password_file"], e1["require_tls"], e1["send_resolved"]),
                         ("am", f + "smtpPassword", True, True))
        self.assertNotIn("auth_password_file", e2)
        self.assertFalse(e2["require_tls"])
        self.assertEqual(e1["headers"], {"Subject": '{{ template "slo.title" . }}'})
        self.assertEqual(t["webhook_configs"][0], {"send_resolved": True, "url_file": f + "webhookUrl"})
        self.assertEqual(t["msteamsv2_configs"][0]["webhook_url_file"], f + "msteamsWebhookUrl")
        self.assertEqual(t["mattermost_configs"][0]["webhook_url_file"], f + "mattermostWebhookUrl")
        self.assertEqual(r["deadman"]["webhook_configs"], [{"send_resolved": False, "url_file": f + "deadmanUrl"}])

    def test_notify_secret_only_when_needed(self):
        for receiver, secret in (({"type": "webhook", "url": "http://x.example/"}, False), ({"type": "ntfy", "topic": "t"}, False),
                                 ({"type": "email", "to": "a@b.c", "from": "d@e.f", "smarthost": "h:25"}, False),
                                 ({"type": "webhook"}, True), ({"type": "discord"}, True),
                                 ({"type": "email", "to": "a@b.c", "from": "d@e.f", "smarthost": "h:25", "username": "u"}, True)):
            am = alertmanager(ok(values(notify={"page": [receiver]}), "prod"))
            self.assertEqual(am["secrets"], ["alertmanager-notify"] if secret else [], receiver)
        self.assertEqual(alertmanager(ok(values(notify={"deadman": {"url": "https://hc.example/x"}}), "prod"))["secrets"], [])
        self.assertEqual(alertmanager(ok(values(notify={"deadman": {"interval": "5m"}}), "prod"))["secrets"],
                         ["alertmanager-notify"])

    def test_notify_language(self):
        page = [{"type": "webhook", "url": "http://x.example/"}]
        for lang, notify_lang, word in (("en", None, "PAGE"), ("zh", None, "紧急"), ("ja", None, "緊急"), ("zh", "ja", "緊急"),
                                        ("ja", "en", "PAGE")):
            n = {"page": page} if notify_lang is None else {"page": page, "language": notify_lang}
            tmpl = alertmanager(ok(values(language=lang, notify=n), "prod"))["templates"]["slo-platform.tmpl"]
            self.assertIn('{{ define "slo.l.page" }}%s{{ end }}' % word, tmpl, (lang, notify_lang))
            self.assertEqual(tmpl.count('define "slo.l.page"'), 1)

    def test_notify_texts_cover_every_language(self):
        d = os.path.join(CHART, "files", "notify")
        words = None
        for lang in ("en", "zh", "ja"):
            with open(os.path.join(d, f"{lang}.tmpl")) as f:
                defined = sorted(re.findall(r'define "(slo\.l\.[a-z]+)"', f.read()))
            words = words or defined
            self.assertEqual(defined, words, lang)
        with open(os.path.join(d, "common.tmpl")) as f:
            used = set(re.findall(r'template "(slo\.l\.[a-z]+)"', f.read()))
        self.assertEqual(used, set(words))

    def test_alertmanager_version_everywhere(self):
        """amtool (CI) and the delivery test run the Alertmanager kube-prometheus-stack ships: the version noted next to
        the chart's pin."""
        with open(os.path.join(CHART, "templates", "platform-monitoring.yaml")) as f:
            v = re.search(r"targetRevision: [0-9.]+ +# Alertmanager v([0-9.]+)", f.read()).group(1)
        with open(os.path.join(ROOT, "scripts", "install-tools.sh")) as f:
            self.assertEqual(set(re.findall(r"alertmanager/releases/download/v([0-9.]+)/", f.read())), {v})
        with open(os.path.join(ROOT, "scripts", "notify-delivery.sh")) as f:
            self.assertEqual(set(re.findall(r"prometheus/alertmanager:v([0-9.]+)", f.read())), {v})

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
                (values(instance={"repo": "", "revision": "main"}), "prod", "instance"),
                (values(notify={"pages": []}), "prod", "pages"),
                (values(notify={"page": [{"type": "sms"}]}), "prod", "type"),
                (values(notify={"page": [{"type": "telegram"}]}), "prod", "chatId"),
                (values(notify={"page": [{"type": "telegram", "chatid": 1}]}), "prod", "chatid"),
                (values(notify={"page": [{"type": "telegram", "chatId": "@ops"}]}), "prod", "chatId"),
                (values(notify={"page": [{"type": "ntfy", "topic": "a/b"}]}), "prod", "topic"),
                (values(notify={"page": [{"type": "slack", "url": "https://hooks.slack.com/x"}]}), "prod", "url"),   # a secret
                (values(notify={"ticket": [{"type": "email", "to": "a@b.c"}]}), "prod", "from"),
                (values(notify={"deadman": {}}), "prod", "deadman"),
                (values(notify={"deadman": {"url": "https://x.example/", "secretKey": "k"}}), "prod", "deadman"),
                (values(notify={"language": "de"}), "prod", "language")):
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
