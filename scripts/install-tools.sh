#!/usr/bin/env bash
# Tools for scripts/validate.sh on CI (Linux x86_64 runners), pinned versions; PyYAML for app.yaml (scripts/appdesc.py).
# k6 matches the image of platform/traffic (its script is inspected with this binary); amtool matches the Alertmanager of
# kube-prometheus-stack in platform/root/templates/platform-monitoring.yaml (its configuration is checked with it).
set -euo pipefail
dest=${1:-/usr/local/bin}
curl -fsSL https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz | tar xz -C "$dest" kubeconform
curl -fsSL https://github.com/kyverno/kyverno/releases/download/v1.19.2/kyverno-cli_v1.19.2_linux_x86_64.tar.gz | tar xz -C "$dest" kyverno
curl -fsSL https://get.helm.sh/helm-v4.3.2-linux-amd64.tar.gz | tar xz -C "$dest" --strip-components=1 linux-amd64/helm
curl -fsSL https://github.com/grafana/k6/releases/download/v1.8.2/k6-v1.8.2-linux-amd64.tar.gz | tar xz -C "$dest" --strip-components=1 k6-v1.8.2-linux-amd64/k6
curl -fsSL https://github.com/prometheus/alertmanager/releases/download/v0.34.2/alertmanager-0.34.2.linux-amd64.tar.gz | tar xz -C "$dest" --strip-components=1 alertmanager-0.34.2.linux-amd64/amtool
python3 -c "import yaml" 2>/dev/null || apt-get install -y -qq python3-yaml >/dev/null
