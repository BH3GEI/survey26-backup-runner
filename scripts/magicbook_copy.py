#!/usr/bin/env python3
"""Cloud-side copy of the Survey26 backups onto MagicBook (runs in GitHub Actions, no Mac involved).

What it copies:
  1. every release of the incremental archive (ARCHIVE_REPO: archive-*, db-*, runner-logs-* ...)
     -> BASE/gh/<archive repo name>/<tag>/<asset>
  2. one full snapshot per ISO week from SNAPSHOT_REPO -> BASE/full-snapshot/<tag>/<asset>
     The week's snapshot is the one already (partly) on MagicBook, else the week's first complete one.

How (owner's rules: append-only, never delete, never overwrite):
  - For each asset the runner asks the GitHub API for its short-lived signed URL and feeds those
    URLs to curl.exe on MagicBook over ssh STDIN (`curl.exe -K -`): no token and no URL is ever
    written to MagicBook's disk, so there is nothing to clean up.
  - curl writes <name>.<RID>.dl; the file is renamed to <name> only after its size and SHA-256
    match GitHub's digest, with File.Move, which refuses to replace an existing file. A temp file
    that fails verification is renamed to <name>.<RID>.bad (kept, reported), never deleted.
  - An asset counts as done when <name> exists on MagicBook with the right size (only verified
    files are ever given their final name). Anything already there with a different size is left
    untouched and reported.
  - Before each batch: MagicBook must keep FLOOR bytes free. A weekly snapshot that does not fit is
    NOT skipped: its release is re-verified on GitHub (every part present, uploaded, sha256 matches
    parts.sha256), it is reported as PENDING, an issue "MagicBook 空间不足" is opened/updated in
    ALERT_REPO for BH3GEI, the run exits non-zero, and every later run retries it.

Env: ARCHIVE_REPO, SNAPSHOT_REPO, ARCHIVE_TOKEN (read archive), SNAPSHOT_TOKEN (read snapshot repo,
     write issues there = ALERT_REPO), MB_SSH (ssh host alias configured by the workflow),
     MB_BASE, TIME_BUDGET_MIN.
"""
from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import os
import subprocess
import sys
import time
import uuid

ARCHIVE_REPO = os.environ.get("ARCHIVE_REPO", "gosimfoundation/survey26-archive")
SNAPSHOT_REPO = os.environ.get("SNAPSHOT_REPO", "BH3GEI/survey26-backup")
ALERT_REPO = os.environ.get("ALERT_REPO", SNAPSHOT_REPO)
TOKENS = {ARCHIVE_REPO: os.environ.get("ARCHIVE_TOKEN", ""), SNAPSHOT_REPO: os.environ.get("SNAPSHOT_TOKEN", "")}
BASE = os.environ.get("MB_BASE", "C:/Users/yao/survey26-backup-20261010")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=30",
       os.environ.get("MB_SSH", "magicbook")]
FLOOR = float(os.environ.get("MB_FLOOR_GB", "5")) * 1e9
FIRST_WEEK = os.environ.get("MB_FIRST_WEEK", "2026-W41")   # weekly snapshots from this ISO week on
MAX_FILES, MAX_BYTES = 120, 6e9
API_GAP = 3600 / 1500            # <= 1500 signed-URL lookups per hour; backs off below 2500 left (the token is shared with site ops, patrol and other jobs)
RID = uuid.uuid4().hex[:8]
START = time.time()
BUDGET = float(os.environ.get("TIME_BUDGET_MIN", "320")) * 60
SUMMARY: list[str] = []
PROBLEMS: list[str] = []


def log(m: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%H:%M:%S} {m}", flush=True)


def time_up() -> bool:
    return time.time() - START > BUDGET


def ps(script: str, timeout: int = 1800, stdin: str | None = None) -> list[str]:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    for attempt in range(5):   # the office link drops a connection now and then (exit 255)
        r = subprocess.run(SSH + [f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"],
                           input=stdin, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 255:
            break
        time.sleep(15 * (attempt + 1))
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError(f"MagicBook command failed (exit {r.returncode}): {r.stderr.strip()[:200]}")
    return [line for line in r.stdout.replace("\r", "").split("\n") if line and not line.startswith(("#<", "<Objs"))]


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def gh(*args: str, repo: str, check: bool = True) -> str:
    for attempt in range(6):   # GitHub answers 502/504 now and then on these big releases
        r = subprocess.run(["gh", *args], capture_output=True, text=True, env={**os.environ, "GH_TOKEN": TOKENS[repo]})
        if r.returncode == 0 or not any(c in r.stderr for c in ("HTTP 5", "timeout", "EOF", "connection")):
            break
        time.sleep(20 * (attempt + 1))
    if check and r.returncode:
        raise RuntimeError(f"gh {args[:3]}: {r.stderr.strip()[:300]}")
    return r.stdout


def gh_lines(path: str, jq: str, repo: str) -> list[dict]:
    out = gh("api", "--paginate", path, "--jq", jq, repo=repo)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def release_tags(repo: str) -> list[str]:
    """All release tags (GraphQL: the REST list embeds every asset and times out on these releases)."""
    owner, name = repo.split("/")
    out = gh("api", "graphql", "--paginate", "-f", "query=query($o:String!,$n:String!,$endCursor:String){repository(owner:$o,name:$n)"
             "{releases(first:100,after:$endCursor){nodes{tagName} pageInfo{hasNextPage endCursor}}}}",
             "-f", f"o={owner}", "-f", f"n={name}", "--jq", ".data.repository.releases.nodes[].tagName", repo=repo)
    return [t for t in out.splitlines() if t.strip()]


def release_assets(repo: str, tag: str) -> list[dict]:
    owner, name = repo.split("/")   # GraphQL: the REST tag lookup embeds up to 1000 assets and times out
    rid = gh("api", "graphql", "-f", "query=query($o:String!,$n:String!,$t:String!){repository(owner:$o,name:$n)"
             "{release(tagName:$t){databaseId}}}", "-f", f"o={owner}", "-f", f"n={name}", "-f", f"t={tag}",
             "--jq", ".data.repository.release.databaseId", repo=repo).strip()
    return gh_lines(f"repos/{repo}/releases/{rid}/assets?per_page=100", ".[] | {id, name, size, digest, state}", repo)


_last = [0.0]


def signed_url(repo: str, asset_id: int) -> str | None:
    for attempt in range(5):
        time.sleep(max(0.0, _last[0] + API_GAP - time.time()))
        _last[0] = time.time()
        try:
            c = http.client.HTTPSConnection("api.github.com", timeout=60)
            c.request("GET", f"/repos/{repo}/releases/assets/{asset_id}",
                      headers={"Authorization": f"Bearer {TOKENS[repo]}", "Accept": "application/octet-stream",
                               "User-Agent": "survey26-backup", "X-GitHub-Api-Version": "2022-11-28"})
            r = c.getresponse()
            loc, left, reset = r.getheader("Location"), r.getheader("X-RateLimit-Remaining"), r.getheader("X-RateLimit-Reset")
            c.close()
            if left is not None and int(left) < 2500:   # leave the shared token's budget to the backups
                wait = max(60, int(reset or 0) - time.time() + 30)
                if time.time() + wait - START > BUDGET:
                    return None
                log(f"  rate limit low ({left} left); pausing {wait / 60:.0f} min")
                time.sleep(wait)
            if r.status in (301, 302, 307) and loc:
                return loc
            log(f"  asset {asset_id}: HTTP {r.status}")
        except Exception as x:  # noqa: BLE001
            log(f"  asset {asset_id}: {type(x).__name__}")
        time.sleep(10 * (attempt + 1))
    return None


def free_bytes() -> int:
    for _ in range(3):
        out = ps("(Get-PSDrive C).Free", 120)
        if out and out[-1].strip().isdigit():
            return int(out[-1])
        time.sleep(30)
    return 0


def remote_listing(rdir: str) -> dict[str, int]:
    out = ps(f"$d = {q(rdir)}; if (Test-Path -LiteralPath $d) {{ Get-ChildItem -LiteralPath $d -File | "
             f"ForEach-Object {{ $_.Name + [char]9 + $_.Length }} }}", 900)
    return {n: int(s) for n, s in (line.rsplit("\t", 1) for line in out if "\t" in line)}


def remote_hashes(rdir: str, names: list[str]) -> dict[str, str]:
    got: dict[str, str] = {}
    for i in range(0, len(names), 40):
        arr = ",".join(q(n) for n in names[i:i + 40])
        out = ps(f"$d = {q(rdir)}; foreach ($n in @({arr})) {{ $p = Join-Path $d $n; if (Test-Path -LiteralPath $p) "
                 f"{{ $n + [char]9 + (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLower() }} }}", 3600)
        got.update(line.rsplit("\t", 1) for line in out if "\t" in line)
    return got


def move_no_replace(rdir: str, pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Rename src -> dst inside rdir with File.Move (throws instead of replacing)."""
    res: dict[str, str] = {}
    for i in range(0, len(pairs), 40):
        arr = ",".join(f"@({q(s)},{q(t)})" for s, t in pairs[i:i + 40])
        out = ps(f"$d = {q(rdir)}; foreach ($p in @({arr})) {{ try {{ [System.IO.File]::Move((Join-Path $d $p[0]), "
                 f"(Join-Path $d $p[1])); 'ok' + [char]9 + $p[1] }} catch {{ 'fail' + [char]9 + $p[1] }} }}", 600)
        res.update((n, s) for s, n in (line.split("\t", 1) for line in out if "\t" in line))
    return res


def batches(items: list[dict]):
    b, size = [], 0
    for a in items:
        if b and (len(b) >= MAX_FILES or size + a["size"] > MAX_BYTES):
            yield b, size
            b, size = [], 0
        b.append(a)
        size += a["size"]
    if b:
        yield b, size


def copy_assets(repo: str, rdir: str, assets: list[dict], label: str) -> tuple[int, int, bool]:
    """Copy the not-yet-present assets. Returns (copied, still_missing, stopped_for_space)."""
    long_ = [a["name"] for a in assets if len(rdir) + len(a["name"]) + 14 > 250]
    if long_:
        PROBLEMS.append(f"{label}: {len(long_)} asset(s) not copied, Windows path too long")
    have = remote_listing(rdir)
    conflict = [a["name"] for a in assets if a["name"] in have and have[a["name"]] != a["size"]]
    if conflict:
        PROBLEMS.append(f"{label}: {len(conflict)} file(s) already on MagicBook with a different size; left untouched")
    todo = [a for a in assets if a["name"] not in have and a["name"] not in long_]
    if not todo:
        return 0, 0, False
    ps(f"New-Item -ItemType Directory -Force -Path {q(rdir)} | Out-Null", 120)
    copied = 0
    for _attempt in range(2):
        failed = []
        for b, size in batches(todo):
            if time_up():
                return copied, len(todo) - copied, False
            free = free_bytes()
            if free - size < FLOOR:
                log(f"{label}: MagicBook free {free / 1e9:.1f} GB, batch needs {size / 1e9:.2f} GB -> stop")
                return copied, len(todo) - copied, True
            lines = ["fail", "silent", "show-error", "retry = 3", "retry-delay = 5"]
            for a in b:
                u = signed_url(repo, a["id"])
                if not u:
                    failed.append(a)
                    continue
                lines += [f'url = "{u}"', f'output = "{rdir}/{a["name"]}.{RID}.dl"']
            b = [a for a in b if a not in failed]
            if not b:
                continue
            t0 = time.time()
            try:   # URLs travel over ssh stdin only; nothing with a URL or token lands on MagicBook's disk
                subprocess.run(SSH + ["curl.exe --parallel --parallel-max 6 -K -"], input="\n".join(lines) + "\n",
                               capture_output=True, text=True, timeout=max(3600, int(size / 1e6) + 900))
            except subprocess.TimeoutExpired:
                log("  curl batch timed out")
            hs = remote_hashes(rdir, [f"{a['name']}.{RID}.dl" for a in b])
            good, bad = [], []
            for a in b:
                want = (a.get("digest") or "").removeprefix("sha256:")
                (good if want and hs.get(f"{a['name']}.{RID}.dl") == want else bad).append(a)
            res = move_no_replace(rdir, [(f"{a['name']}.{RID}.dl", a["name"]) for a in good])
            for a in good:
                if res.get(a["name"]) == "ok":
                    copied += 1
                else:
                    PROBLEMS.append(f"{label}: {a['name']} verified but final name taken; kept as .dl")
            present = [a for a in bad if f"{a['name']}.{RID}.dl" in hs]
            if present:   # keep (never delete) a download that failed verification, out of the way
                move_no_replace(rdir, [(f"{a['name']}.{RID}.dl", f"{a['name']}.{RID}.bad") for a in present])
                PROBLEMS.append(f"{label}: {len(present)} download(s) failed verification, kept as *.{RID}.bad")
            failed += bad
            log(f"  {label}: batch {len(b)} files {size / 1e9:.2f} GB in {time.time() - t0:.0f}s: "
                f"{len(good)} ok, {len(bad)} failed")
        todo = failed
        if not todo:
            break
    return copied, len(todo), False


# ------------------------------------------------------------------ 1. incremental archive releases

def copy_archive() -> None:
    tags = release_tags(ARCHIVE_REPO)
    total_copied = total_missing = 0
    name = ARCHIVE_REPO.split("/")[1]
    for tag in sorted(tags):
        if time_up():
            SUMMARY.append("archive: time budget reached, continues next run")
            break
        assets = [a for a in release_assets(ARCHIVE_REPO, tag)
                  if a["state"] == "uploaded" and not a["name"].startswith("probe")]   # probe*.bin: upload speed tests
        c, m, stopped = copy_assets(ARCHIVE_REPO, f"{BASE}/gh/{name}/{tag}", assets, tag)
        total_copied += c
        total_missing += m
        if stopped:
            PROBLEMS.append(f"archive: MagicBook below the {FLOOR / 1e9:.0f} GB floor while copying {tag}")
            break
    SUMMARY.append(f"archive: {len(tags)} releases, copied {total_copied} new file(s), {total_missing} still missing")


# ------------------------------------------------------------------ 2. weekly full snapshots

def snapshot_parts(tag: str) -> tuple[list[dict], dict] | None:
    """(parts in restore order, parts.sha256 asset) if the snapshot release is complete and verified, else None."""
    assets = {a["name"]: a for a in release_assets(SNAPSHOT_REPO, tag)}
    man = assets.get("parts.sha256")
    if not man or man["state"] != "uploaded":
        return None
    body = gh("api", f"repos/{SNAPSHOT_REPO}/releases/assets/{man['id']}", "-H", "Accept: application/octet-stream",
              repo=SNAPSHOT_REPO)
    parts = []
    for line in body.splitlines():
        if not line.strip():
            continue
        sha, pname = line.split(None, 1)
        a = assets.get(pname.strip())
        if not a or a["state"] != "uploaded" or (a.get("digest") or "") != f"sha256:{sha}":
            return None
        parts.append(a)
    return (parts, man) if parts else None


def copy_weekly_snapshots() -> None:
    tags = sorted(t for t in release_tags(SNAPSHOT_REPO) if t.startswith("snapshot-"))
    on_mb = set(ps(f"$d = {q(BASE + '/full-snapshot')}; if (Test-Path -LiteralPath $d) {{ Get-ChildItem -LiteralPath $d -Directory -Name }}", 300))
    weeks: dict[str, list[str]] = {}
    for t in tags:
        d = dt.datetime.strptime(t[9:17], "%Y%m%d")
        wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        if wk >= FIRST_WEEK:
            weeks.setdefault(wk, []).append(t)
    this_week = "{}-W{:02d}".format(*dt.datetime.now(dt.timezone.utc).isocalendar()[:2])
    pending = []
    for wk, cands in sorted(weeks.items()):
        started = [t for t in cands if t in on_mb]
        chosen = None
        for t in (started or cands):
            sp = snapshot_parts(t)
            if sp:
                chosen = (t, sp)
                break
        if not chosen:
            if wk != this_week:
                PROBLEMS.append(f"{wk}: no complete, verified snapshot release on GitHub")
            continue
        tag, (parts, man) = chosen
        rdir = f"{BASE}/full-snapshot/{tag}"
        have = remote_listing(rdir) if tag in on_mb else {}
        need = sum(a["size"] for a in parts if a["name"] not in have)
        if need == 0 and "parts.sha256" in have:
            SUMMARY.append(f"snapshot {wk}: {tag} complete on MagicBook")
            continue
        free = free_bytes()
        if free - need < FLOOR:
            pending.append((wk, tag, need, free, len(parts)))
            continue
        c, m, stopped = copy_assets(SNAPSHOT_REPO, rdir, parts + [man], f"snapshot {tag}")
        if stopped:   # ran out of space part-way: same as not fitting
            pending.append((wk, tag, need, free_bytes(), len(parts)))
        elif m:       # time budget or failed downloads: not a space problem, but never silently green
            PROBLEMS.append(f"snapshot {wk}: {tag} copy in progress ({c} new file(s), {m} still missing) -- continues next run")
        else:
            SUMMARY.append(f"snapshot {wk}: {tag} copied ({c} new file(s)), complete on MagicBook")
    for wk, tag, need, free, n in pending:
        SUMMARY.append(f"PENDING snapshot {wk}: {tag} — needs {need / 1e9:.1f} GB more, MagicBook free {free / 1e9:.1f} GB "
                       f"(floor {FLOOR / 1e9:.0f} GB); fully kept in GitHub release ({n} parts verified); retried every run")
    alert_space(pending)


def alert_space(pending: list) -> None:
    title = os.environ.get("MB_ALERT_TITLE", "MagicBook 空间不足")
    open_ = json.loads(gh("issue", "list", "--repo", ALERT_REPO, "--state", "open", "--search", f'in:title "{title}"',
                          "--json", "number,title", repo=SNAPSHOT_REPO) or "[]")
    open_ = [i for i in open_ if i["title"] == title]
    url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    if not pending:
        for i in open_:
            gh("issue", "close", str(i["number"]), "--repo", ALERT_REPO, "--comment",
               f"已恢复：每周快照都已完整拷到 MagicBook。{url}", repo=SNAPSHOT_REPO, check=False)
        return
    rows = "\n".join(f"- {wk}：`{tag}` 还需要 **{need / 1e9:.1f} GB**，MagicBook 现在剩 **{free / 1e9:.1f} GB**"
                     f"（至少保留 {FLOOR / 1e9:.0f} GB）。GitHub release 里 {n} 个分片都在，sha256 已逐个核对。"
                     for wk, tag, need, free, n in pending)
    body = (f"每周完整快照没法拷到 MagicBook，空间不够。\n\n{rows}\n\n"
            "这些快照没有跳过：每次运行都会自动重试，空间够了就接着拷。拷贝程序不会删除或覆盖任何文件，"
            f"要腾空间需要你来决定。\n\n运行记录：{url}")
    if open_:
        gh("issue", "comment", str(open_[0]["number"]), "--repo", ALERT_REPO, "--body", body, repo=SNAPSHOT_REPO, check=False)
    else:
        gh("issue", "create", "--repo", ALERT_REPO, "--title", title, "--assignee", "BH3GEI", "--body", body,
           repo=SNAPSHOT_REPO, check=False)
    PROBLEMS.append(f"{len(pending)} weekly snapshot(s) PENDING: MagicBook lacks space (issue opened/updated)")


def main() -> None:
    what = set(sys.argv[1:]) or {"archive", "snapshots"}
    log(f"start run {RID}; MagicBook free {free_bytes() / 1e9:.1f} GB")
    # Snapshots first: ~25 large files need ~25 API calls, while the archive is tens of thousands of
    # small files whose pace is set by the API budget.
    if "snapshots" in what:
        copy_weekly_snapshots()
    if "archive" in what:
        copy_archive()
    SUMMARY.append(f"MagicBook free at end: {free_bytes() / 1e9:.1f} GB")
    status = "\n".join(SUMMARY + [f"PROBLEM: {p}" for p in PROBLEMS])
    log("\n" + status)
    if os.environ.get("GITHUB_STEP_SUMMARY"):   # public: no host names, URLs or file names beyond tags
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("### MagicBook copy\n\n```\n" + status + "\n```\n")
    sys.exit(1 if PROBLEMS else 0)


if __name__ == "__main__":
    main()
