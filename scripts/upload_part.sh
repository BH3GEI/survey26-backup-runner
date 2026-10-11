#!/usr/bin/env bash
# split --filter target: stdin = one encrypted part; write, upload (retried, append-only, verified),
# record "<sha256>  <asset name actually used>" in parts.sha256 (restore concatenates in that order).
set -euo pipefail
F=$1; cat > "$F"
HERE=$(cd "$(dirname "$0")" && pwd); SHA=$(sha256sum "$F" | cut -d' ' -f1)
for i in 1 2 3 4 5 6; do NAME=$(python3 "$HERE/put_asset.py" "$SNAPSHOT_REPO" "$TAG" "$F") && break; [ $i = 6 ] && exit 1; sleep $((i*20)); done
echo "$SHA  $NAME" >> "$RUNNER_TEMP/parts.sha256"
rm -f "$F"  # local scratch copy only; the release holds the verified bytes
