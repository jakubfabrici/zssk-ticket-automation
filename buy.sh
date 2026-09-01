#!/usr/bin/env bash
# Thin wrapper around upstream buy.py with this household's default route.
#
#   ./buy.sh                          # today, next departure after now
#   ./buy.sh --stop-before-pay        # fill everything in, do not confirm
#   ./buy.sh -d 05.09.2026 -t 07:12   # explicit date/time
#   DEPARTURE="Košice" ARRIVAL="Spišská Nová Ves" ./buy.sh
#
# Any extra argument is passed straight through to buy.py, and an explicitly
# given flag wins over the defaults below.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPARTURE="${DEPARTURE:-Spišská Nová Ves}"
ARRIVAL="${ARRIVAL:-Košice}"
DATE="${DATE:-$(date +%d.%m.%Y)}"
TIME="${TIME:-$(date +%H:%M)}"

# given <short> <long> -- true when the caller already passed that flag
given() {
  local short=$1 long=$2 arg
  for arg in "${CALLER_ARGS[@]}"; do
    [ "$arg" = "$short" ] && return 0
    [ "$arg" = "$long" ] && return 0
  done
  return 1
}

CALLER_ARGS=("$@")
args=()
given -D --departure || args+=(--departure "$DEPARTURE")
given -A --arrival   || args+=(--arrival   "$ARRIVAL")
given -d --date      || args+=(--date      "$DATE")
given -t --time      || args+=(--time      "$TIME")
given -v --verbose   || args+=(--verbose)
# -H and -F are mutually exclusive in buy.py, so default to headless only when
# the caller asked for neither.
given -H --headless || given -F --fullscreen || args+=(--headless)

exec "$APP_DIR/.venv/bin/python" "$APP_DIR/buy.py" \
  "${args[@]}" --person "$APP_DIR/person.txt" "$@"
