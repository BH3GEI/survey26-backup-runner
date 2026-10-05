#!/usr/bin/env python3
"""Backup health check + scheduler backstop. Runs at the end of every incremental / DB-dump run
and on its own cron, in the public runner repo.

Unhealthy when: no successful incremental run in ARCHIVE_MAX_LAG_MIN (45) min; the last 2
incremental runs failed; newest DB dump > 90 min old; no successful full snapshot in 50 h;
no successful stats export in 13 h or its last 2 runs failed.
-> open/update ONE issue labelled backup-alert in the PRIVATE archive repo, assigned to
ARCHIVE_ALERT_ASSIGNEES (GitHub notifies them), re-dispatch what is stale, exit 1.
Healthy -> close that issue. Due (not yet late) snapshots are dispatched quietly, because
GitHub's cron is unreliable. Public log output: one status line with lags only.
Tokens: GH_TOKEN = this repo's GITHUB_TOKEN (runs, dispatch); ALERT_TOKEN = write token for issues."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RUNNER = os.environ["GITHUB_REPOSITORY"]
ALERT_REPO = os.environ.get("ARCHIVE_REPO", "gosimfoundation/survey26-archive")
MAX_LAG = float(os.environ.get("ARCHIVE_MAX_LAG_MIN") or 270)
DB_LIMIT, FULL_DUE, FULL_LIMIT = 270.0, 48 * 60 + 5, 50 * 60
STATS_DUE, STATS_LIMIT = 6 * 60 + 10, 13 * 60
NOW = datetime.now(timezone.utc)


def gh(*args: str, alert: bool = False, check: bool = True) -> str:
    env = {**os.environ, "GH_TOKEN": os.environ["ALERT_TOKEN"]} if alert else None
    r = subprocess.run(["gh", *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed")
    return r.stdout


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def lag(dt) -> float:
    return float("inf") if dt is None else (NOW - dt).total_seconds() / 60


def runs(wf: str) -> list[dict]:
    return json.loads(gh("api", f"repos/{RUNNER}/actions/workflows/{wf}/runs?per_page=20",
                         "--jq", "[.workflow_runs[]|{status,conclusion,updated_at}]"))


def last_ok(rs):
    ok = [ts(r["updated_at"]) for r in rs if r["conclusion"] == "success"]
    return max(ok) if ok else None


def main() -> None:
    problems: list[str] = []
    inc = runs("incremental.yml")
    inc_lag = lag(last_ok(inc))
    done = [r for r in inc if r["status"] == "completed"]
    if inc_lag > MAX_LAG:
        problems.append(f"incremental: last success {inc_lag:.0f} min ago (limit {MAX_LAG:.0f})")
    if len(done) >= 2 and all(r["conclusion"] != "success" for r in done[:2]):
        problems.append("incremental: last 2 runs did not succeed")
    snap = Path(os.environ.get("ARCHIVE_ROOT", "archive")) / "db/snapshots.jsonl"
    last_db = None
    if snap.exists() and snap.read_text().strip():
        last_db = datetime.strptime(json.loads(snap.read_text().strip().splitlines()[-1])["ts"],
                                    "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    db_lag = lag(last_db)
    if db_lag > DB_LIMIT:
        problems.append(f"DB dump: newest {db_lag:.0f} min old (limit {DB_LIMIT:.0f})")
    full = runs("full-snapshot.yml")
    full_lag = lag(last_ok(full))
    full_running = any(r["status"] != "completed" for r in full)
    if full_lag > FULL_LIMIT and not full_running:
        problems.append(f"full snapshot: last success {full_lag / 60:.1f} h ago (limit {FULL_LIMIT / 60:.0f} h)")

    stats = runs("stats-export.yml")
    stats_lag = lag(last_ok(stats))
    stats_running = any(r["status"] != "completed" for r in stats)
    stats_done = [r for r in stats if r["status"] == "completed"]
    if stats_lag > STATS_LIMIT and not stats_running:
        problems.append(f"stats export: last success {stats_lag / 60:.1f} h ago (limit {STATS_LIMIT / 60:.0f} h)")
    if len(stats_done) >= 2 and all(r["conclusion"] != "success" for r in stats_done[:2]):
        problems.append("stats export: last 2 runs did not succeed")

    # scheduler backstop (cron is unreliable)
    if stats_lag > STATS_DUE and not stats_running:
        gh("workflow", "run", "stats-export.yml", "--repo", RUNNER, check=False)
    if db_lag > 250 and not any(r["status"] != "completed" for r in runs("db-dump.yml")):
        gh("workflow", "run", "db-dump.yml", "--repo", RUNNER, check=False)
    if full_lag > FULL_DUE and not full_running:
        gh("workflow", "run", "full-snapshot.yml", "--repo", RUNNER, check=False)
    if inc_lag > MAX_LAG and not any(r["status"] != "completed" for r in inc):
        gh("workflow", "run", "incremental.yml", "--repo", RUNNER, check=False)

    status = (f"{NOW:%Y-%m-%d %H:%M}Z incremental {inc_lag:.0f} min, DB dump {db_lag:.0f} min, "
              f"full snapshot {full_lag / 60:.1f} h, stats export {stats_lag / 60:.1f} h -- " + ("UNHEALTHY" if problems else "healthy"))
    print(status)
    if not os.environ.get("ALERT_TOKEN"):
        sys.exit(1 if problems else 0)
    open_issues = json.loads(gh("issue", "list", "--repo", ALERT_REPO, "--label", "backup-alert", "--state", "open",
                                "--json", "number", alert=True, check=False) or "[]")
    if problems:
        body = status + "\n\n" + "\n".join("- " + p for p in problems) + \
            f"\n\nRunner: https://github.com/{RUNNER}/actions (stale workflows were re-dispatched). " \
            "Encrypted run logs: releases runner-logs-YYYYMMDD. Runbook: RESTORE.md."
        if open_issues:
            gh("issue", "comment", str(open_issues[0]["number"]), "--repo", ALERT_REPO, "--body", body, alert=True)
        else:
            gh("label", "create", "backup-alert", "--repo", ALERT_REPO, "--color", "B60205", "--force", alert=True, check=False)
            base = ["issue", "create", "--repo", ALERT_REPO, "--label", "backup-alert",
                    "--title", "BACKUP ALERT: backups are failing or lagging", "--body", body]
            extra = sum((["--assignee", a] for a in os.environ.get("ARCHIVE_ALERT_ASSIGNEES", "").replace(",", " ").split()), [])
            if not gh(*base, *extra, alert=True, check=False).strip():
                gh(*base, alert=True)
        sys.exit(1)
    for i in open_issues:
        gh("issue", "close", str(i["number"]), "--repo", ALERT_REPO, "--comment", "Recovered: " + status, alert=True, check=False)


if __name__ == "__main__":
    main()
