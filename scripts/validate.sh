#!/usr/bin/env bash
# Everything renders as Argo CD renders it and passes kubeconform (CRDs such as Application / Probe via the CRDs
# catalog, Pyrra's from our pinned copy):
#   - the platform's charts (platform/slo, platform/traffic) with their ci/ values: they are tested even with no
#     application present; synthetic traffic also through `k6 inspect` (the script must build sane scenarios)
#   - every Application the ApplicationSet derives from an app.yaml (scripts/render.py), traffic again through k6
#   - the Applications themselves (apps/, apps/optional/) and every platform kustomization
#   - the optional admission policies: their own tests, then every application must pass them (what Kyverno checks
#     at admission, but on the pull request)
# Then scripts/test.sh (unit tests), when present. Portable: macOS bash 3 and Linux.
set -euo pipefail
cd "$(dirname "$0")/.."
KC=${KUBECONFORM:-kubeconform}
KY=${KYVERNO:-kyverno}
K6=${K6:-k6}
PY=${PYTHON:-python3}
CRDS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
PINNED='platform/pyrra/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'   # from our pinned CRD (the catalog's is older)
kc() { $KC -strict -summary -schema-location default -schema-location "$PINNED" -schema-location "$CRDS" "$@" -; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# k6_inspect <rendered traffic manifests>: the script with the Deployment's env must yield the users scenario, a day of
# hourly stages and positive rates (a broken REQUESTS / day setting fails here, not in the cluster)
k6_inspect() {
  "$PY" -I - "$1" "$tmp/traffic.js" > "$tmp/k6.env" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
open(sys.argv[2], "w").write(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["traffic.js"])
for e in next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["env"]:
    print(f"{e['name']}={e['value']}")
PY
  local args=() line
  while IFS= read -r line; do args+=(-e "$line"); done < "$tmp/k6.env"
  $K6 inspect --execution-requirements "${args[@]}" "$tmp/traffic.js" | "$PY" -I -c '
import json, sys
o = json.load(sys.stdin); sc = o["scenarios"]; u = sc["users"]
assert u["executor"] == "ramping-arrival-rate" and len(u["stages"]) >= 24, u
assert all(s["target"] >= 1 for s in u["stages"]) and u["maxVUs"] >= u["preAllocatedVUs"], u
peak = max(s["target"] for s in u["stages"])
print("  k6: %d scenarios, start %s/min, peak %s/min" % (len(sc), u["startRate"], peak))'
}

echo "• platform charts (ci/ values)"
for c in platform/slo platform/traffic; do
  for v in "$c"/ci/*.yaml; do
    echo "  $v"
    helm lint --quiet --strict "$c" -f "$v" > /dev/null
    helm template ci "$c" --namespace demo-dev -f "$v" > "$tmp/chart.yaml"
    kc < "$tmp/chart.yaml"
    if [ "$c" = platform/traffic ]; then k6_inspect "$tmp/chart.yaml"; fi
  done
done

echo "• applications (app.yaml → ApplicationSet)"
"$PY" -I scripts/render.py list | awk '$2 !~ /^platform\// || $2 == "platform/slo" || $2 == "platform/traffic" {print $1, $2}' > "$tmp/apps"
n=0
while read -r a path; do
  echo "• $a"
  "$PY" -I scripts/render.py render "$a" > "$tmp/app-$a.yaml"
  kc < "$tmp/app-$a.yaml"
  if [ "$path" = platform/traffic ]; then k6_inspect "$tmp/app-$a.yaml"; fi
  n=$((n + 1))
done < "$tmp/apps"
[ "$n" -gt 0 ] || echo "  (no app.yaml: nothing to render)"

echo "• Applications (apps/, apps/optional/)"
for d in apps apps/optional; do
  echo "  $d"; kubectl kustomize "$d" | kc
done

echo "• platform components"
for p in platform/*/; do
  [ -f "$p/Chart.yaml" ] && continue   # charts: above
  echo "  $p"; kubectl kustomize "$p" | kc -skip CustomResourceDefinition   # no schema for CRDs themselves
done

echo "• admission policies (optional platform/policies)"
$KY test platform/policies/tests
if [ "$n" -gt 0 ]; then
  # every application namespace carries slo-platform/app, so every rendered application must pass
  "$PY" -I - > "$tmp/values.yaml" <<'PY'
import sys
sys.path.insert(0, "scripts")
import render
print("apiVersion: cli.kyverno.io/v1alpha1\nkind: Values\nnamespaceSelector:")
for ns in sorted({a["namespace"] for a in render.apps() if a["enforced"]}):
    print(f"  - {{ name: {ns}, labels: {{ slo-platform/app: {ns.rsplit('-', 1)[0]} }} }}")
PY
  # Argo CD puts what has no namespace into the destination namespace: so must we, or no selector matches
  "$PY" -I - "$tmp" <<'PY'
import os, sys, yaml
sys.path.insert(0, "scripts")
import render
CLUSTER = {"Namespace", "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition", "PersistentVolume", "StorageClass",
           "PriorityClass", "IngressClass", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
for a in render.apps():
    f = os.path.join(sys.argv[1], f"app-{a['name']}.yaml")
    if not os.path.exists(f):
        continue
    docs = [d for d in yaml.safe_load_all(open(f)) if d]
    for d in docs:
        if d["kind"] not in CLUSTER:
            d["metadata"].setdefault("namespace", a["namespace"])
    yaml.safe_dump_all(docs, open(os.path.join(sys.argv[1], f"ns-{a['name']}.yaml"), "w"))
PY
  args=()
  while read -r a _; do args+=(--resource "$tmp/ns-$a.yaml"); done < "$tmp/apps"
  out=$($KY apply platform/policies/workload-baseline.yaml --values-file "$tmp/values.yaml" "${args[@]}" 2>&1) || true
  echo "$out" | grep -vE '^\s*$'
  echo "$out" | grep -qE 'fail: 0, warn: 0, error: 0' || { echo "✗ policy violations (see above)"; exit 1; }
fi

echo "• experiment exporter"
"$PY" -I -m py_compile platform/experiments/exporter.py

if [ -x scripts/test.sh ]; then
  echo "• unit tests (scripts/test.sh)"
  scripts/test.sh
fi
echo "✓ valid"
