#!/usr/bin/env bash
# sealed.sh <log-name> <command...>
# Runs a command with ALL of its stdout/stderr encrypted (age, ARCHIVE_AGE_RECIPIENTS) into a
# log file that is uploaded only to the private archive repo (release runner-logs-YYYYMMDD).
# The public Actions log gets exactly one line: "<log-name>: exit <code>".
set -uo pipefail
name=$1; shift
args=(); for r in $(echo "$ARCHIVE_AGE_RECIPIENTS" | tr ',' ' '); do args+=(-r "$r"); done
log="$RUNNER_TEMP/$name-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT.log.age"
"$@" 2>&1 | age "${args[@]}" > "$log"
rc=${PIPESTATUS[0]}
python3 "$(dirname "$0")/upload_log.py" "$log" >/dev/null 2>&1 || echo "$name: (encrypted log upload failed)"
echo "$name: exit $rc"
exit "$rc"
