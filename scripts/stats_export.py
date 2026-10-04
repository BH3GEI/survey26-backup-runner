#!/usr/bin/env python3
"""Export aggregate LLM-usage statistics to the PRIVATE repo BH3GEI/survey26-stats.

Reads (read-only transactions, Supabase Management API) the hourly aggregates built by
private.stats_refresh() plus the report queries in stats_queries.sql, writes CSV/JSON and a
regenerated REPORT.md into a RAM clone of the private repo, commits and pushes.
Run only through sealed.sh: output is encrypted; the public log shows one status line.
Env: SUPABASE_ACCESS_TOKEN, SUPABASE_PROJECT_REF, GH_TOKEN (write to the stats repo)."""
from __future__ import annotations

import csv, json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = os.environ.get("STATS_REPO", "BH3GEI/survey26-stats")
WORK = Path(os.environ.get("ARCHIVE_RAMDIR", "/dev/shm/s26")) / "stats"
SQL = Path(__file__).resolve().parent / "stats_queries.sql"
MAX_AGE_MIN = 150
TABLES = {"llm_usage_daily": "select * from private.stats_llm_usage_daily order by day_cst, team_name, phase, purpose, card, family, vendor",
          "egress_hosts_daily": "select * from private.stats_egress_hosts_daily order by day_cst, team_name, host, port",
          "meta": "select * from private.stats_meta"}


def query(sql: str) -> list[dict]:
    for attempt in range(4):
        try:
            return _query(sql)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def paged(sql: str, size: int = 250) -> list[dict]:
    rows: list[dict] = []
    while True:
        page = query(f"{sql} limit {size} offset {len(rows)}")
        rows += page
        if len(page) < size:
            return rows


def _query(sql: str) -> list[dict]:
    body = json.dumps({"query": "begin read only; set local statement_timeout='60s'; " + sql.strip().rstrip(";") + "; commit;"}).encode()
    req = urllib.request.Request(f"https://api.supabase.com/v1/projects/{os.environ['SUPABASE_PROJECT_REF']}/database/query",
                                 data=body, headers={"Authorization": "Bearer " + os.environ["SUPABASE_ACCESS_TOKEN"],
                                                     "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        rows = json.load(r)
    if not isinstance(rows, list):
        raise RuntimeError("query failed")
    return rows


def sections() -> tuple[str, list[tuple[str, str]]]:
    parts = re.split(r"^-- ===== (.+?) =====\s*$", SQL.read_text(), flags=re.M)
    base, out = "", []
    for name, body in zip(parts[1::2], parts[2::2]):
        if name.startswith("BASE"):
            base = body.strip()
        else:
            out.append((name, body.strip().rstrip(";")))
    return base, out


def md_table(rows: list[dict]) -> str:
    if not rows:
        return "_(no rows)_\n"
    ks = list(rows[0])
    fmt = lambda v: "" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v)).replace("|", "/")
    return "\n".join(["| " + " | ".join(ks) + " |", "|" + "---|" * len(ks)] +
                     ["| " + " | ".join(fmt(r[k]) for k in ks) + " |" for r in rows]) + "\n"


def git(*a: str) -> str:
    return subprocess.run(["git", "-C", str(WORK), *a], check=True, capture_output=True, text=True).stdout


def main() -> None:
    now = datetime.now(timezone.utc)
    WORK.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rm", "-rf", str(WORK)], check=True)
    url = f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{REPO}.git"
    subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(WORK)], check=True, capture_output=True)
    git("config", "user.name", "survey26-stats-bot"); git("config", "user.email", "actions@users.noreply.github.com")
    data = WORK / "data"; data.mkdir(exist_ok=True)
    tables = {k: paged(q) for k, q in TABLES.items()}
    for k, rows in tables.items():
        (data / f"{k}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
        with open(data / f"{k}.csv", "w", newline="") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    meta = tables["meta"][0] if tables["meta"] else {}
    refreshed = datetime.fromisoformat(meta["refreshed_at"].replace(" ", "T").replace("+00", "+00:00")) if meta else None
    age = (now - refreshed).total_seconds() / 60 if refreshed else float("inf")

    base, qs = sections()
    out = [f"# LLM usage statistics (auto-generated)\n\nExported {now:%Y-%m-%d %H:%M} UTC by "
           f"BH3GEI/survey26-backup-runner (`stats-export`, every 6 h). Aggregates refreshed hourly by "
           f"`private.stats_refresh()` at {meta.get('refreshed_at', '?')} ({age:.0f} min before export).\n\n"
           "Definitions: real teams = not hidden, no \"(test)\", leader not excluded. LLM run = platform "
           "model-proxy calls (before 2026-10-04 09:43Z) or outbound connections (after). Zero-token proxy "
           "calls = most likely failed. `llm_real` excludes one non-LLM endpoint routed through the proxy. "
           "Egress logs have no tokens/call counts. Correlation only - LLM teams also submit far more often.\n\n"
           "Data files: `data/*.csv|json` (aggregates). First-cut narrative analysis: `analysis/`. SQL: `queries.sql`.\n"]
    failed = []
    for name, body in qs:
        try:
            rows = query(base + "\n" + body)
            out.append(f"\n## {name}\n\n" + md_table(rows))
        except Exception:
            failed.append(name); out.append(f"\n## {name}\n\n_(query failed in this export)_\n")
    (WORK / "REPORT.md").write_text("".join(out))
    (WORK / "queries.sql").write_text(SQL.read_text())
    git("add", "-A")
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", f"stats export {now:%Y-%m-%dT%H:%MZ}")
        git("push", "-q", "origin", "HEAD")
    subprocess.run(["rm", "-rf", str(WORK)], check=False)
    print(json.dumps({"usage_rows": len(tables["llm_usage_daily"]), "egress_rows": len(tables["egress_hosts_daily"]),
                      "aggregate_age_min": round(age), "failed_queries": failed}))
    if age > MAX_AGE_MIN or failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
