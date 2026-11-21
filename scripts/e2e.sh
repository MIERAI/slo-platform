#!/usr/bin/env bash
# The template instantiated end to end on a throwaway kind cluster, with no GitHub token and no external repository:
#
#   1. `bin/slo-platform init` an instance with sample-app (cluster `e2e`: role lab, runtime kind)
#   2. bootstrap/tofu/kind creates the kind cluster slo-e2e (only the cluster: -target)
#   3. a git server inside that cluster (scripts/e2e/git-server.yaml) gets two bare repositories: product.git (this
#      checkout's working tree, as one commit on branch e2e — uncommitted changes included) and instance.git; the
#      instance's platform.yaml points product.repo / instance.repo at them (http://git.slo-e2e-git.svc.cluster.local)
#   4. bootstrap/tofu/kind again, completely: Argo CD + the root Application
#   5. wait until every Application is Synced + Healthy (the root, what it renders, what the ApplicationSet generates)
#   6. --experiment: sample-app-down through scripts/experiment.py, which must PASS
#
# The cluster and the temporary directory are always removed at the end (trap), unless --keep. On failure a diagnosis
# is printed and written to the diagnostics directory (non-healthy Applications, events, pods not ready).
#
#   scripts/e2e.sh [--experiment] [--keep] [--timeout MINUTES] [--diag DIR] [--results DIR]
#
# Needs: Docker (≈ 7 GB for the cluster), OpenTofu ≥ 1.10, kubectl, git, Python 3 with PyYAML; kind (optional: deletes
# the cluster, else docker rm). Never touches ~/.kube/config or another kind cluster: the kubeconfig is the instance's
# .secrets/e2e/kubeconfig, OpenTofu's state and working directory live in the temporary directory.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
CLUSTER=e2e                      # the key in platform.yaml; the kind cluster is slo-$CLUSTER
KIND_NAME=slo-$CLUSTER
GIT_URL=http://git.slo-e2e-git.svc.cluster.local
EXPERIMENT=$ROOT/examples/sample-app/experiments/sample-app-down.yaml

experiment=0 keep=0 timeout_min=30
diag=${E2E_DIAG:-$PWD/e2e-diagnostics} results=
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case $1 in
    --experiment) experiment=1 ;;
    --keep) keep=1 ;;
    --timeout) timeout_min=$2; shift ;;
    --diag) diag=$2; shift ;;
    --results) results=$2; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done
results=${results:-$diag/results}

say() { printf '\n\033[1m── %s\033[0m  (%s)\n' "$*" "$(date +%H:%M:%S)"; }
die() { echo "✗ $*" >&2; exit 1; }

for t in docker tofu kubectl git "$PY"; do command -v "$t" >/dev/null || die "$t not found (prerequisites: docs/quickstart.md)"; done
"$PY" -c 'import yaml' 2>/dev/null || die "PyYAML missing for $PY (pip install pyyaml, or apt install python3-yaml)"
docker info >/dev/null 2>&1 || die "Docker is not running"
cluster_containers() { docker ps -aq --filter "label=io.x-k8s.kind.cluster=$KIND_NAME"; }
[ -z "$(cluster_containers)" ] || die "a kind cluster $KIND_NAME exists already (left by --keep?): kind delete cluster --name $KIND_NAME"

work=$(mktemp -d "${TMPDIR:-/tmp}/slo-e2e.XXXXXX")
instance=$work/instance
export TF_VAR_instance_dir=$instance TF_VAR_cluster=$CLUSTER
export TF_DATA_DIR=$work/tofu      # .terraform of this run: a checkout's own bootstrap/tofu/kind/.terraform stays as it is
export KUBECONFIG=$instance/.secrets/$CLUSTER/kubeconfig
tofu_kind() { tofu -chdir="$ROOT/bootstrap/tofu/kind" "$@"; }
started=$(date +%s)

diagnose() {
  [ -s "$KUBECONFIG" ] || { echo "(no cluster to diagnose)"; return; }
  mkdir -p "$diag"
  echo "diagnosis → $diag"
  kubectl get nodes -o wide > "$diag/nodes.txt" 2>&1
  kubectl describe nodes > "$diag/nodes-describe.txt" 2>&1
  kubectl -n argocd get applications -o wide > "$diag/applications.txt" 2>&1
  kubectl -n argocd get applications -o yaml > "$diag/applications.yaml" 2>&1
  kubectl -n argocd get applicationsets -o yaml > "$diag/applicationsets.yaml" 2>&1
  kubectl get pods -A -o wide > "$diag/pods.txt" 2>&1
  kubectl get events -A --sort-by=.lastTimestamp > "$diag/events.txt" 2>&1
  kubectl -n argocd logs deploy/argocd-repo-server --tail=300 > "$diag/argocd-repo-server.log" 2>&1
  kubectl -n argocd logs statefulset/argocd-application-controller --tail=300 > "$diag/argocd-application-controller.log" 2>&1
  kubectl -n slo-e2e-git logs git --tail=100 > "$diag/git-server.log" 2>&1
  echo "── Applications not Synced+Healthy"
  "$PY" "$ROOT/scripts/e2e/wait.py" unhealthy || true
  echo "── pods not ready"
  local bad
  bad=$(kubectl get pods -A --no-headers 2>/dev/null | awk '{split($3, r, "/"); if ($4 != "Completed" && ($4 != "Running" || r[1] != r[2])) print $1 "/" $2}')
  if [ -z "$bad" ]; then echo "(none)"; fi
  for p in $bad; do
    kubectl get pod -n "${p%%/*}" "${p#*/}" --no-headers 2>/dev/null
    kubectl describe pod -n "${p%%/*}" "${p#*/}" >> "$diag/pods-not-ready.txt" 2>&1
    kubectl logs -n "${p%%/*}" "${p#*/}" --all-containers --tail=50 >> "$diag/pods-not-ready.log" 2>&1
  done
  echo "── recent warnings"
  kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp 2>/dev/null | tail -25
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  set +e
  if [ "$rc" != 0 ]; then say "FAILED (exit $rc) after $(( ($(date +%s) - started) / 60 )) min"; diagnose; fi
  if [ "$keep" = 1 ]; then
    say "kept: cluster $KIND_NAME, instance $instance"
    echo "export KUBECONFIG=$KUBECONFIG"
    echo "delete: kind delete cluster --name $KIND_NAME && rm -rf $work"
  else
    say "cleaning up"
    if command -v kind >/dev/null; then kind delete cluster --name "$KIND_NAME"
    else cluster_containers | xargs docker rm -f >/dev/null 2>&1; fi
    rm -rf "$work"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

say "instance: bin/slo-platform init (sample-app, cluster $CLUSTER: lab / kind)"
"$PY" "$ROOT/bin/slo-platform" init "$instance" --owner e2e --repo instance --cluster "$CLUSTER:lab:kind" \
  --example sample-app --version e2e --product-repo "$GIT_URL/product.git"
# the in-cluster instance repository; the daily regression 12 h away (it would run the same experiments concurrently)
"$PY" - "$instance/platform.yaml" "$GIT_URL/instance.git" "$(( (10#$(date -u +%H) + 12) % 24 ))" <<'PY'
import re, sys
path, repo, hour = sys.argv[1:]
text = open(path).read()
text, n1 = re.subn(r"https://github\.com/e2e/instance\.git", repo, text)
text, n2 = re.subn(r'schedule: "[^"]*"', f'schedule: "0 {hour} * * *"', text)
assert n1 == 1 and n2 == 1, (n1, n2)
open(path, "w").write(text)
PY
"$PY" "$ROOT/bin/slo-platform" check "$instance"

say "repositories: product (working tree of $ROOT) and instance"
mkdir -p "$work/git"
export GIT_AUTHOR_NAME=slo-e2e GIT_AUTHOR_EMAIL=e2e@example.invalid GIT_COMMITTER_NAME=slo-e2e GIT_COMMITTER_EMAIL=e2e@example.invalid
# the working tree as one parentless commit: a temporary index, so the checkout's index, refs and files stay as they
# are (and a shallow CI checkout pushes fine)
GIT_INDEX_FILE=$work/index git -C "$ROOT" read-tree HEAD
GIT_INDEX_FILE=$work/index git -C "$ROOT" add -A
tree=$(GIT_INDEX_FILE=$work/index git -C "$ROOT" write-tree)
commit=$(git -C "$ROOT" commit-tree -m "slo-platform e2e ($(git -C "$ROOT" rev-parse --short HEAD))" "$tree")
git init -q --bare "$work/git/product.git"
git -C "$ROOT" push -q "$work/git/product.git" "$commit:refs/heads/e2e"
git -C "$work/git/product.git" symbolic-ref HEAD refs/heads/e2e
git -C "$instance" init -q -b main
git -C "$instance" add -A
git -C "$instance" commit -q -m "slo-platform instance (e2e)"
git clone -q --bare "$instance" "$work/git/instance.git"
echo "product: branch e2e = $commit (HEAD $(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet HEAD -- || echo ', with uncommitted changes'))"

say "kind cluster $KIND_NAME (bootstrap/tofu/kind, cluster only)"
tofu_kind init -input=false >/dev/null
tofu_kind apply -input=false -auto-approve -target=kind_cluster.this

say "git server in the cluster"
kubectl apply -f "$ROOT/scripts/e2e/git-server.yaml"
kubectl -n slo-e2e-git wait --for=condition=Ready pod/git --timeout=300s
COPYFILE_DISABLE=1 tar -C "$work/git" -cf - product.git instance.git |
  kubectl -n slo-e2e-git exec -i git -- tar -C /srv/git -xof -
kubectl -n slo-e2e-git exec git -- git ls-remote http://localhost:8080/product.git
kubectl -n slo-e2e-git exec git -- git ls-remote http://localhost:8080/instance.git

say "bootstrap (bootstrap/tofu/kind: Argo CD + the root Application)"
tofu_kind apply -input=false -auto-approve
echo "bootstrap done after $(( ($(date +%s) - started) / 60 )) min"

say "waiting for every Application: Synced + Healthy (timeout ${timeout_min} min)"
"$PY" "$ROOT/scripts/e2e/wait.py" apps $((timeout_min * 60)) || die "the platform did not converge within ${timeout_min} min"
kubectl -n argocd get applications
echo "converged after $(( ($(date +%s) - started) / 60 )) min"

if [ "$experiment" = 1 ]; then
  say "experiment: $(basename "$EXPERIMENT")"
  "$PY" "$ROOT/scripts/e2e/wait.py" probe sample-app-dev 900 || die "no clean baseline for the experiment in sample-app-dev"
  mkdir -p "$results"
  # the instance's copy of the experiment (what the daily regression would run)
  if "$PY" "$ROOT/scripts/experiment.py" --kubeconfig "$KUBECONFIG" --results "$results" \
      "$instance/apps/sample-app/experiments/$(basename "$EXPERIMENT")"; then
    echo "experiment PASS (report: $results)"
  else
    rc=$?
    verdict=FAIL; [ "$rc" != 2 ] || verdict=INVALID
    die "experiment $verdict (report: $results)"
  fi
fi

say "PASS after $(( ($(date +%s) - started) / 60 )) min"
