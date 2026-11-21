#!/usr/bin/env bash
# Everything renders as Argo CD renders it and passes kubeconform (CRDs such as Application / Probe via the CRDs
# catalog, Pyrra's from our pinned copy):
#   - the platform's charts (platform/slo, platform/traffic, platform/experiments) with their ci/ values: they are
#     tested even with no application present; synthetic traffic also through `k6 inspect` (the script must build
#     sane scenarios)
#   - the root chart (platform/root) with every ci/ platform.yaml, once per cluster in it (helm lint against
#     values.schema.json, kubeconform); its behaviour per role / runtime / options is unit-tested
#     (platform/root/test_root.py, in scripts/test.sh)
#   - an instance assembled from examples/ (scripts/render.py), for every cluster of platform/root/ci/platform.yaml:
#     every Application the root chart makes from the product or the instance, including those the ApplicationSet
#     derives from each app.yaml; traffic again through k6. With no examples, only the platform's.
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
for c in platform/slo platform/traffic platform/experiments; do
  for v in "$c"/ci/*.yaml; do
    echo "  $v"
    helm lint --quiet --strict "$c" -f "$v" > /dev/null
    helm template ci "$c" --namespace demo-dev -f "$v" > "$tmp/chart.yaml"
    kc < "$tmp/chart.yaml"
    if [ "$c" = platform/traffic ]; then k6_inspect "$tmp/chart.yaml"; fi
  done
done

# clusters <platform.yaml>: the names of its clusters, one per line
clusters() { "$PY" -I -c 'import sys, yaml; print("\n".join(yaml.safe_load(open(sys.argv[1]))["clusters"]))' "$1"; }

echo "• root chart (platform/root, ci/ platform.yaml files × their clusters)"
for v in platform/root/ci/*.yaml; do
  for cl in $(clusters "$v"); do
    echo "  $v cluster=$cl"
    helm lint --quiet --strict platform/root -f "$v" --set "cluster=$cl" > /dev/null
    helm template slo-platform platform/root --namespace argocd -f "$v" --set "cluster=$cl" | kc
  done
done

echo "• instance assembled from examples/ (scripts/render.py)"
: > "$tmp/apps"
for cl in $(clusters platform/root/ci/platform.yaml); do
  echo "  cluster $cl"
  "$PY" -I scripts/render.py --cluster "$cl" all "$tmp/$cl" > "$tmp/list-$cl"
  while read -r a path ns enforced; do
    if grep -q "^$a " "$tmp/apps"; then continue; fi   # the same Application on another cluster: rendered already
    echo "$a $path $ns $enforced" >> "$tmp/apps"
    cp "$tmp/$cl/$a.yaml" "$tmp/app-$a.yaml"
    echo "• $a"
    kc -skip CustomResourceDefinition < "$tmp/app-$a.yaml"   # no schema for CRDs themselves (platform components)
    if [ "$path" = platform/traffic ]; then k6_inspect "$tmp/app-$a.yaml"; fi
  done < "$tmp/list-$cl"
done
n=$(grep -c ' enforced$' "$tmp/apps" || true)
[ "$n" -gt 0 ] || echo "  (no app.yaml: only the platform's Applications)"

echo "• admission policies (optional platform/policies)"
$KY test platform/policies/tests
if [ "$n" -gt 0 ]; then
  # every application namespace carries slo-platform/app, so every rendered application must pass. Argo CD puts what
  # has no namespace into the destination namespace: so must we, or no selector matches.
  "$PY" -I - "$tmp" > "$tmp/values.yaml" <<'PY'
import os, sys, yaml
tmp = sys.argv[1]
CLUSTER = {"Namespace", "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition", "PersistentVolume", "StorageClass",
           "PriorityClass", "IngressClass", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
apps = [l.split() for l in open(os.path.join(tmp, "apps")) if l.strip()]
apps = [(a, ns) for a, _, ns, enforced in apps if enforced == "enforced"]
print("apiVersion: cli.kyverno.io/v1alpha1\nkind: Values\nnamespaceSelector:")
for ns in sorted({ns for _, ns in apps}):
    print(f"  - {{ name: {ns}, labels: {{ slo-platform/app: {ns.rsplit('-', 1)[0]} }} }}")
for a, ns in apps:
    with open(os.path.join(tmp, f"app-{a}.yaml")) as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    for d in docs:
        if d["kind"] not in CLUSTER:
            d["metadata"].setdefault("namespace", ns)
    with open(os.path.join(tmp, f"ns-{a}.yaml"), "w") as f:
        yaml.safe_dump_all(docs, f)
PY
  args=()
  while read -r a _ _ enforced; do
    if [ "$enforced" = enforced ]; then args+=(--resource "$tmp/ns-$a.yaml"); fi
  done < "$tmp/apps"
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
