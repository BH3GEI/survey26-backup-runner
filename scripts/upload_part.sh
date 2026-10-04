#!/usr/bin/env bash
# split --filter target: stdin = one encrypted part; write, checksum, upload (retried), delete.
set -euo pipefail
F=$1; cat > "$F"
(cd "$(dirname "$F")" && sha256sum "$(basename "$F")") >> "$RUNNER_TEMP/parts.sha256"
for i in 1 2 3 4 5 6; do gh release upload "$TAG" "$F" --repo "$SNAPSHOT_REPO" --clobber && break; [ $i = 6 ] && exit 1; sleep $((i*20)); done
rm -f "$F"
