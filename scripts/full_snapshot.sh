#!/usr/bin/env bash
# snapshot_stream.py | zstd | age | split (<=1900 MiB) -> Release snapshot-<UTC> in $SNAPSHOT_REPO.
set -euo pipefail
: "${SNAPSHOT_REPO:?}" "${ARCHIVE_AGE_RECIPIENTS:?}"
HERE=$(cd "$(dirname "$0")" && pwd)
export TAG="snapshot-$(date -u +%Y%m%dT%H%M%SZ)" SNAPSHOT_SUMMARY="$ARCHIVE_RAMDIR/summary.json"
gh release create "$TAG" --repo "$SNAPSHOT_REPO" --title "$TAG" --notes "Encrypted full snapshot (in progress)"
args=(); for r in $(echo "$ARCHIVE_AGE_RECIPIENTS" | tr ',' ' '); do args+=(-r "$r"); done
: > "$RUNNER_TEMP/parts.sha256"
GH_TOKEN="$RUNNER_READ_TOKEN" python3 "$HERE/snapshot_stream.py" | zstd -q -T0 -6 | age "${args[@]}" | \
  split -b 1900M -d -a 3 --filter="$HERE/upload_part.sh \"\$FILE\"" - "$RUNNER_TEMP/$TAG.tar.zst.age.part"
gh release upload "$TAG" "$RUNNER_TEMP/parts.sha256" --repo "$SNAPSHOT_REPO" --clobber
gh release edit "$TAG" --repo "$SNAPSHOT_REPO" --notes "Encrypted full snapshot. Summary: $(cat "$SNAPSHOT_SUMMARY")"
echo "uploaded $TAG: $(wc -l < "$RUNNER_TEMP/parts.sha256") part(s) $(cat "$SNAPSHOT_SUMMARY")"
