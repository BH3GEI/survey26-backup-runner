#!/usr/bin/env python3
"""Idempotent, incremental export of AGENTIC-OBSERVER26 / survey26 contestant artifacts out of Supabase
into this archive repo. Safe to re-run: skips anything already written for an unchanged sha256/updated_at.

Usage:
    cd survey26-archive
    set -a && . /path/to/supabase.env && set +a
    HTTPS_PROXY=... python3.12 ops/archive/export.py [section ...]

Sections (default: all): teams, revisions, materializations, batches, runs, sessions, messages,
submissions_table, staging_bucket, results_bucket, submissions_bucket, code_archives, run_archives

Intentionally NOT exported: public.scenarios / phases (would leak hidden-card seeds/manifests —
competitive-integrity risk, and not part of the requested scope), private.observer_providers and
any key/ciphertext column (contestants' model API keys).

Reads only (SUPABASE_SERVICE_ROLE_KEY for storage, SUPABASE_ACCESS_TOKEN for the SQL management-API
endpoint). Never touches private.observer_providers or any key/ciphertext/secret column. Queries the
DB gently: small LIMIT/OFFSET pages with a short sleep between, since the project is on a fragile
Micro tier.
"""
from __future__ import annotations

import hashlib
import http.client as http_client
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ARCHIVE_ROOT: checkout of the private archive repo (the public runner keeps its scripts apart).
ROOT = Path(os.environ.get("ARCHIVE_ROOT") or Path(__file__).resolve().parents[2]).resolve()
# Scratch space for anything that must exist as a file (git clones): RAM, never the runner disk.
RAMDIR = os.environ.get("ARCHIVE_RAMDIR") or None
UA = "survey26-archive-export"
PAGE_SLEEP = 1.0

# --------------------------------------------------------------------- encryption
#
# CI only ever holds the age PUBLIC key (ARCHIVE_AGE_RECIPIENTS, one or more age1...
# recipients, space/comma separated). The private key never leaves the maintainer's
# machine, so every file that lands in git must be encrypted before `git add` and the
# plaintext removed immediately after. backup.py is the only caller that needs this;
# export.py itself is unaffected when ARCHIVE_AGE_RECIPIENTS is unset (ad-hoc/local use).

def _recipients() -> list[str]:
    raw = os.environ.get("ARCHIVE_AGE_RECIPIENTS", "")
    return [r for r in re.split(r"[\s,]+", raw.strip()) if r]


def seal(path: Path, data: bytes) -> Path:
    """Encrypt in memory and write ONLY the ciphertext (path + ".age"). Plaintext never touches
    the disk: this is the only way any content is written by the backup pipeline."""
    recipients = _recipients()
    if not recipients:
        raise RuntimeError("ARCHIVE_AGE_RECIPIENTS is not set; refusing to write")
    out = path.with_name(path.name + ".age")
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["age"]
    for r in recipients:
        args += ["-r", r]
    subprocess.run(args + ["-o", str(out)], input=data, check=True)
    return out


def encrypt_file(path: Path) -> Path:
    """Encrypt path in place with age, remove the plaintext, return the new .age path."""
    recipients = _recipients()
    if not recipients:
        raise RuntimeError("ARCHIVE_AGE_RECIPIENTS is not set; refusing to leave plaintext for commit")
    out = path.with_name(path.name + ".age")
    args = ["age"]
    for r in recipients:
        args += ["-r", r]
    args += ["-o", str(out), str(path)]
    subprocess.run(args, check=True)
    path.unlink()
    return out


# --------------------------------------------------------------------- GitHub Release assets
#
# Large/unbounded-size binaries (bucket objects, code tarballs, per-run workflow_result.json
# dumps that range from a few KB to 80+ MB) are uploaded as Release assets instead of committed
# to git: release assets don't count against Git LFS storage/bandwidth quota (free tier: 1 GB
# of each), unlike committing the same bytes through git-lfs. Only small, bounded-size files
# (per-table JSONL deltas, decisions.csv, per-revision metadata.json) are committed directly.

RELEASE_REPO = os.environ.get("ARCHIVE_REPO") or "gosimfoundation/survey26-archive"


def release_tag(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d', time.gmtime())}"


# GitHub caps a single release at 1000 assets ("file_count limited to 1000 assets per
# release"). Busy days blew through that for archive-results / archive-runresults and every
# upload after the 1000th failed until UTC midnight, so a day's category is sharded:
# <prefix>-YYYYMMDD, then <prefix>-YYYYMMDD-2, -3, ... each holding at most RELEASE_ASSET_CAP.
RELEASE_ASSET_CAP = 950
_release_counts: dict[str, int] = {}
# Release/issue writes to the private archive use ARCHIVE_WRITE_TOKEN (narrow write token);
# GH_TOKEN stays the read token for the runner-org repositories.
WRITE_ENV = {**os.environ, "GH_TOKEN": os.environ["ARCHIVE_WRITE_TOKEN"]} if os.environ.get("ARCHIVE_WRITE_TOKEN") else None


def _asset_count(tag: str) -> int | None:
    """Asset count of an existing release, None if it doesn't exist."""
    owner, name = RELEASE_REPO.split("/", 1)
    r = subprocess.run(
        ["gh", "api", "graphql", "-f", "query=query($o:String!,$n:String!,$t:String!){repository(owner:$o,name:$n)"
         "{release(tagName:$t){releaseAssets{totalCount}}}}", "-f", f"o={owner}", "-f", f"n={name}", "-f", f"t={tag}",
         "--jq", ".data.repository.release.releaseAssets.totalCount // -1"],
        capture_output=True, text=True, timeout=60, env=WRITE_ENV,
    )
    if r.returncode != 0:
        raise RuntimeError(f"release lookup failed for {tag}: {r.stderr[:200]}")
    n = int(r.stdout.strip() or -1)
    return None if n < 0 else n


def _ensure_release(prefix: str) -> str:
    """Return a release tag for today's prefix that still has room, creating it if needed."""
    base = release_tag(prefix)
    for shard in range(1, 100):
        tag = base if shard == 1 else f"{base}-{shard}"
        if tag not in _release_counts:
            n = _asset_count(tag)
            if n is None:
                subprocess.run(
                    ["gh", "release", "create", tag, "--repo", RELEASE_REPO, "--title", tag,
                     "--notes", "Encrypted archive assets for this day/category. Age-encrypted; "
                                "decrypt with ops/archive/restore.sh and the maintainer's private key."],
                    check=True, capture_output=True, env=WRITE_ENV,
                )
                n = 0
            _release_counts[tag] = n
        if _release_counts[tag] < RELEASE_ASSET_CAP:
            return tag
    raise RuntimeError(f"no release shard with room for {base}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class RateLimited(RuntimeError):
    """GitHub API rate limit hit (the token is a person's 5000/h budget shared with other tools):
    stop the category for this run instead of failing every remaining item."""


def release_upload(tag_prefix: str, local_path: Path, asset_name: str) -> dict:
    """Upload local_path as asset_name to today's release for tag_prefix, delete the local
    file, and return the manifest entry to record (never committed to git -- the manifest's
    sha256 + release_tag + asset name are enough to locate and verify it later)."""
    # GitHub drops non-ASCII characters from asset names on upload (a Chinese team slug became
    # "__<revision>__code@..."), so store an ASCII-safe name and record exactly that.
    asset_name = re.sub(r"[^A-Za-z0-9._@+-]", "_", asset_name.encode("ascii", "ignore").decode()) or "asset"
    tag = _ensure_release(tag_prefix)
    digest = sha256_file(local_path)
    upload_path = local_path if local_path.name == asset_name else local_path.with_name(asset_name)
    if upload_path != local_path:
        local_path.rename(upload_path)
    try:
        r = subprocess.run(
            ["gh", "release", "upload", tag, str(upload_path), "--repo", RELEASE_REPO, "--clobber"],
            capture_output=True, timeout=180, env=WRITE_ENV,
        )
        if r.returncode != 0:
            err = r.stderr.decode()[:300]
            if "rate limit" in err.lower():
                raise RateLimited(err)
            if "file_count limited" in err:
                _release_counts[tag] = RELEASE_ASSET_CAP  # full after all; next call moves to the next shard
            raise RuntimeError(f"gh release upload failed: {err}")
        _release_counts[tag] += 1
    finally:
        upload_path.unlink(missing_ok=True)
    return {"status": "ok", "release_tag": tag, "asset": asset_name, "sha256": digest}


def _done_ok(entry) -> bool:
    """True for a manifest entry that's permanently settled: either migrated to a release
    (dict) or confirmed gone for good (missing/bad-ref). A bare "ok" string is a pre-release-
    migration entry (the object used to be committed straight to git) that still needs
    reprocessing; "fetch-failed" is transient and always worth retrying."""
    return isinstance(entry, dict) or entry in ("missing", "bad-ref")


def flatten_key(key: str) -> str:
    return key.replace("/", "__")


class _Buffered:
    """A fully-read response: same .status/.read() surface as HTTPResponse, but the
    retry loop below can safely retry a body read that dies partway through."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = io.BytesIO(body)

    def read(self, *a):
        return self._body.read(*a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _open(req, timeout: int, max_attempts: int = 8, backoff: float = 10):
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _Buffered(resp.status, resp.read())
        except urllib.error.HTTPError:
            raise  # a real response from the server; never worth retrying
        except (urllib.error.URLError, http_client.IncompleteRead, ConnectionError, TimeoutError):
            # Transient network/proxy-tunnel failures (seen both on a flaky local proxy and
            # occasionally against the Micro-tier project itself under load).
            if attempt == max_attempts - 1:
                raise
            time.sleep(backoff)


def sql(query: str):
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{os.environ['SUPABASE_PROJECT_REF']}/database/query",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": "Bearer " + os.environ["SUPABASE_ACCESS_TOKEN"], "Content-Type": "application/json", "User-Agent": UA})
    for attempt in range(4):
        try:
            with _open(req, 120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            # 429/5xx and a spurious 404 "Cannot GET" (seen intermittently on the same POST that
            # succeeds a second later) are transient; anything else is a real error.
            if attempt < 3 and (e.code == 429 or e.code >= 500 or "Cannot GET" in body):
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"SQL API HTTP {e.code}: {body}") from None


def http(method: str, url: str, data: bytes | None = None, *, key: str | None = "SUPABASE_SERVICE_ROLE_KEY",
         content_type: str = "application/octet-stream") -> tuple[int, bytes]:
    headers = {"apikey": os.environ["SUPABASE_ANON_KEY"], "User-Agent": UA}
    if key:
        headers["Authorization"] = "Bearer " + os.environ[key]
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(os.environ["SUPABASE_URL"] + url, data=data, method=method, headers=headers)
    try:
        # Lower budget than sql(): this is a per-object fetch inside a loop over potentially
        # thousands of objects, and a stuck one is cheap to just retry next run (fetch_objects'
        # manifest only marks a key "ok" on success) -- unlike a management-API page, nothing
        # here is worth burning 8x120s of the job's timeout on.
        with _open(req, 60, max_attempts=3, backoff=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def obj_path(bucket: str, name: str) -> str:
    return f"/storage/v1/object/{bucket}/" + urllib.parse.quote(name, safe="/")


def q(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    """Sealed (age) JSONL; returns the .age path. No plaintext file is ever written."""
    data = "".join(json.dumps(row, default=str, ensure_ascii=False) + "\n" for row in rows).encode()
    out = seal(path, data)
    print(f"  wrote {out.relative_to(ROOT)} ({len(rows)} rows)")
    return out


def paged_select(table: str, columns: str, order_col: str = "created_at", page_size: int = 200,
                  since: str | None = None) -> list[dict]:
    """since, if given, is an exclusive lower bound on order_col (e.g. last run's watermark)."""
    col_names = {c.strip().split()[-1] for c in columns.split(",")}
    if order_col not in col_names:
        raise ValueError(f"paged_select: order_col {order_col!r} must be included in columns to page on")
    rows: list[dict] = []
    last = since
    while True:
        where = f"where {order_col} > {q(last)}" if last is not None else ""
        chunk = sql(f"select {columns} from {table} {where} order by {order_col} limit {page_size}")
        if not chunk:
            break
        rows.extend(chunk)
        last = chunk[-1][order_col]
        if len(chunk) < page_size:
            break
        time.sleep(PAGE_SLEEP)
    return rows


# --------------------------------------------------------------------- tables
#
# Column lists are pulled out as constants so backup.py (the incremental/encrypted CI
# orchestrator) can reuse the exact same projections with paged_select(..., since=...)
# instead of duplicating column lists. The export_* functions below are the original
# full/ad-hoc exporters (still handy for a one-off local run) and are unchanged in behaviour.

TEAMS_COLUMNS = "id, name, slug, leader_id, max_size, is_locked, is_hidden, created_at"
REVISIONS_COLUMNS = (
    "id, project_id, source_kind, source_location, source_digest, source_commit, repository, status, "
    "manifest, adapter_files, approval_digest, public_test, explanation, error, approved_at, created_at, archived_at"
)
PROJECTS_COLUMNS = "id, team_id, owner_id, title, created_at"
EVIDENCE_COLUMNS = "revision_id, notes, code_url, updated_at"
MATERIALIZATIONS_COLUMNS = "revision_id, archive_ref, digest"
BATCHES_COLUMNS = "id, team_id, user_id, phase_id, revision_id, purpose, mode, status, score, created_at, finished_at, quota_refunded"
RUNS_COLUMNS = (
    "id, batch_id, scenario_id, status, error, score, result_path, decisions_digest, score_summary, "
    "created_at, started_at, finished_at, score_check"
)
SESSIONS_COLUMNS = (
    "run_id, expires_at, deadline_at, ready_at, publication, next_sequence, token_limit, call_limit, "
    "concurrency_limit, tokens_used, tokens_reserved, calls_used, calls_active"
)
MESSAGES_COLUMNS = "run_id, sequence, observation, response, committed, created_at"
SUBMISSIONS_COLUMNS = (
    "id, team_id, user_id, phase_id, scenario_id, kind, title, notes, storage_path, original_filename, sha256, "
    "status, score, science_score, completion, uniformity, metrics, error, is_excluded, created_at, started_at, "
    "finished_at, base_science, program_bonus, request_reward, penalty_total, completed_tiles, required_missing, "
    "flexible_shortfall, termination_reason, coverage_bonus, coverage_evenness"
)
EVALUATIONS_COLUMNS = (
    "id, submission_id, scenario_id, status, score, science_score, completion, uniformity, report_path, "
    "decisions_path, log_path, summary, error, runtime_seconds, created_at, finished_at, base_science, "
    "program_bonus, request_reward, penalty_total, completed_tiles, required_missing, flexible_shortfall, "
    "termination_reason, accounted_wallclock_seconds, replay_path, workflow_path, coverage_bonus, coverage_evenness"
)


def export_teams():
    print("== teams ==")
    rows = sql(f"select {TEAMS_COLUMNS} from public.teams order by created_at")
    write_jsonl(ROOT / "data/teams.jsonl", rows)


def export_revisions():
    print("== observer_revisions + observer_projects + observer_evidence ==")
    rows = paged_select("public.observer_revisions", REVISIONS_COLUMNS)
    write_jsonl(ROOT / "data/observer_revisions.jsonl", rows)
    write_jsonl(ROOT / "data/observer_projects.jsonl",
                sql(f"select {PROJECTS_COLUMNS} from public.observer_projects order by created_at"))
    write_jsonl(ROOT / "data/observer_evidence.jsonl",
                sql(f"select {EVIDENCE_COLUMNS} from public.observer_evidence order by updated_at"))


def export_materializations():
    print("== observer_materializations (archive_ref only, no secrets) ==")
    rows = sql(f"select {MATERIALIZATIONS_COLUMNS} from private.observer_materializations order by revision_id")
    write_jsonl(ROOT / "data/observer_materializations.jsonl", rows)


def export_batches():
    print("== observer_batches ==")
    rows = paged_select("public.observer_batches", BATCHES_COLUMNS)
    write_jsonl(ROOT / "data/observer_batches.jsonl", rows)


def export_runs():
    print("== observer_runs ==")
    rows = paged_select("public.observer_runs", RUNS_COLUMNS)
    write_jsonl(ROOT / "data/observer_runs.jsonl", rows)
    return rows


def export_sessions():
    print("== observer_sessions (metadata; publication already cleared by compaction for most) ==")
    rows = sql(f"select {SESSIONS_COLUMNS} from private.observer_sessions order by run_id")
    write_jsonl(ROOT / "data/observer_sessions.jsonl", rows)


def export_messages():
    print("== observer_messages (per-step trace; NOTE most/all already blanked by the compact cron job) ==")
    rows = paged_select("private.observer_messages", MESSAGES_COLUMNS, order_col="created_at", page_size=500)
    write_jsonl(ROOT / "data/observer_messages.jsonl", rows)


def export_submissions_table():
    print("== submissions + evaluations (old CSV-era scoring) ==")
    rows = paged_select("public.submissions", SUBMISSIONS_COLUMNS, order_col="created_at", page_size=200)
    write_jsonl(ROOT / "data/submissions.jsonl", rows)
    evals = paged_select("public.evaluations", EVALUATIONS_COLUMNS, order_col="created_at", page_size=200)
    write_jsonl(ROOT / "data/evaluations.jsonl", evals)


# --------------------------------------------------------------------- storage buckets
#
# Deliberately NOT a recursive bucket.list() crawl: with ~260-700 UUID subfolders per bucket that
# takes one sequential HTTP round-trip per folder (minutes, through the proxy) and hammers the
# fragile Micro-tier project for no benefit. Instead we download exactly the object keys named by
# DB rows we already fetched (storage_path / report_path / decisions_path / log_path / replay_path /
# workflow_path / observer-staging's <project_id>/<revision_id>/source.zip convention). Anything not
# referenced by a DB row is out of scope for this pass (noted as a phase-2 gap-check in the README).

def load_manifest(manifest_path: Path) -> dict:
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return {}


def save_manifest(manifest_path: Path, done: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(done, f)


def fetch_objects(bucket: str, dest: Path, manifest_path: Path, keys: list[str], on_progress=None,
                  release_tag_prefix: str | None = None, should_stop=None):
    """on_progress, if given, is called every ~25 items with a short status string -- backup.py
    uses it to commit+push what's on disk so far, since a single bucket can have thousands of
    objects and take several dispatches' worth of job-timeout to finish; without a mid-loop
    checkpoint, everything downloaded since the last full-category commit is thrown away when
    the job is cancelled for exceeding its time limit.

    should_stop, if given, is checked at the same cadence and breaks the loop (after saving
    the manifest) rather than letting the job run until the hard timeout kills it -- a run that
    stops itself and exits normally reports success and immediately redispatches its successor;
    one killed by the timeout reports "cancelled" and only redispatches after a guarded delay.

    release_tag_prefix routes every downloaded object through a GitHub Release instead of git
    (see the "GitHub Release assets" section above) -- always set for bucket objects, since
    they're arbitrary-sized contestant files, not bounded bookkeeping data."""
    print(f"== fetching {len(keys)} referenced objects from bucket {bucket} -> {dest.relative_to(ROOT)} ==", flush=True)
    done = load_manifest(manifest_path)
    new_count = missing_count = error_count = 0
    todo = [k for k in keys if not _done_ok(done.get(k))]
    for i, name in enumerate(todo):
        try:
            status, body = http("GET", obj_path(bucket, name))
        except Exception as e:  # noqa: BLE001 - one unreachable object must not sink the whole
            # bucket; it's simply retried next run since it was never marked done.
            error_count += 1
            print(f"  ERROR {bucket}/{name}: {e}", flush=True)
            continue
        # Storage returns a plain 404 for some keys but a 400 with a NoSuchKey/not_found body
        # for others (seen for expired observer-staging zips, which temporary_upload_lifecycle
        # already deleted by design) -- both mean "nothing to back up here", not a real failure.
        if status == 404 or (status == 400 and b"NoSuchKey" in body):
            done[name] = "missing"
            missing_count += 1
            continue
        if status != 200:
            print(f"  DOWNLOAD FAIL {bucket}/{name}: {status} {body[:150]}", flush=True)
            continue
        local = seal(dest / name, body)
        if release_tag_prefix:
            asset_name = f"{bucket}__{flatten_key(name)}" + (".age" if local.name.endswith(".age") else "")
            done[name] = release_upload(release_tag_prefix, local, asset_name)
        else:
            done[name] = "ok"
        new_count += 1
        if (i + 1) % 25 == 0:
            save_manifest(manifest_path, done)
            print(f"  ... {i + 1}/{len(todo)} processed ({new_count} new, {missing_count} missing, {error_count} errors)", flush=True)
            if on_progress:
                on_progress(f"{bucket}: {i + 1}/{len(todo)} objects")
            if should_stop and should_stop():
                print(f"  stopping early at {i + 1}/{len(todo)} (time budget) -- resumed next run", flush=True)
                break
            time.sleep(1.0)
    save_manifest(manifest_path, done)
    print(f"  downloaded {new_count} new objects, {missing_count} missing, {error_count} errors, {len(done)} tracked total", flush=True)


def staging_bucket(on_progress=None, should_stop=None):
    rows = sql(
        "select rv.id as revision_id, rv.project_id from public.observer_revisions rv "
        "where rv.source_kind = 'zip' order by rv.created_at"
    )
    keys = [f"{r['project_id']}/{r['revision_id']}/source.zip" for r in rows]
    fetch_objects("observer-staging", ROOT / "buckets/observer-staging",
                  ROOT / "ops/archive/.manifest-staging.json", keys, on_progress,
                  release_tag_prefix="archive-staging", should_stop=should_stop)


def results_bucket(on_progress=None, should_stop=None):
    # created_at must be selected: paged_select's cursor reads it off of each row.
    rows = paged_select(
        "public.evaluations",
        "report_path, decisions_path, log_path, replay_path, workflow_path, created_at",
        order_col="created_at", page_size=200,
    )
    keys: list[str] = []
    for r in rows:
        for col in ("report_path", "decisions_path", "log_path", "replay_path", "workflow_path"):
            if r.get(col):
                keys.append(r[col])
    fetch_objects("results", ROOT / "buckets/results", ROOT / "ops/archive/.manifest-results.json", keys, on_progress,
                  release_tag_prefix="archive-results", should_stop=should_stop)


def submissions_bucket(on_progress=None, should_stop=None):
    # created_at must be selected: paged_select's cursor reads it off of each row.
    rows = paged_select("public.submissions", "storage_path, created_at", order_col="created_at", page_size=200)
    keys = [r["storage_path"] for r in rows if r.get("storage_path")]
    fetch_objects("submissions", ROOT / "buckets/submissions", ROOT / "ops/archive/.manifest-submissions.json", keys, on_progress,
                  release_tag_prefix="archive-submissions", should_stop=should_stop)


def all_buckets(on_progress=None, should_stop=None):
    """Every object in every bucket, enumerated from storage.objects (one cheap SQL query, not
    an HTTP listing crawl). Supersedes the old referenced-keys-only staging/results/submissions
    passes, which missed avatars, scenarios, observer-scenarios (hidden-card bundles -- encrypted
    like everything else) and any object no DB row happened to point at, and which looked for
    observer-staging keys under a naming scheme that matched none of the live objects.

    Manifest .manifest-objects.json is keyed "<bucket>/<name>" and stores the object's eTag, so
    an object overwritten in place is re-archived; entries already uploaded by the old
    per-bucket manifests are adopted as-is (no re-download)."""
    rows = sql(
        "select bucket_id, name, coalesce(metadata->>'eTag', updated_at::text) as etag "
        "from storage.objects where name is not null order by created_at"
    )
    manifest_path = ROOT / "ops/archive/.manifest-objects.json"
    done = load_manifest(manifest_path)
    legacy = {
        "observer-staging": load_manifest(ROOT / "ops/archive/.manifest-staging.json"),
        "results": load_manifest(ROOT / "ops/archive/.manifest-results.json"),
        "submissions": load_manifest(ROOT / "ops/archive/.manifest-submissions.json"),
    }
    todo = []
    for r in rows:
        k = f"{r['bucket_id']}/{r['name']}"
        e = done.get(k)
        if isinstance(e, dict) and e.get("etag") == r["etag"]:
            continue
        old = legacy.get(r["bucket_id"], {}).get(r["name"])
        if e is None and isinstance(old, dict):
            done[k] = {**old, "etag": r["etag"]}
            continue
        todo.append(r)
    print(f"== storage: {len(rows)} objects in storage.objects, {len(todo)} to archive ==", flush=True)
    new = errors = 0
    for i, r in enumerate(todo):
        if should_stop and should_stop():
            print(f"  stopping early at {i}/{len(todo)} (time budget) -- resumed next run", flush=True)
            break
        bucket, name = r["bucket_id"], r["name"]
        try:
            status, body = http("GET", obj_path(bucket, name))
        except Exception as e:  # noqa: BLE001 - retried next run (not marked done)
            errors += 1
            print(f"  ERROR {bucket}/{name}: {e}", flush=True)
            continue
        if status == 404 or (status == 400 and b"NoSuchKey" in body):
            continue  # deleted between the query and the fetch; next run won't list it
        if status != 200:
            errors += 1
            print(f"  DOWNLOAD FAIL {bucket}/{name}: {status} {body[:150]}", flush=True)
            continue
        local = seal(ROOT / "buckets" / bucket / name, body)
        asset = f"{bucket}__{flatten_key(name)}.age"
        try:
            done[f"{bucket}/{name}"] = {**release_upload("archive-objects", local, asset), "etag": r["etag"]}
            new += 1
        except RateLimited:
            save_manifest(manifest_path, done)
            raise
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  UPLOAD FAIL {bucket}/{name}: {e}", flush=True)
        if (i + 1) % 25 == 0:
            save_manifest(manifest_path, done)
            if on_progress:
                on_progress(f"storage objects: {i + 1}/{len(todo)}")
    save_manifest(manifest_path, done)
    print(f"  archived {new} objects, {errors} errors, {len(done)} tracked", flush=True)
    if errors and not new:
        raise RuntimeError(f"storage: {errors} errors and no progress")


# --------------------------------------------------------------------- GitHub-side mirrors
#
# archive_ref / result_path look like "github:<org>/<repo>@<sha>". Two different kinds of commit
# live in the same per-participant repo: a code materialization (agent source snapshot) and, for
# each evaluated run, a result commit containing only decisions.csv + workflow_result.json (NOT the
# raw observation/response trace - that lives solely in private.observer_messages, which the compact
# cron job has already blanked for every run as of this export - see README "Known data loss").

ARCHIVE_REF_RE = re.compile(r"^github:([^/]+)/([^@]+)@([0-9a-f]{7,40})$")


def parse_archive_ref(ref: str):
    m = ARCHIVE_REF_RE.match(ref)
    if not m:
        return None
    return {"org": m.group(1), "repo": m.group(2), "sha": m.group(3)}


def gh_raw(org: str, repo: str, path: str, sha: str) -> bytes | None:
    try:
        r = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw",
             f"repos/{org}/{repo}/contents/{path}?ref={sha}"],
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def gh_tarball(org: str, repo: str, sha: str) -> bytes | None:
    try:
        r = subprocess.run(["gh", "api", f"repos/{org}/{repo}/tarball/{sha}"], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def mirror_code_archives(on_progress=None, should_stop=None):
    print("== mirroring agent code snapshots (observer_materializations.archive_ref) ==", flush=True)
    rows = sql(
        "select m.revision_id, m.archive_ref, m.digest, rv.status, rv.created_at, rv.archived_at, "
        "t.slug as team_slug, t.name as team_name "
        "from private.observer_materializations m "
        "join public.observer_revisions rv on rv.id = m.revision_id "
        "join public.observer_projects p on p.id = rv.project_id "
        "join public.teams t on t.id = p.team_id "
        "order by rv.created_at"
    )
    manifest_path = ROOT / "ops/archive/.manifest-code-archives.json"
    done = load_manifest(manifest_path)
    new_count = fail_count = 0
    for i, row in enumerate(rows):
        if should_stop and should_stop():
            print(f"  stopping early at {i}/{len(rows)} (time budget) -- resumed next run", flush=True)
            break
        rev_id = row["revision_id"]
        ref = parse_archive_ref(row["archive_ref"] or "")
        team_slug = row["team_slug"] or "unknown-team"
        team_dir = ROOT / "teams" / team_slug / rev_id
        meta_path = team_dir / "metadata.json"
        meta_done_path = meta_path.with_name(meta_path.name + ".age") if _recipients() else meta_path
        if _done_ok(done.get(rev_id)) and meta_done_path.exists():
            continue
        meta = {k: row[k] for k in ("revision_id", "archive_ref", "digest", "status", "created_at", "archived_at",
                                     "team_slug", "team_name")}
        if ref is None:
            done[rev_id] = "bad-ref"
            fail_count += 1
        else:
            tb = gh_tarball(ref["org"], ref["repo"], ref["sha"])
            if tb is None:
                done[rev_id] = "fetch-failed"
                fail_count += 1
            else:
                # The tarball goes to a Release asset (code snapshots are unbounded size,
                # unlike metadata.json below); never committed to git.
                team_dir.mkdir(parents=True, exist_ok=True)
                tb_path = seal(team_dir / f"code@{ref['sha'][:12]}.tar.gz", tb)
                asset_name = f"{team_slug}__{rev_id}__code@{ref['sha'][:12]}.tar.gz" + (
                    ".age" if tb_path.name.endswith(".age") else "")
                done[rev_id] = release_upload("archive-code", tb_path, asset_name)
                new_count += 1
        team_dir.mkdir(parents=True, exist_ok=True)
        seal(meta_path, json.dumps(meta, indent=2, default=str).encode())
        if (i + 1) % 20 == 0:
            save_manifest(manifest_path, done)
            print(f"  ... {i + 1}/{len(rows)} revisions processed ({new_count} new, {fail_count} failed)", flush=True)
            if on_progress:
                on_progress(f"code archives: {i + 1}/{len(rows)} revisions")
            if should_stop and should_stop():
                print(f"  stopping early at {i + 1}/{len(rows)} (time budget) -- resumed next run", flush=True)
                break
            time.sleep(1.0)
    save_manifest(manifest_path, done)
    print(f"  mirrored {new_count} code archives, {fail_count} failed/missing, {len(rows)} revisions total", flush=True)


def mirror_run_result_archives(on_progress=None, should_stop=None):
    print("== mirroring per-run result archives (decisions.csv + workflow_result.json) ==", flush=True)
    rows = sql(
        "select id as run_id, result_path, status, finished_at from public.observer_runs "
        "where result_path like 'github:%' order by finished_at"
    )
    manifest_path = ROOT / "ops/archive/.manifest-run-archives.json"
    done = load_manifest(manifest_path)
    missing: list[dict] = []
    new_count = 0
    budget = int(os.environ.get("ARCHIVE_MAX_RUNS_PER_PASS", "250"))  # ~3 API calls each
    for i, row in enumerate(rows):
        if should_stop and should_stop():
            print(f"  stopping early at {i}/{len(rows)} (time budget) -- resumed next run", flush=True)
            break
        if new_count >= budget:
            print(f"  {budget} runs this pass (API budget) -- rest resumed next run", flush=True)
            break
        run_id = row["run_id"]
        if _done_ok(done.get(run_id)):
            continue
        ref = parse_archive_ref(row["result_path"])
        run_dir = ROOT / "runs" / run_id
        if ref is None:
            missing.append({"run_id": run_id, "reason": "unparseable result_path", "result_path": row["result_path"]})
            done[run_id] = "bad-ref"
            continue
        # decisions.csv is consistently a few KB -- committed straight to git like any other
        # small bookkeeping file. workflow_result.json ranges from a few KB to 80+ MB (it's the
        # full result dump, not a summary), so -- like bucket objects and code tarballs -- it
        # goes to a Release asset instead, never committed.
        entry: dict = {}
        ok_any = False
        for fname in ("decisions.csv", "workflow_result.json"):
            content = gh_raw(ref["org"], ref["repo"], fname, ref["sha"])
            if content is None:
                entry[fname] = "fetch-failed"
                continue
            run_dir.mkdir(parents=True, exist_ok=True)
            fpath = seal(run_dir / fname, content)
            if fname == "workflow_result.json":
                asset_name = f"{run_id}__{fname}" + (".age" if fpath.name.endswith(".age") else "")
                entry[fname] = release_upload("archive-runresults", fpath, asset_name)
            else:
                entry[fname] = "ok"
            ok_any = True
        if ok_any and all(v != "fetch-failed" for v in entry.values()):
            done[run_id] = entry
        elif ok_any:
            done[run_id] = "fetch-failed"  # partial: retried next run instead of being marked done
            missing.append({"run_id": run_id, "reason": "partial github fetch", "result_path": row["result_path"]})
            new_count += 1
        else:
            done[run_id] = "fetch-failed"
            missing.append({"run_id": run_id, "reason": "github fetch failed", "result_path": row["result_path"]})
        if (i + 1) % 25 == 0:
            save_manifest(manifest_path, done)
            print(f"  ... {i + 1}/{len(rows)} runs processed ({new_count} new, {len(missing)} missing so far)", flush=True)
            if on_progress:
                on_progress(f"run archives: {i + 1}/{len(rows)} runs")
            if should_stop and should_stop():
                print(f"  stopping early at {i + 1}/{len(rows)} (time budget) -- resumed next run", flush=True)
                break
            time.sleep(1.0)
    save_manifest(manifest_path, done)
    if missing:
        missing_path = ROOT / f"data/missing_run_archives/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl"
        write_jsonl(missing_path, missing)
    print(f"  mirrored {new_count} run archives, {len(missing)} missing/failed out of {len(rows)} github-tagged runs", flush=True)


def mirror_source_repos(on_progress=None, should_stop=None):
    """Repository-kind revisions that were never materialized into a runner-org repo (failed /
    queued / withdrawn before materialization) have no archive_ref and often no source_commit --
    only the contestant's repo URL. Mirror that repo (git bundle of all refs) once per revision,
    recording the HEAD sha and fetch time, so every submitted code version is preserved even if
    the contestant later deletes or rewrites it. Unreachable (deleted/private) repos are recorded
    as such with the time of the attempt. Manifest: .manifest-source-repos.json (by revision id)."""
    import shutil
    import tempfile
    from datetime import datetime, timezone
    rows = sql(
        "select rv.id, rv.source_location from public.observer_revisions rv "
        "left join private.observer_materializations m on m.revision_id = rv.id "
        "where rv.source_kind = 'repository' and m.revision_id is null order by rv.created_at"
    )
    manifest_path = ROOT / "ops/archive/.manifest-source-repos.json"
    done = load_manifest(manifest_path)
    cache: dict[str, tuple] = {}
    for row in rows:
        if should_stop and should_stop():
            break
        rev = row["id"]
        if isinstance(done.get(rev), dict):
            continue
        url = re.sub(r"//[^/@]*@", "//", (row["source_location"] or "").strip())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if url not in cache:
            tmp = Path(tempfile.mkdtemp(dir=RAMDIR))
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            r = subprocess.run(["git", "clone", "-q", "--mirror", url, str(tmp / "m.git")],
                               capture_output=True, text=True, env=env, timeout=600)
            if r.returncode != 0 or not url.startswith("https://"):
                cache[url] = (None, None, tmp)
            else:
                head = subprocess.run(["git", "-C", str(tmp / "m.git"), "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip() or None
                b = tmp / "repo.bundle"
                ok = subprocess.run(["git", "-C", str(tmp / "m.git"), "bundle", "create", str(b), "--all"],
                                    capture_output=True).returncode == 0
                cache[url] = (b if ok else None, head, tmp)
        bundle, head, tmp = cache[url]
        if bundle is None:
            done[rev] = {"status": "unreachable", "url": url, "fetched_at": now}
            print(f"  {rev}: {url} unreachable (deleted/private/empty)", flush=True)
            continue
        enc = seal(ROOT / "srcrepos" / f"{rev}.bundle", bundle.read_bytes())
        entry = release_upload("archive-srcrepos", enc, f"{rev}__source.bundle.age")
        done[rev] = {**entry, "url": url, "head": head, "fetched_at": now}
        print(f"  {rev}: {url} @ {head}", flush=True)
        save_manifest(manifest_path, done)
    for _, _, tmp in cache.values():
        shutil.rmtree(tmp, ignore_errors=True)
    save_manifest(manifest_path, done)
    if on_progress:
        on_progress(f"source repos: {len(done)} revisions tracked")


SECTIONS = {
    "teams": export_teams,
    "revisions": export_revisions,
    "materializations": export_materializations,
    "batches": export_batches,
    "runs": export_runs,
    "sessions": export_sessions,
    "messages": export_messages,
    "submissions_table": export_submissions_table,
    "staging_bucket": staging_bucket,
    "results_bucket": results_bucket,
    "submissions_bucket": submissions_bucket,
    "all_buckets": all_buckets,
    "code_archives": mirror_code_archives,
    "source_repos": mirror_source_repos,
    "run_archives": mirror_run_result_archives,
}

if __name__ == "__main__":
    wanted = sys.argv[1:] or list(SECTIONS.keys())
    for name in wanted:
        if name not in SECTIONS:
            print(f"unknown section {name}, choices: {list(SECTIONS)}", file=sys.stderr)
            sys.exit(1)
    for name in wanted:
        SECTIONS[name]()
