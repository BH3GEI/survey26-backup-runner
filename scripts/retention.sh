#!/usr/bin/env bash
# Retention: keep every snapshot from the last 7 days; older ones only the newest per ISO week.
# DISABLED unless repo variable RETENTION_ENABLED=true (owner: keep everything until results
# are final, i.e. at least until 2026-10-31). Prints what it would delete otherwise.
set -euo pipefail
SNAPSHOT_REPO=${SNAPSHOT_REPO:-$GITHUB_REPOSITORY}
CUT=$(date -u -d '7 days ago' +%Y%m%d)
gh release list --repo "$SNAPSHOT_REPO" --limit 1000 --json tagName --jq '.[].tagName|select(startswith("snapshot-"))' | sort -r | \
while read -r t; do
  d=${t#snapshot-}; d=${d:0:8}; [ "$d" \< "$CUT" ] || continue
  wk=$(date -u -d "$d" +%G-%V)
  if grep -qx "$wk" /tmp/kept-weeks 2>/dev/null; then
    if [ "${RETENTION_ENABLED:-false}" = "true" ]; then gh release delete "$t" --repo "$SNAPSHOT_REPO" --cleanup-tag -y; echo "deleted $t"
    else echo "retention disabled: would delete $t"; fi
  else echo "$wk" >> /tmp/kept-weeks; fi
done
