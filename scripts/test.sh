#!/usr/bin/env bash
# Every unit test of the repository: scripts/test_*.py, bin/test_*.py and platform/**/test_*.py, in parallel (each
# file is its own process; the tests use temporary directories, never the cluster). Needs python3 with PyYAML
# (apt install python3-yaml, pip install pyyaml, or: uv run --with pyyaml scripts/test.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -I -c 'import yaml' 2>/dev/null || { echo "✗ PyYAML is required (pip install pyyaml)" >&2; exit 1; }

tests=()
while IFS= read -r f; do tests+=("$f"); done < <(
  { ls scripts/test_*.py bin/test_*.py 2>/dev/null; find platform -name 'test_*.py' -type f 2>/dev/null; } | sort -u)
if [ "${#tests[@]}" -eq 0 ]; then echo "no tests found" >&2; exit 1; fi

logs=$(mktemp -d)
trap 'rm -rf "$logs"' EXIT
pids=()
for i in "${!tests[@]}"; do
  python3 -I -B "${tests[$i]}" >"$logs/$i.log" 2>&1 &
  pids+=("$!")
done
failed=0
for i in "${!tests[@]}"; do
  if wait "${pids[$i]}"; then
    echo "✓ ${tests[$i]}: $(grep -E '^Ran [0-9]+ tests?' "$logs/$i.log" | tail -1)"
  else
    failed=1
    echo "✗ ${tests[$i]}"
    sed 's/^/    /' "$logs/$i.log"
  fi
done
exit "$failed"
