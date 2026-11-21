#!/usr/bin/env python3
"""Alertmanager's configuration out of a rendering of the root chart (platform/root), for amtool.

  alertmanager-config.py <root chart rendering> <outdir>             → outdir/alertmanager.yaml, outdir/*.tmpl
  alertmanager-config.py --compare <root chart rendering> <kube-prometheus-stack rendering>
                                     → exit 1 unless the chart's Secret holds the same configuration and texts
  alertmanager-config.py --values <root chart rendering> <file>      → platform-monitoring's Helm values (for --compare)

The templates' path (/etc/alertmanager/config/*.tmpl in the cluster) is rewritten to outdir, so `amtool check-config`
loads the texts too. Prints whether the configuration mounts the receivers' Secret (secret) and its receivers.
"""
import base64
import os
import sys

import yaml

IN_CLUSTER = "/etc/alertmanager/config/*.tmpl"


def monitoring_values(rendering):
    with open(rendering) as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    app = next(d for d in docs if d["kind"] == "Application" and d["metadata"]["name"] == "platform-monitoring")
    return app["spec"]["source"]["helm"]["valuesObject"]


def from_root(rendering):
    am = monitoring_values(rendering)["alertmanager"]
    return am["config"], am.get("templateFiles") or {}, am["alertmanagerSpec"].get("secrets") or []


def from_kps(rendering):
    with open(rendering) as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    s = next(d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "alertmanager-kps-alertmanager")
    data = {k: base64.b64decode(v).decode() for k, v in s["data"].items()}
    am = next(d for d in docs if d["kind"] == "Alertmanager")
    return yaml.safe_load(data.pop("alertmanager.yaml")), data, am["spec"].get("secrets") or []


def write(config, templates, outdir):
    os.makedirs(outdir, exist_ok=True)
    if templates:
        assert config.get("templates") == [IN_CLUSTER], config.get("templates")
        config = dict(config, templates=[os.path.join(os.path.abspath(outdir), "*.tmpl")])
    with open(os.path.join(outdir, "alertmanager.yaml"), "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
    for name, text in templates.items():
        with open(os.path.join(outdir, name), "w") as f:
            f.write(text)


def main(argv):
    if argv[0] == "--values":
        with open(argv[2], "w") as f:
            yaml.safe_dump(monitoring_values(argv[1]), f, allow_unicode=True)
        return 0
    if argv[0] == "--compare":
        want, got = from_root(argv[1]), from_kps(argv[2])
        # kube-prometheus-stack merges its own default global (resolve_timeout) into the configuration
        got[0].setdefault("global", {}).pop("resolve_timeout", None)
        if not got[0]["global"]:
            del got[0]["global"]
        for what, a, b in zip(("configuration", "templates", "secrets"), want, got):
            if a != b:
                print(f"{what} differs:\n--- root chart\n{yaml.safe_dump(a, allow_unicode=True)}--- kube-prometheus-stack\n"
                      f"{yaml.safe_dump(b, allow_unicode=True)}")
                return 1
        return 0
    config, templates, secrets = from_root(argv[0])
    outdir = argv[-1]
    write(config, templates, outdir)
    print(f"secrets={','.join(secrets) or '-'} receivers={','.join(r['name'] for r in config['receivers'])} "
          f"templates={','.join(templates) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
