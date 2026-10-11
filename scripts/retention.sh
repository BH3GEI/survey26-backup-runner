#!/usr/bin/env bash
# Retention REPORT ONLY. Owner's rule (2026-10-11): never delete any backup, anywhere. This step
# only lists which snapshots an "every snapshot of the last 7 days, older ones newest-per-ISO-week"
# policy WOULD drop, so space can be planned; it has no code path that deletes anything, whatever
# RETENTION_ENABLED says.
set -euo pipefail
SNAPSHOT_REPO=${SNAPSHOT_REPO:-$GITHUB_REPOSITORY}
CUT=$(date -u -d '7 days ago' +%Y%m%d)
gh release list --repo "$SNAPSHOT_REPO" --limit 1000 --json tagName --jq '.[].tagName|select(startswith("snapshot-"))' | sort -r | \
while read -r t; do
  d=${t#snapshot-}; d=${d:0:8}; [ "$d" \< "$CUT" ] || continue
  wk=$(date -u -d "$d" +%G-%V)
  if grep -qx "$wk" /tmp/kept-weeks 2>/dev/null; then echo "report only (never deleted): $t"
  else echo "$wk" >> /tmp/kept-weeks; fi
done
