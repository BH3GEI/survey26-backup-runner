# survey26-backup-runner (public)

Workflows + scripts only. They produce the **encrypted** backups of the GOSIM 2026 survey-agent
hackathon platform and upload them to two **private** repositories; nothing readable is published here.

| workflow | schedule | output (private) |
|---|---|---|
| `incremental` | self-redispatching relay (~15 min), cron backstop | `gosimfoundation/survey26-archive` (git + releases) |
| `db-dump` | hourly | `gosimfoundation/survey26-archive` releases `db-YYYYMMDD` |
| `full-snapshot` | every 2 days | `BH3GEI/survey26-backup` releases `snapshot-<UTC>` |
| `monitor` | after every run + cron | alert issue (label `backup-alert`) in the private archive, assigned to the owner |

Why public: GitHub-hosted runners are free for public repositories.

How it stays private (same model as the sealed public runner pool):
- Everything is age-encrypted (X25519) to the archive's public key **in memory**; only ciphertext is
  written to disk (`export.seal`, streaming `pg_dump | age`, `tar | zstd | age`). Git mirrors and
  any temporary plaintext live in RAM (`/dev/shm`).
- All program output goes through `scripts/sealed.sh`: it is encrypted to the same key and
  uploaded to the private archive (`runner-logs-YYYYMMDD`); the public log shows one line per step.
- No artifacts, caches or job summaries. Triggers are `schedule` / `workflow_dispatch` only (no
  `push` / `pull_request`), jobs run only on `main` of this repository, so secrets are never
  exposed to forks. Actions restricted to GitHub-owned actions; default token read-only; fork PR
  workflows need approval; log retention 1 day.
- The private key is not here (owner holds it; see the private archive's RESTORE.md).

Switch: repository variable `BACKUP_ENABLED`. Retention of full snapshots: `RETENTION_ENABLED`
(off; keep everything until after 2026-10-31).
