#!/usr/bin/env python3
"""Incremental, encrypted CI backup orchestrator for survey26-archive.

Runs inside the GitHub Actions workflow every ~15 minutes. Wraps export.py's
primitives (sql/paged_select/fetch_objects/mirror_*) with:

  - watermarking: tables with a reliable created_at/updated_at are fetched with
    `since=<last watermark>` so each run only pulls new rows (ops/archive/watermarks.json).
  - snapshot hashing: small/bounded tables with no reliable change-timestamp
    (teams, observer_projects, observer_materializations, observer_sessions -- the
    last one is exactly why this whole pipeline exists: `publication` has no
    updated_at and is nulled by the compaction job, so it is re-fetched in full
    every run and only written/committed when its content hash actually changed,
    i.e. zero-cost on unchanged runs but never misses a late-arriving publication)
    (ops/archive/snapshot_hashes.json).
  - encryption: export.py encrypts every object/code-archive file in place as soon
    as it is written (see export.encrypt_file / ARCHIVE_AGE_RECIPIENTS); this script
    does the same for the table dumps it writes directly.
  - buckets/code archives/run archives: delegated as-is to export.py's existing
    per-object manifests (already incremental, now also already encrypted).

Only ever holds the age PUBLIC key (ARCHIVE_AGE_RECIPIENTS). Never decrypts.
Commits only if something actually changed, so re-running is a no-op.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export as E  # noqa: E402

ROOT = E.ROOT
WATERMARKS_PATH = ROOT / "ops/archive/watermarks.json"
SNAPSHOT_HASHES_PATH = ROOT / "ops/archive/snapshot_hashes.json"
SECTION_SLEEP = 2.0

# Stop starting new work once this much wall-clock time has passed, well under the job's
# timeout-minutes, so a run that still has a huge backlog exits with a normal, successful
# `python3 backup.py` return code instead of being killed by the job timeout -- which GitHub
# Actions reports as "cancelled", and which (for good reason -- see backup.yml) only redispatches
# its successor after a short deliberate delay, not immediately. A run that stops itself early
# and succeeds redispatches instantly, so getting *this* number right matters more than any
# other single thing for keeping the chain alive on a backlog this size.
RUN_START = time.time()
TIME_BUDGET_SECONDS = float(os.environ.get("ARCHIVE_TIME_BUDGET_SECONDS", "1800"))


def time_up() -> bool:
    return time.time() - RUN_START > TIME_BUDGET_SECONDS

# table, columns, order_col, page_size, output dir name
DELTA_TABLES = [
    ("observer_revisions", "public.observer_revisions", E.REVISIONS_COLUMNS, "created_at", 200),
    ("observer_batches", "public.observer_batches", E.BATCHES_COLUMNS, "created_at", 200),
    ("observer_runs", "public.observer_runs", E.RUNS_COLUMNS, "created_at", 200),
    ("observer_messages", "private.observer_messages", E.MESSAGES_COLUMNS, "created_at", 500),
    ("observer_evidence", "public.observer_evidence", E.EVIDENCE_COLUMNS, "updated_at", 200),
    ("submissions", "public.submissions", E.SUBMISSIONS_COLUMNS, "created_at", 200),
    ("evaluations", "public.evaluations", E.EVALUATIONS_COLUMNS, "created_at", 200),
]

# table, columns, order_col (paged through from scratch every run; written only on hash change)
SNAPSHOT_TABLES = [
    ("teams", "public.teams", E.TEAMS_COLUMNS, "created_at"),
    ("observer_projects", "public.observer_projects", E.PROJECTS_COLUMNS, "created_at"),
    ("observer_materializations", "private.observer_materializations", E.MATERIALIZATIONS_COLUMNS, "revision_id"),
    ("observer_sessions", "private.observer_sessions", E.SESSIONS_COLUMNS, "run_id"),
]


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def run_timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def write_and_encrypt(rows: list[dict], dest_dir: Path, stamp: str) -> Path | None:
    if not rows:
        return None
    return E.write_jsonl(dest_dir / f"{stamp}.jsonl", rows)


def run_delta_tables(watermarks: dict, stamp: str, summary: list[str]) -> None:
    for key, table, columns, order_col, page_size in DELTA_TABLES:
        if time_up():
            print(f"-- time budget reached, skipping remaining table deltas starting at {key} --", flush=True)
            summary.append(f"{key}: SKIPPED (time budget) -- retried next run")
            break
        since = watermarks.get(key)
        print(f"-- {key}: fetching since={since!r} --", flush=True)
        try:
            rows = E.paged_select(table, columns, order_col=order_col, page_size=page_size, since=since)
            out = write_and_encrypt(rows, ROOT / "data" / key, stamp)
        except Exception as e:  # noqa: BLE001 - one flaky table must not sink the whole run or
            # re-fetch tables that already succeeded this run; watermark is simply left where
            # it was, so the next scheduled run retries this table's delta from the same point.
            line = f"{key}: FAILED - {e}"
            print("  " + line, flush=True)
            summary.append(line)
            save_json(WATERMARKS_PATH, watermarks)
            continue
        if rows:
            watermarks[key] = str(rows[-1][order_col])
            line = f"{key}: +{len(rows)} rows -> {out.relative_to(ROOT)}"
        else:
            line = f"{key}: no new rows (since={since})"
        print("  " + line, flush=True)
        summary.append(line)
        save_json(WATERMARKS_PATH, watermarks)
        time.sleep(SECTION_SLEEP)


def canonical_hash(rows: list[dict]) -> str:
    blob = json.dumps(rows, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def run_snapshot_tables(snap_hashes: dict, stamp: str, summary: list[str]) -> None:
    for key, table, columns, order_col in SNAPSHOT_TABLES:
        if time_up():
            print(f"-- time budget reached, skipping remaining snapshot tables starting at {key} --", flush=True)
            summary.append(f"{key}: SKIPPED (time budget) -- retried next run")
            break
        print(f"-- {key}: full paged fetch (hash-diffed) --", flush=True)
        try:
            rows = E.paged_select(table, columns, order_col=order_col, page_size=300, since=None)
        except Exception as e:  # noqa: BLE001 - see run_delta_tables; retried next scheduled run
            line = f"{key}: FAILED - {e}"
            print("  " + line, flush=True)
            summary.append(line)
            continue
        h = canonical_hash(rows)
        if snap_hashes.get(key) == h:
            line = f"{key}: unchanged ({len(rows)} rows, hash {h[:12]})"
        else:
            out = write_and_encrypt(rows, ROOT / "data" / key, stamp)
            snap_hashes[key] = h
            line = f"{key}: CHANGED, snapshot {len(rows)} rows -> {out.relative_to(ROOT) if out else 'EMPTY'}"
        print("  " + line, flush=True)
        summary.append(line)
        save_json(SNAPSHOT_HASHES_PATH, snap_hashes)
        time.sleep(SECTION_SLEEP)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def checkpoint(message: str) -> bool:
    """Commit+push whatever is on disk right now. Called after each phase (table batch,
    each individual bucket/mirror) so a run that's killed by the job timeout partway through
    a slow phase (e.g. thousands of runner-org GitHub API calls) still keeps everything it
    finished, instead of losing the whole run because the single commit-at-the-end never ran."""
    # Whole-repo add, not an explicit pathspec list: at the "tables" checkpoint none of the
    # object-mirror manifest files exist yet, and `git add -- <nonexistent pathspec>` fails
    # the entire command (stages nothing at all), which previously made this a silent no-op
    # followed by a crash in `git commit` once it found nothing staged. .gitignore already
    # restricts what's trackable (.age files + the small plaintext bookkeeping files only).
    r = git("add", "-A")
    if r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        raise RuntimeError("git add failed")
    status = git("status", "--porcelain")
    if not status.stdout.strip():
        return False
    r = git("commit", "-m", message)
    if r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        raise RuntimeError("git commit failed")
    for attempt in range(5):
        r = git("push", "origin", "HEAD")
        if r.returncode == 0:
            break
        print(r.stdout, r.stderr, file=sys.stderr)
        if attempt == 4:
            raise RuntimeError("git push failed")
        # Someone else moved origin/main (a concurrent run, or the maintainer pushing a fix)
        # since checkout -- rebase our one commit on top and retry, rather than aborting this
        # whole category and losing an otherwise-successful checkpoint.
        print(f"  push rejected, rebasing onto origin/main and retrying (attempt {attempt + 1}/5)", flush=True)
        git("fetch", "origin", "main")
        rb = git("rebase", "origin/main")
        if rb.returncode != 0:
            print(rb.stdout, rb.stderr, file=sys.stderr)
            raise RuntimeError("git rebase failed after a push conflict")
        time.sleep(3)
    print(f"  checkpoint committed+pushed: {message.splitlines()[0]}", flush=True)
    return True


def run_object_mirrors(summary: list[str]) -> bool:
    changed = False
    for name, fn in (
        ("unmaterialized repository revisions (contestant repos)", E.mirror_source_repos),
        ("storage objects (all buckets)", E.all_buckets),
        ("code archives (runner-org tarballs)", E.mirror_code_archives),
        ("run result archives", E.mirror_run_result_archives),
    ):
        if time_up():
            print(f"-- time budget reached, skipping remaining categories starting at {name} --", flush=True)
            summary.append(f"{name}: SKIPPED (time budget) -- retried next run")
            break
        print(f"--- {name} ---", flush=True)
        on_progress = lambda status: checkpoint(f"Automated encrypted backup: {name} (in progress)\n\n{status}")  # noqa: E731
        try:
            fn(on_progress, time_up)
            line = f"{name}: ok" if not time_up() else f"{name}: partial (time budget) -- resumed next run"
        except E.RateLimited:
            line = f"{name}: partial (GitHub API rate limit) -- resumed next run"
            print("  " + line, flush=True)
        except Exception as e:  # noqa: BLE001 - keep going; report partial failures, don't abort the whole run
            line = f"{name}: FAILED - {e}"
            print(f"  WARNING: {name} failed: {e}", file=sys.stderr)
        summary.append(line)
        changed = checkpoint(f"Automated encrypted backup: {name}\n\n{line}") or changed
        time.sleep(SECTION_SLEEP)
    return changed


def main() -> None:
    if not E._recipients():
        print("ARCHIVE_AGE_RECIPIENTS is not set; refusing to run (would leave plaintext)", file=sys.stderr)
        sys.exit(1)
    # Optional argv filter (e.g. `backup.py tables` or `backup.py objects`) for staged
    # testing; CI always runs with no args, i.e. everything.
    wanted = set(sys.argv[1:]) or {"tables", "objects"}
    stamp = run_timestamp()
    watermarks = load_json(WATERMARKS_PATH)
    snap_hashes = load_json(SNAPSHOT_HASHES_PATH)
    summary: list[str] = []
    changed = False

    if "tables" in wanted:
        run_delta_tables(watermarks, stamp, summary)
        run_snapshot_tables(snap_hashes, stamp, summary)
        save_json(WATERMARKS_PATH, watermarks)
        save_json(SNAPSHOT_HASHES_PATH, snap_hashes)
        changed = checkpoint("Automated encrypted backup: table deltas\n\n" + "\n".join(summary)) or changed
    if "objects" in wanted:
        changed = run_object_mirrors(summary) or changed

    print("\n".join(summary))
    failed = [line for line in summary if "FAILED" in line]
    gha_out = os.environ.get("GITHUB_OUTPUT")
    if gha_out:
        with open(gha_out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
    if failed:
        # Progress is already committed above; exiting non-zero makes the run red so the
        # monitor (backup-monitor.yml) and GitHub's own failure notifications see it, instead
        # of a category failing every run behind a green check mark.
        print("FAILED categories:\n" + "\n".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
