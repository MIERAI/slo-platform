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
            "instance": {"repo": "https://git.example/i.git", "revision": "live"},
            "experiments": ["apps/*/experiments"]})

    def test_experiment_directories(self):
        def dirs(instance=None, regression=None):
            v = values(instance={**BASE["instance"], **(instance or {})},
                       regression={**BASE["regression"], **(regression or {})})
            return helm_values(ok(v, "lab")["platform-experiments"])["experiments"]
        self.assertEqual(dirs(), ["apps/*/experiments"])
        self.assertEqual(dirs({"apps": "."}), ["*/experiments"])
        self.assertEqual(dirs({"apps": "deploy/apps"}), ["deploy/apps/*/experiments"])
        self.assertEqual(dirs({"apps": "."}, {"experiments": "experiments"}), ["experiments"])
        self.assertEqual(dirs(None, {"experiments": ["a", "teams/*/experiments"]}), ["a", "teams/*/experiments"])

    def test_extras(self):
        v = values(clusters={"prod": {"role": "prod", "extras": [{"name": "mine", "path": "clusters/prod"},
                                                                 {"name": "ui", "path": "ui"}]},
                             "lab": {"role": "lab"}},
                   instance={"repo": "https://git.example/i.git", "revision": "live"})
        prod, lab = ok(v, "prod"), ok(v, "lab")
        self.assertTrue({"extras-mine", "extras-ui"} <= set(prod))
        self.assertFalse([n for n in lab if n.startswith("extras-")])
        self.assertEqual(prod["extras-mine"]["spec"], {
            "project": "default",
            "source": {"repoURL": "https://git.example/i.git", "targetRevision": "live", "path": "clusters/prod"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "argocd"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True},
                           "retry": {"limit": 5, "backoff": {"duration": "10s", "factor": 2, "maxDuration": "3m"}}}})
        dup = values(clusters={"prod": {"role": "prod", "extras": [{"name": "a", "path": "one"}, {"name": "a", "path": "two"}]}})
        rc, _, err = helm(dup, "prod")
        self.assertNotEqual(rc, 0)
        self.assertIn("clusters.prod.extras: a twice", err)

    def test_components_enabled(self):
        names = {"monitoring": "platform-monitoring", "pyrra": "platform-pyrra", "blackbox": "platform-blackbox",
                 "chaos-mesh": "platform-chaos-mesh", "cert-manager": "platform-cert-manager",
                 "argo-rollouts": "platform-argo-rollouts", "cnpg": "platform-cnpg", "experiments": "platform-experiments",
                 "kyverno": "platform-kyverno", "policies": "platform-policies", "object-storage": "platform-object-storage"}
        with open(os.path.join(CHART, "values.schema.json")) as f:
            self.assertEqual(set(json.load(f)["properties"]["components"]["properties"]), set(names) | {"tracing", "logging"})
        both = set(ok(BASE, "prod")) | set(ok(BASE, "lab"))
        for c, app in names.items():
            v = values(components={c: {"enabled": False}})
            self.assertIn(app, both, c)
            self.assertNotIn(app, set(ok(v, "prod")) | set(ok(v, "lab")), c)
            # enabled: true is the default, and does not override the role / option / storage that gates a component
            self.assertEqual(ok(values(components={c: {"enabled": True}}), "prod"), ok(BASE, "prod"), c)
        off = values(optional={"policies": False}, components={"kyverno": {"enabled": True}, "policies": {"enabled": True}})
        self.assertNotIn("platform-kyverno", ok(off, "prod"))

    def test_tracing(self):
        # opt-in, external only: a Tempo datasource in Grafana, no Application; off renders exactly as without the key
        ext = {"queryUrl": "http://tempo.example:3200", "endpoint": "http://tempo.example:4318"}
        for cl in ("prod", "lab"):
            self.assertEqual(ok(values(components={"tracing": {"external": ext}}), cl), ok(BASE, cl))
            self.assertEqual(ok(values(components={"tracing": {"enabled": False, "external": ext}}), cl), ok(BASE, cl))
        on = ok(values(components={"tracing": {"enabled": True, "external": ext}}), "prod")
        self.assertEqual(set(on), set(ok(BASE, "prod")))                        # no Application of its own
        g, g0 = (helm_values(o["platform-monitoring"])["grafana"] for o in (on, ok(BASE, "prod")))
        self.assertEqual(g.pop("datasources"), {"tempo.yaml": {"apiVersion": 1, "datasources": [
            {"name": "Tempo", "uid": "tempo", "type": "tempo", "access": "proxy", "url": "http://tempo.example:3200"}]}})
        self.assertEqual(g, g0)
        for name in set(on) - {"platform-monitoring"}:
            self.assertEqual(on[name], ok(BASE, "prod")[name], name)
        # with the user's additionalDataSources (a list that replaces): both are there
        mine = [{"name": "x", "type": "prometheus", "url": "http://x"}]
        v = values(components={"tracing": {"enabled": True, "external": ext},
                               "monitoring": {"values": {"grafana": {"additionalDataSources": mine}}}})
        g = helm_values(ok(v, "prod")["platform-monitoring"])["grafana"]
        self.assertEqual((g["additionalDataSources"], list(g["datasources"])), (mine, ["tempo.yaml"]))
        # per cluster: on for prod only, the URL at the top level
        v = values(components={"tracing": {"external": ext}})
        v["clusters"] = copy.deepcopy(v["clusters"])
        v["clusters"]["prod"]["components"] = {"tracing": {"enabled": True}}
        self.assertIn("datasources", helm_values(ok(v, "prod")["platform-monitoring"])["grafana"])
        self.assertEqual(ok(v, "lab"), ok(BASE, "lab"))
        # refusals: enabled without a URL; not a URL; values (no in-cluster Tempo in this release)
        for t, want in (({"enabled": True}, "enabled needs external.queryUrl"),
                        ({"enabled": True, "external": {"endpoint": "http://x:4318"}}, "queryUrl"),
                        ({"enabled": True, "external": {"queryUrl": "tempo:3200"}}, "queryUrl"),
                        ({"values": {"x": 1}}, "values")):
            rc, _, err = helm(values(components={"tracing": t}), "prod")
            self.assertNotEqual(rc, 0, t)
            self.assertIn(want, err)

    def test_logging(self):
        # like tracing: a Loki datasource in Grafana, no Application; next to Tempo's when both are on
        ext = {"queryUrl": "http://loki.example:3100"}
        tr = {"enabled": True, "external": {"queryUrl": "http://tempo.example:3200"}}
        for cl in ("prod", "lab"):
            self.assertEqual(ok(values(components={"logging": {"external": ext}}), cl), ok(BASE, cl))
        loki = {"apiVersion": 1, "datasources": [
            {"name": "Loki", "uid": "loki", "type": "loki", "access": "proxy", "url": "http://loki.example:3100"}]}
        on = ok(values(components={"logging": {"enabled": True, "external": ext}}), "prod")
        self.assertEqual(set(on), set(ok(BASE, "prod")))
        g, g0 = (helm_values(o["platform-monitoring"])["grafana"] for o in (on, ok(BASE, "prod")))
        self.assertEqual(g.pop("datasources"), {"loki.yaml": loki})
        self.assertEqual(g, g0)
        both = helm_values(ok(values(components={"logging": {"enabled": True, "external": ext}, "tracing": tr}),
                              "prod")["platform-monitoring"])["grafana"]["datasources"]
        self.assertEqual((sorted(both), both["loki.yaml"]), (["loki.yaml", "tempo.yaml"], loki))
        # tracing alone renders exactly as in 0.1.4
        t = helm_values(ok(values(components={"tracing": tr}), "prod")["platform-monitoring"])["grafana"]["datasources"]
        self.assertEqual(list(t), ["tempo.yaml"])
        for t, want in (({"enabled": True}, "components.logging: enabled needs external.queryUrl (an external Loki's"),
                        ({"enabled": True, "external": {"queryUrl": "loki:3100"}}, "queryUrl"),
                        ({"values": {"x": 1}}, "values")):
            rc, _, err = helm(values(components={"logging": t}), "prod")
            self.assertNotEqual(rc, 0, t)
            self.assertIn(want, err)

    def test_components_per_cluster(self):
        # a small lab drops Grafana and keeps prod's; cluster-level values merge over the top-level ones
        v = values(components={"monitoring": {"values": {"grafana": {"additionalDataSources": [{"name": "x"}]}}}})
        v["clusters"] = copy.deepcopy(v["clusters"])
        v["clusters"]["lab"]["components"] = {"monitoring": {"values": {"grafana": {"enabled": False}}},
                                              "blackbox": {"enabled": False}}
        prod, lab = ok(v, "prod"), ok(v, "lab")
        gp, gl = helm_values(prod["platform-monitoring"])["grafana"], helm_values(lab["platform-monitoring"])["grafana"]
        self.assertNotEqual(gp.get("enabled"), False)
        self.assertEqual(gp["additionalDataSources"], [{"name": "x"}])
        self.assertFalse(gl["enabled"])
        self.assertEqual(gl["additionalDataSources"], [{"name": "x"}])     # the top-level part still applies
        self.assertIn("platform-blackbox", prod)
        self.assertNotIn("platform-blackbox", lab)

    def test_components_values(self):
        n = {"page": [{"type": "telegram", "chatId": "-1001234567890"}]}
        v = values(notify=n, components={
            "monitoring": {"values": {
                "prometheus": {"prometheusSpec": {"storageSpec": {"volumeClaimTemplate": {"spec": {
                    "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "15Gi"}}}}}}},
                "grafana": {"enabled": False, "additionalDataSources": [{"name": "x", "type": "prometheus", "url": "http://x"}]},
                "kubeApiServer": {"serviceMonitor": {"metricRelabelings": []}},
                "kubeEtcd": None}},
            "chaos-mesh": {"values": {"chaosDaemon": {"socketPath": "/custom.sock"}}},
            "blackbox": {"values": {"replicas": 2}},
            "experiments": {"values": {"gitCredentialsSecret": "my-git"}}})
        base, objs = ok(values(notify=n), "lab"), ok(v, "lab")
        m, b = helm_values(objs["platform-monitoring"]), helm_values(base["platform-monitoring"])
        ps = m["prometheus"]["prometheusSpec"]
        self.assertEqual(ps["storageSpec"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"], "15Gi")
        self.assertEqual(ps["retention"], "30d")                                     # maps merge: the rest stays
        self.assertFalse(m["grafana"]["enabled"])                                    # false overrides
        self.assertEqual(m["grafana"]["admin"], b["grafana"]["admin"])
        self.assertEqual(len(m["grafana"]["additionalDataSources"]), 1)
        self.assertEqual(m["kubeApiServer"]["serviceMonitor"]["metricRelabelings"], [])   # lists replace
        self.assertIsNone(m["kubeEtcd"])                                             # null: the chart's default
        self.assertEqual(m["alertmanager"], b["alertmanager"])                       # untouched, numbers included
        cm = helm_values(objs["platform-chaos-mesh"])
        self.assertEqual(cm["chaosDaemon"]["socketPath"], "/custom.sock")
        self.assertEqual(cm["chaosDaemon"]["runtime"], "containerd")
        self.assertEqual(helm_values(objs["platform-blackbox"])["replicas"], 2)
        x = helm_values(objs["platform-experiments"])
        self.assertEqual((x["gitCredentialsSecret"], x["schedule"]), ("my-git", BASE["regression"]["schedule"]))
        # prod's Alertmanager after the round trip through the merge: the Telegram chat ID is still a number
        am = helm_values(ok(v, "prod")["platform-monitoring"])["alertmanager"]["config"]
        self.assertEqual(am, alertmanager(ok(values(notify=n), "prod"))["config"])
        self.assertEqual(next(r for r in am["receivers"] if r["name"] == "page")["telegram_configs"][0]["chat_id"],
                         -1001234567890)

    def test_namespace_metadata_expressions(self):
        v = values(applications={"namespaceLabels": {"example.com/a": "x", "b": ""},
                                 "namespaceAnnotations": {"note": 'say "hi"'}})
        patch = ok(v, "prod")["applications"]["spec"]["templatePatch"]
        self.assertIn('(get $ns "labels" | default dict) (dict "b" "" "example.com/a" "x") }}', patch)
        self.assertIn('(get $ns "annotations" | default dict) (dict "note" "say \\"hi\\"") }}', patch)
        plain = ok(BASE, "prod")["applications"]["spec"]["templatePatch"]
        self.assertIn('(get $ns "labels" | default dict) (dict) }}', plain)

    def test_chaos_mesh_off_cleans_up(self):
        # the Application deletes what it installed when it goes; namespaces get the inject annotation only where
        # Chaos Mesh runs
        on = ok(BASE, "prod")
        self.assertEqual(on["platform-chaos-mesh"]["metadata"]["finalizers"], ["resources-finalizer.argocd.argoproj.io"])
        self.assertIn('merge (dict "chaos-mesh.org/inject" "enabled") (get $ns "annotations"',
                      on["applications"]["spec"]["templatePatch"])
        for v in (values(components={"chaos-mesh": {"enabled": False}}),
                  values(clusters={**BASE["clusters"], "prod": {**BASE["clusters"]["prod"],
                                                               "components": {"chaos-mesh": {"enabled": False}}}})):
            off = ok(v, "prod")
            self.assertNotIn("platform-chaos-mesh", off)
            patch = off["applications"]["spec"]["templatePatch"]
            self.assertIn('merge (dict) (get $ns "annotations"', patch)
            self.assertNotIn("chaos-mesh.org/inject", patch.split("managedNamespaceMetadata")[0].split("$annotations :=")[-1])
            self.assertEqual(patch.replace('merge (dict) (get', 'merge (dict "chaos-mesh.org/inject" "enabled") (get'),
                             on["applications"]["spec"]["templatePatch"])
        self.assertIn("platform-chaos-mesh", ok(values(clusters={**BASE["clusters"], "prod": {**BASE["clusters"]["prod"],
                      "components": {"chaos-mesh": {"enabled": False}}}}), "lab"))   # other clusters keep it

    def test_product_version_everywhere(self):
        v = values(product={"repo": "https://git.example/p.git", "version": "v9.9.9"})
        for name, o in ok(v, "lab").items():
            for s in o["spec"].get("sources") or [o["spec"].get("source") or o["spec"]["template"]["spec"]["source"]]:
                if s["repoURL"] == "https://git.example/p.git":
                    self.assertEqual(s["targetRevision"], "v9.9.9", name)
                elif name.startswith("extras-"):   # pragma: no cover (none in BASE)
                    self.assertEqual(s["repoURL"], v["instance"]["repo"], name)
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
        for apps, glob in ((".", "*/app.yaml"), ("deploy/apps", "deploy/apps/*/app.yaml")):
            s2 = ok(values(instance={**BASE["instance"], "apps": apps}), "prod")["applications"]
            self.assertEqual(render._git_generator(s2["spec"]["generators"])["files"], [{"path": glob}], apps)
        src = s["spec"]["template"]["spec"]["source"]
        self.assertEqual((src["repoURL"], src["targetRevision"]), ("https://git.example/p.git", "v0.1.0"))
        patch = s["spec"]["templatePatch"]
        self.assertEqual(patch.count('repoURL: "https://git.example/i.git"'), 2)    # helm and kustomize components
        self.assertEqual(patch.count('targetRevision: "live"'), 2)
        self.assertIn('(dict "minutes" 60 "tzOffsetHours" 5.5)', patch)            # Asia/Kolkata
        self.assertNotIn("${", json.dumps(s))

    def test_applications_exclude(self):
        # clusters.<name>.applications.exclude: git generator files with exclude: true, on that cluster only
        v = values(instance={**BASE["instance"], "apps": "."})
        v["clusters"] = copy.deepcopy(v["clusters"])
        v["clusters"]["lab"]["applications"] = {"exclude": ["storefront", "sample-app"]}
        files = lambda c: render._git_generator(ok(v, c)["applications"]["spec"]["generators"])["files"]  # noqa: E731
        self.assertEqual(files("prod"), [{"path": "*/app.yaml"}])
        self.assertEqual(files("lab"), [{"path": "*/app.yaml"}, {"path": "storefront/app.yaml", "exclude": True},
                                        {"path": "sample-app/app.yaml", "exclude": True}])
        v["instance"]["apps"] = "deploy/apps"
        self.assertEqual(files("lab")[1], {"path": "deploy/apps/storefront/app.yaml", "exclude": True})
        # without the key (or with an empty list) the ApplicationSet is exactly what it was
        empty = copy.deepcopy(v)
        empty["clusters"]["lab"]["applications"] = {"exclude": []}
        bare = copy.deepcopy(v)
        del bare["clusters"]["lab"]["applications"]
        self.assertEqual(ok(empty, "lab")["applications"], ok(bare, "lab")["applications"])
        for bad in ({"exclude": ["Big"]}, {"exclude": "storefront"}, {"include": ["x"]}, {"exclude": ["a", "a"]}):
            b = copy.deepcopy(v)
            b["clusters"]["lab"]["applications"] = bad
            self.assertNotEqual(helm(b, "lab")[0], 0, bad)

    def test_environments_exclude(self):
        # clusters.<name>.environments.exclude / applications.environments.<app>.exclude: an if around each
        # environment of the ApplicationSet's environments list, on that cluster only
        def envs(o):   # the environments list's template
            return o["applications"]["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elementsYaml"]
        v = values()
        v["clusters"] = copy.deepcopy(v["clusters"])
        before = {c: ok(v, c)["applications"] for c in ("prod", "lab")}
        v["clusters"]["lab"]["environments"] = {"exclude": ["staging"]}
        v["clusters"]["lab"]["applications"] = {"environments": {"storefront": {"exclude": ["prod", "dev"]},
                                                                 "sample-app": {"exclude": ["dev"]}}}
        self.assertEqual(ok(v, "prod")["applications"], before["prod"])
        lab = envs(ok(v, "lab"))
        self.assertIn('{{- range .environments }}{{- if not (or (has .name (list "staging")) (has .name (get (dict '
                      '"sample-app" (list "dev") "storefront" (list "prod" "dev")) $.name | default list))) }}\n', lab)
        self.assertIn("{{- end }}{{- end }}\n", lab)
        self.assertEqual(lab.count("{{- if"), envs({"applications": before["lab"]}).count("{{- if") + 1)
        v["clusters"]["lab"]["applications"] = {"environments": {"storefront": {"exclude": ["prod"]}}}
        del v["clusters"]["lab"]["environments"]
        self.assertIn('{{- if not (or (has .name (get (dict "storefront" (list "prod")) $.name | default list))) }}',
                      envs(ok(v, "lab")))
        # without the keys (or with empty lists) the ApplicationSet is exactly what it was
        v["clusters"]["lab"]["environments"] = {"exclude": []}
        v["clusters"]["lab"]["applications"] = {"environments": {"storefront": {"exclude": []}}}
        self.assertEqual(ok(v, "lab")["applications"], before["lab"])
        for bad in ({"environments": {"exclude": ["Big"]}}, {"environments": {"exclude": "dev"}},
                    {"environments": {"only": ["dev"]}}, {"environments": {"exclude": ["dev", "dev"]}},
                    {"applications": {"environments": {"storefront": ["dev"]}}},
                    {"applications": {"environments": {"storefront": {"only": ["dev"]}}}},
                    {"applications": {"environments": {"Storefront": {"exclude": ["dev"]}}}}):
            b = copy.deepcopy(v)
            b["clusters"]["lab"].pop("environments")
            b["clusters"]["lab"].pop("applications")
            b["clusters"]["lab"].update(bad)
            self.assertNotEqual(helm(b, "lab")[0], 0, bad)

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
                (values(notify={"language": "de"}), "prod", "language"),
                (values(instance={**BASE["instance"], "apps": "../x"}), "prod", "apps"),
                (values(regression={"experiments": "/abs"}), "prod", "experiments"),
                (values(clusters={"prod": {"role": "prod", "extras": [{"name": "a"}]}}), "prod", "path"),
                (values(components={"grafana": {"enabled": False}}), "prod", "grafana"),
                (values(components={"pyrra": {"values": {"x": 1}}}), "prod", "values"),     # kustomize-sourced
                (values(applications={"namespaceLabels": {"a": "b c"}}), "prod", "namespaceLabels"),
                (values(applications={"namespaceLabels": {"a": 1}}), "prod", "namespaceLabels")):
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

    def test_existing_repo_instance(self):
        """platform/root/ci/existing-repo.yaml: applications at the root, extras (a kustomize directory, plain
        manifests) rendered from the instance."""
        if not os.path.isdir(os.path.join(ROOT, "examples")):
            self.skipTest("no examples")
        platform = os.path.join(CHART, "ci", "existing-repo.yaml")
        inst = render.example_instance(ROOT, platform)
        self.assertTrue(os.path.exists(os.path.join(inst, "storefront", "app.yaml")))
        self.assertTrue(os.path.exists(os.path.join(inst, "experiments", "api-errors.yaml")))
        self.assertFalse(os.path.exists(os.path.join(inst, "storefront", "experiments")))
        for cluster in ("laptop", "lab"):
            apps = {a["name"]: a for a in render.apps(ROOT, platform=platform, cluster=cluster)}
            self.assertEqual(apps[f"extras-{cluster}"]["root"], inst)
            self.assertEqual(apps[f"extras-{cluster}"]["path"], f"clusters/{cluster}")
            self.assertEqual(apps["storefront-prod"]["path"], "storefront/overlays/prod")
            self.assertEqual(apps["storefront-worker-dev"]["path"], "storefront/worker/chart")
            self.assertIn("storefront-slo-prod", apps)
            docs = [d for d in yaml.safe_load_all(render.render(apps[f"extras-{cluster}"])) if d]
            self.assertEqual([(d["kind"], d["metadata"]["name"]) for d in docs], [("Application", "platform-ui")])
            self.assertNotIn("platform-kyverno", apps)       # components.kyverno.enabled: false

    def test_applications_exclude_instance(self):
        """clusters.lab.applications.exclude: render.py leaves the application out of that cluster only, as Argo CD's
        git generator does."""
        if not os.path.isdir(os.path.join(ROOT, "examples")):
            self.skipTest("no examples")
        platform = os.path.join(CHART, "ci", "existing-repo.yaml")
        inst = render.example_instance(ROOT, platform)
        with open(platform) as f:
            v = yaml.safe_load(f)
        v["clusters"]["lab"]["applications"] = {"exclude": ["storefront"]}
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        pf = os.path.join(d, "platform.yaml")
        with open(pf, "w") as f:
            yaml.safe_dump(v, f)
        lab = {a["name"] for a in render.apps(ROOT, instance=inst, platform=pf, cluster="lab")}
        laptop = {a["name"] for a in render.apps(ROOT, instance=inst, platform=pf, cluster="laptop")}
        self.assertFalse([n for n in lab if n.startswith("storefront")], lab)
        self.assertIn("sample-app-prod", lab)
        self.assertTrue({"storefront-prod", "storefront-worker-dev", "storefront-slo-prod"} <= laptop)
        self.assertEqual(laptop, {a["name"] for a in render.apps(ROOT, platform=platform, cluster="laptop")})

    def test_environments_exclude_instance(self):
        """clusters.lab.environments.exclude / applications.environments.<app>.exclude: render.py leaves those
        environments' Applications out of that cluster only, as the ApplicationSet's environments list does."""
        if not os.path.isdir(os.path.join(ROOT, "examples")):
            self.skipTest("no examples")
        platform = os.path.join(CHART, "ci", "existing-repo.yaml")
        inst = render.example_instance(ROOT, platform)
        with open(platform) as f:
            v = yaml.safe_load(f)
        v["clusters"]["lab"]["environments"] = {"exclude": ["staging"]}
        v["clusters"]["lab"]["applications"] = {"environments": {"storefront": {"exclude": ["prod"]}}}
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        pf = os.path.join(d, "platform.yaml")
        with open(pf, "w") as f:
            yaml.safe_dump(v, f)
        lab = {a["name"] for a in render.apps(ROOT, instance=inst, platform=pf, cluster="lab")}
        laptop = {a["name"] for a in render.apps(ROOT, instance=inst, platform=pf, cluster="laptop")}
        everything = {a["name"] for a in render.apps(ROOT, platform=platform, cluster="lab")}
        gone = {n for n in everything if n.endswith(("-staging", "-prod")) and n.startswith("storefront")}
        self.assertTrue({"storefront-prod", "storefront-worker-staging", "storefront-slo-prod",
                         "storefront-traffic-staging"} <= gone, gone)
        self.assertEqual(lab, everything - gone)
        self.assertTrue({"storefront-dev", "sample-app-prod", "sample-app-dev"} <= lab)
        self.assertEqual(laptop, {a["name"] for a in render.apps(ROOT, platform=platform, cluster="laptop")})


if __name__ == "__main__":
    unittest.main()
