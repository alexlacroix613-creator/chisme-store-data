#!/bin/bash
# refresh.sh — weekly Chisme Alberta store-locator refresh (runs on Optimus).
#
# 1. Regenerates chisme-alberta-stores.json from the live AGLC/LiquorConnect feed
# 2. Commits + pushes to origin/main (GitHub Pages -> chisme.party reads this)
# 3. Appends a `run_ok <ISO ts> <store_count>` line to run.log on success,
#    or `run_fail <ISO ts> <reason>` on any failure.
#
# DEAD-MAN'S SWITCH: this is a WEEKLY job. If run.log's latest `run_ok` line is
# more than 8 days old, the job has died — investigate launchd / the cron log.
# (Mirrors the VENN / PBG dead-man pattern, scaled from daily/hourly to weekly.)
#
# Scheduled via launchd: com.siempre.chisme-locator-refresh (Mon 06:00 MT).
# Additive only — touches nothing else on the box.

set -uo pipefail

# Resolve repo root (script lives in <repo>/scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="$REPO_DIR/run.log"
JSON="$REPO_DIR/chisme-alberta-stores.json"
PY="${PYTHON_BIN:-/usr/bin/python3}"

# Make sure gh-provided git credentials + git are on PATH for launchd.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

fail() {
  echo "run_fail $(ts) $1" >> "$LOG"
  echo "REFRESH FAILED: $1" >&2
  exit 1
}

cd "$REPO_DIR" || fail "cannot_cd_repo"

# Stay current with remote before regenerating (avoid non-fast-forward).
git pull --ff-only origin main >/dev/null 2>&1 || true

# 1. Regenerate the locator JSON in place.
"$PY" "$SCRIPT_DIR/scrape_chisme_ab.py" -o "$JSON" || fail "generator_error"

# Sanity: must be valid JSON with a plausible store_count.
COUNT="$("$PY" - "$JSON" << 'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
n = int(d.get("store_count", 0))
assert n >= 20, f"store_count too low: {n}"   # ~48 expected; guard against empty/partial
print(n)
PYEOF
)" || fail "json_sanity_failed"

# 2. Commit + push (only if something changed).
git add "$JSON"
if git diff --cached --quiet; then
  echo "run_ok $(ts) $COUNT (no_change)" >> "$LOG"
  echo "No change in store data ($COUNT stores). Logged run_ok."
  exit 0
fi

git -c user.name="Chisme Locator Bot" -c user.email="alex@siempretequila.com" \
    commit -m "chore: weekly Chisme AB store refresh ($COUNT stores)" >/dev/null \
    || fail "commit_error"

git push origin main >/dev/null 2>&1 || fail "push_error"

# 3. Success.
echo "run_ok $(ts) $COUNT" >> "$LOG"
echo "Refresh OK: pushed $COUNT stores to origin/main."
exit 0
