#!/usr/bin/env bash
# Nothing personal in the product: fail on addresses, hostnames and paths that belong in an instance's platform.yaml.
# Patterns: Tailscale's CGNAT range (100.64.0.0/10), private LAN ranges, the reference installation's host names.
set -euo pipefail
cd "$(dirname "$0")/.."
patterns=(
  '(^|[^0-9.])100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}'   # no \b: git grep -E is POSIX
  '(^|[^0-9.])192\.168\.[0-9]{1,3}\.[0-9]{1,3}'
  '(^|[^0-9.])10\.42\.0\.1([^0-9]|$)'
  'xiaoxin|Library/Application Support'
)
found=0
for p in "${patterns[@]}"; do
  if git grep -nIE "$p" -- . ':!scripts/check-residue.sh'; then found=1; fi
done
if [ "$found" = 1 ]; then echo "✗ personal values above belong in platform.yaml" >&2; exit 1; fi
echo "✓ no residue"
