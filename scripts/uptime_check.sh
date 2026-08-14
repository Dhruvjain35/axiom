#!/usr/bin/env bash
# AXIOM :: is the demo actually alive?
#
#   ./scripts/uptime_check.sh                       # the live deployment
#   ./scripts/uptime_check.sh http://localhost:8000 # anywhere else
#
# Judging runs Aug 19 - Sep 15 with nobody watching. The failure this exists to catch is
# not "the site is down" — a free uptime monitor pinging / catches that. It is the quieter
# one: the page still loads, returns 200, and is USELESS, because the database is
# unreachable, or the demo tenant was wiped and never re-seeded, or the provider ledger is
# gone. A judge opening that sees a dead board and concludes the project does not work.
#
# So this asserts the demo is USABLE, not merely reachable:
#
#   1. /api/health reports ok            — DB and provider both answer
#   2. a mission exists with tasks       — there is something to look at
#   3. the vector index is still CHOSEN  — recall has not silently degraded to a scan
#   4. zero duplicate effects            — the headline claim is still true
#
# Exit 0 means a judge would have a good experience. Any non-zero means intervene.
#
# Point any free monitor at it, or run it from cron:
#   */15 * * * * /path/to/axiom/scripts/uptime_check.sh || mail -s 'AXIOM demo down' you@…
set -uo pipefail

BASE="${1:-https://axiom-one-sage.vercel.app}"
TIMEOUT="${TIMEOUT:-45}"
FAIL=0

say()  { printf '  %-34s %s\n' "$1" "$2"; }
bad()  { printf '  %-34s \033[31m%s\033[0m\n' "$1" "$2"; FAIL=1; }

# Cold starts are real on serverless: the first request after an idle hour pays for the
# container AND the first database connection. Retrying twice distinguishes "waking up"
# from "broken", which is the difference between a useful alert and one that gets muted.
fetch() {
  local url="$1" body
  for attempt in 1 2 3; do
    body="$(curl -sS --max-time "$TIMEOUT" "$url" 2>/dev/null)" && [ -n "$body" ] && {
      printf '%s' "$body"; return 0; }
    sleep $(( attempt * 3 ))
  done
  return 1
}

json() { python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: print(''); raise SystemExit
p='$1'.split('.')
for k in p:
    if isinstance(d,dict): d=d.get(k)
    else: d=None; break
print('' if d is None else d)" 2>/dev/null; }

echo
echo "  AXIOM uptime check -> $BASE"
echo "  ----------------------------------------------------------------"

HEALTH="$(fetch "$BASE/api/health")" || { bad "reachable" "no response after 3 tries"; echo; exit 1; }

STATUS="$(printf '%s' "$HEALTH" | json status)"
DB="$(printf '%s' "$HEALTH" | json db)"
PROV="$(printf '%s' "$HEALTH" | json provider)"

[ "$STATUS" = "ok" ] && say "health"   "ok"            || bad "health"   "status=$STATUS"
[ "$DB"   = "True" ] && say "database" "reachable"     || bad "database" "unreachable"
[ "$PROV" = "True" ] && say "provider" "reachable"     || bad "provider" "unreachable"

# A demo with no mission is a blank page. demo_state self-heals, so this failing means the
# heal itself is broken — exactly the silent case a plain ping would miss.
MISSION="$(fetch "$BASE/api/mission")" || MISSION='{}'
TITLE="$(printf '%s' "$MISSION" | json title)"
[ -n "$TITLE" ] && say "mission present" "$TITLE" || bad "mission present" "no mission — the board is blank"

# The headline claim, asked of the provider's own ledger rather than of AXIOM.
STATS="$(fetch "$BASE/api/provider/stats")" || STATS='{}'
DUPES="$(printf '%s' "$STATS" | json duplicate_orders)"
[ "$DUPES" = "0" ] && say "duplicate effects" "0" || bad "duplicate effects" "$DUPES — THE CLAIM IS BROKEN"

# Correct rows come back whether or not the optimizer picks the vector index, so a
# degradation here is invisible to every other check. Only the PLAN shows it.
RECALL="$(curl -sS --max-time "$TIMEOUT" -XPOST "$BASE/api/memories/recall" \
          -H 'content-type: application/json' \
          -d '{"query":"duplicate charge on customer card","memory_class":"SEMANTIC","k":3}' 2>/dev/null)"
IDX="$(printf '%s' "$RECALL" | json plan_uses_vector_index)"
[ "$IDX" = "True" ] && say "vector index" "used (not a scan)" \
                    || bad "vector index" "NOT used — recall degraded to a full scan"

echo "  ----------------------------------------------------------------"
if [ "$FAIL" -eq 0 ]; then
  echo "  OK — a judge opening this right now would have a working demo."
else
  echo "  FAILING — intervene before a judge sees it."
fi
echo
exit "$FAIL"
