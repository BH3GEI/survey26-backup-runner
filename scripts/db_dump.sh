#!/usr/bin/env bash
# Hourly encrypted FULL logical dump of the Supabase DB (public/private/auth/storage; vault and the
# rows of private.config excluded). pg_dump output is streamed straight into age; the only other
# consumer is `pg_restore -l` (table-of-contents check), whose output stays in RAM ($ARCHIVE_RAMDIR).
# Upload: Release db-YYYYMMDD in $ARCHIVE_REPO; one line appended to db/snapshots.jsonl there.
set -euo pipefail
: "${PGCONN:?}" "${ARCHIVE_AGE_RECIPIENTS:?}" "${ARCHIVE_ROOT:?}" "${ARCHIVE_REPO:?}" "${ARCHIVE_RAMDIR:?}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ); TAG="db-${STAMP:0:8}"
OUT="$RUNNER_TEMP/full-$STAMP.dump.age"; R="$ARCHIVE_RAMDIR/dump"; mkdir -p "$R"
args=(); for r in $(echo "$ARCHIVE_AGE_RECIPIENTS" | tr ',' ' '); do args+=(-r "$r"); done
mkfifo "$R/fifo"
age "${args[@]}" -o "$OUT" < "$R/fifo" & AGE_PID=$!
pg_dump "$PGCONN" -Fc -Z 6 -n public -n private -n auth -n storage --exclude-table-data=private.config \
  | tee "$R/fifo" | { pg_restore -l > "$R/toc"; cat > /dev/null; }
wait "$AGE_PID"
TABLES=$(grep -c ' TABLE DATA ' "$R/toc")
for t in "public observer_runs" "private observer_messages" "public teams" "auth users"; do
  grep -q " TABLE DATA $t " "$R/toc" || { echo "dump lacks $t" >&2; exit 1; }
done
[ "$TABLES" -ge 50 ] || { echo "only $TABLES tables" >&2; exit 1; }
rm -rf "$R"
COUNTS=$(psql "$PGCONN" -Atc "select json_build_object('teams',(select count(*) from public.teams),
  'observer_runs',(select count(*) from public.observer_runs),'observer_batches',(select count(*) from public.observer_batches),
  'observer_revisions',(select count(*) from public.observer_revisions),'observer_messages',(select count(*) from private.observer_messages),
  'observer_sessions',(select count(*) from private.observer_sessions),'storage_objects',(select count(*) from storage.objects))")
SHA=$(sha256sum "$OUT" | cut -d' ' -f1); BYTES=$(stat -c %s "$OUT")
gh release view "$TAG" --repo "$ARCHIVE_REPO" >/dev/null 2>&1 || \
  gh release create "$TAG" --repo "$ARCHIVE_REPO" --title "$TAG" --notes "Hourly encrypted full DB dumps (pg_dump -Fc | age). Restore: ops/archive/restore_db.sh"
for i in 1 2 3 4 5; do gh release upload "$TAG" "$OUT" --repo "$ARCHIVE_REPO" --clobber && break; [ $i = 5 ] && exit 1; sleep 20; done
rm -f "$OUT"
cd "$ARCHIVE_ROOT"; mkdir -p db
python3 - "$STAMP" "$TAG" "full-$STAMP.dump.age" "$SHA" "$BYTES" "$TABLES" "$COUNTS" <<'PY'
import json, sys
stamp, tag, asset, sha, size, tables, counts = sys.argv[1:]
with open("db/snapshots.jsonl", "a") as f:
    f.write(json.dumps({"ts": stamp, "release_tag": tag, "asset": asset, "sha256": sha, "bytes": int(size),
                        "tables": int(tables), "counts": json.loads(counts)}) + "\n")
PY
git add db/snapshots.jsonl; git commit -qm "DB snapshot full-$STAMP.dump.age"
for i in 1 2 3 4 5; do git pull -q --rebase origin main && git push -q origin HEAD:main && break; [ $i = 5 ] && exit 1; sleep 10; done
echo "snapshot $TAG/full-$STAMP.dump.age $BYTES bytes, $TABLES tables, $COUNTS"
