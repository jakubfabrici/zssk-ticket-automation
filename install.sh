#!/usr/bin/env bash
# Install the ZSSK ticket automation (github.com/europ/zssk-ticket-automation)
# inside an LXC container (Debian/Ubuntu). Run as root in the container:
#
#   bash install.sh
#
# The upstream project is pinned to a known commit and patched for modern
# Selenium/Python (see patches/), because it was last touched in 2019.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/zssk}"
UPSTREAM="${UPSTREAM:-https://github.com/europ/zssk-ticket-automation.git}"
UPSTREAM_REF="${UPSTREAM_REF:-a6bcffddc8985367ab69791eb1a82389d8b6f47e}"
# Directory holding install.sh, i.e. where patches/ and person.example.txt live.
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="${PATCH:-$KIT_DIR/patches/0001-modernize-selenium4-python3.patch}"

say() { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }

ETC_DIR=/etc/zssk
STATE_DIR=/var/lib/zssk

say "migrating local state out of the git working tree"
mkdir -p "$ETC_DIR" "$STATE_DIR/debug" "$STATE_DIR/downloads" "$STATE_DIR/profile"
chmod 0700 "$STATE_DIR/debug" "$STATE_DIR/downloads" "$STATE_DIR/profile"
mkdir -p "$ETC_DIR/users" "$STATE_DIR/users"
chmod 0700 "$ETC_DIR/users" "$STATE_DIR/users"
# Order matters: these run BEFORE the git reset below, which would otherwise
# overwrite a filled-in person.txt (tracked upstream) and before git clean,
# which would delete the ledger (untracked in the working tree).
if [ ! -e "$ETC_DIR/person.txt" ]; then
  if [ -f "$APP_DIR/person.txt" ] && [ ! -L "$APP_DIR/person.txt" ] \
     && ! grep -q "example.com" "$APP_DIR/person.txt"; then
    install -m 0600 "$APP_DIR/person.txt" "$ETC_DIR/person.txt"
    echo "   migrated person.txt into $ETC_DIR"
  else
    install -m 0600 "$KIT_DIR/person.example.txt" "$ETC_DIR/person.txt"
    echo "   wrote template $ETC_DIR/person.txt -- fill it in before buying"
  fi
fi
if [ ! -e "$ETC_DIR/routes.json" ]; then
  if [ -f "$APP_DIR/bulk.json" ] && [ ! -L "$APP_DIR/bulk.json" ]; then
    install -m 0644 "$APP_DIR/bulk.json" "$ETC_DIR/routes.json"
    echo "   migrated bulk.json into $ETC_DIR/routes.json (legacy format still loads)"
  else
    install -m 0644 "$KIT_DIR/routes.example.json" "$ETC_DIR/routes.json"
    echo "   wrote template $ETC_DIR/routes.json"
  fi
fi
# Fail fast on a hand-edited config BEFORE the git reset/clean below can
# remove the app while the enrichment step later would abort anyway.
if command -v python3 >/dev/null 2>&1; then
  python3 -m json.tool "$ETC_DIR/routes.json" >/dev/null || {
    echo "!! $ETC_DIR/routes.json is not valid JSON - fix it and re-run install" >&2
    exit 1
  }
fi
if [ -f "$APP_DIR/tickets.json" ]; then
  if [ ! -e "$STATE_DIR/ledger.json" ]; then
    mv "$APP_DIR/tickets.json" "$STATE_DIR/ledger.json"
    echo "   migrated ledger into $STATE_DIR/ledger.json"
  else
    # A bare CLI run without --config still defaults its ledger here; never
    # leave a purchase record where the git clean below would eat it.
    mv "$APP_DIR/tickets.json" "$STATE_DIR/tickets.json.stray-$(date +%Y%m%d%H%M%S)"
    echo "   moved stray worktree ledger aside into $STATE_DIR"
  fi
fi

say "installing system packages (firefox + python)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# firefox-esr is the Debian package; Ubuntu ships plain "firefox" (often snap,
# which does not work in an LXC) so firefox-esr is tried first either way.
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates curl \
  xvfb fonts-dejavu-core
apt-get install -y -qq firefox-esr || apt-get install -y -qq firefox

say "fetching upstream at $UPSTREAM_REF"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin
  git -C "$APP_DIR" checkout -q -f "$UPSTREAM_REF"
  git -C "$APP_DIR" reset -q --hard "$UPSTREAM_REF"
  git -C "$APP_DIR" clean -qfd -e person.txt -e '.venv' -e 'debug-*'
else
  git clone -q "$UPSTREAM" "$APP_DIR"
  git -C "$APP_DIR" checkout -q "$UPSTREAM_REF"
fi

say "applying compatibility patch"
# Idempotent: skip when the working tree already carries the patch.
if git -C "$APP_DIR" apply --check "$PATCH" 2>/dev/null; then
  git -C "$APP_DIR" apply "$PATCH"
  echo "   patch applied"
elif git -C "$APP_DIR" apply --reverse --check "$PATCH" 2>/dev/null; then
  echo "   patch already applied"
else
  echo "!! the patch does not apply to $UPSTREAM_REF -- upstream moved?" >&2
  exit 1
fi

say "creating virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "installing geckodriver"
# Selenium Manager normally downloads a matching geckodriver on first run, but
# that needs outbound HTTPS at run time; installing it up front keeps runs
# offline-safe and makes failures obvious now rather than mid-purchase.
GD_VER="${GECKODRIVER_VERSION:-0.37.1}"
# Upgrade too, not only install: firefox-esr 140 wants geckodriver 0.37.x and
# Selenium logs a compatibility warning with the 0.36.0 this used to pin.
if ! command -v geckodriver >/dev/null 2>&1 \
   || ! geckodriver --version 2>/dev/null | head -1 | grep -q "geckodriver $GD_VER"; then
  ARCH=$(uname -m); case "$ARCH" in x86_64) GD_ARCH=linux64;; aarch64) GD_ARCH=linux-aarch64;; *) GD_ARCH="";; esac
  if [ -n "$GD_ARCH" ] && curl -fsSL -o /tmp/geckodriver.tar.gz \
      "https://github.com/mozilla/geckodriver/releases/download/v${GD_VER}/geckodriver-v${GD_VER}-${GD_ARCH}.tar.gz"; then
    tar -C /usr/local/bin -xzf /tmp/geckodriver.tar.gz geckodriver
    chmod +x /usr/local/bin/geckodriver
    rm -f /tmp/geckodriver.tar.gz
    echo "   geckodriver $GD_VER installed"
  else
    echo "   could not download geckodriver -- Selenium Manager will try at run time"
  fi
else
  echo "   geckodriver already present: $(geckodriver --version | head -1)"
fi

say "linking legacy paths into $ETC_DIR"
# The reset above restored the tracked placeholder person.txt; replace both
# well-known paths with symlinks so every old command keeps working.
rm -f "$APP_DIR/person.txt"
ln -s "$ETC_DIR/person.txt" "$APP_DIR/person.txt"
ln -sfn "$ETC_DIR/routes.json" "$APP_DIR/bulk.json"
# Standard paths for anything the migrated config does not name yet.
python3 - "$ETC_DIR/routes.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as handle:
    config = json.load(handle)
for key, value in (("person", "/etc/zssk/person.txt"),
                   ("ledger", "/var/lib/zssk/ledger.json"),
                   ("debug_dir", "/var/lib/zssk/debug"),
                   ("download_dir", "/var/lib/zssk/downloads"),
                   ("profile_dir", "/var/lib/zssk/profile")):
    config.setdefault(key, value)
with open(path, "w") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
PY

install -m 0755 "$KIT_DIR/buy.sh" "$APP_DIR/buy.sh"

say "installing the bulk buyer and the web UI"
install -m 0755 "$KIT_DIR/zssk_bulk.py" "$APP_DIR/zssk_bulk.py"
install -m 0755 "$KIT_DIR/zssk_runner.py" "$APP_DIR/zssk_runner.py"
install -m 0755 "$KIT_DIR/zssk_users.py" "$APP_DIR/zssk_users.py"
install -m 0755 "$KIT_DIR/zssk_auto.py" "$APP_DIR/zssk_auto.py"
install -m 0755 "$KIT_DIR/zssk_web.py" "$APP_DIR/zssk_web.py"
rm -rf "$APP_DIR/templates" "$APP_DIR/static"
cp -r "$KIT_DIR/templates" "$KIT_DIR/static" "$APP_DIR/"
"$APP_DIR/.venv/bin/pip" install -q 'flask>=3,<4'
chmod 0700 "$STATE_DIR/debug"    # screenshots of a checkout carry personal data

say "installing the weekly top-up timer (left disabled)"
# Installed but not enabled: the timer buys tickets, so switching it on is a
# deliberate act after a dry run has worked on this machine.
install -m 0644 "$KIT_DIR/systemd/zssk-topup.service" /etc/systemd/system/
install -m 0644 "$KIT_DIR/systemd/zssk-topup.timer" /etc/systemd/system/
systemctl daemon-reload || true
echo "   enable it with: systemctl enable --now zssk-topup.timer"

install -m 0644 "$KIT_DIR/systemd/zssk-web.service" /etc/systemd/system/
systemctl daemon-reload || true
systemctl enable zssk-web.service || true
# The token is minted BEFORE the restart: a freshly restarted app would
# otherwise serve /setup for a moment with no token required at all.
if [ ! -e "$ETC_DIR/webui.env" ]; then
  if [ ! -s "$ETC_DIR/setup-token" ]; then
    # 0600 from the first byte: the subshell umask applies before the redirect
    # creates the file, so the token is never world-readable.
    ( umask 077; python3 -c 'import secrets; print(secrets.token_hex(8))' > "$ETC_DIR/setup-token" )
  fi
  echo "   setup token (the /setup page asks for it once): $(cat "$ETC_DIR/setup-token")"
fi
# restart, not start: a redeploy must load the new code, and restart also
# starts a unit that is not running yet.
systemctl restart zssk-web.service || true
echo "   web UI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"

cat <<MSG

Installed into $APP_DIR

  1. edit the passenger data:   the web UI at port 8080 (or nano /etc/zssk/person.txt)
  2. set the route and weekdays: nano /etc/zssk/routes.json

Bulk buying (recommended -- plans the whole 60-day window, keeps a ledger):

  $APP_DIR/.venv/bin/python $APP_DIR/zssk_bulk.py --config /etc/zssk/routes.json --list
  xvfb-run -a $APP_DIR/.venv/bin/python $APP_DIR/zssk_bulk.py --config /etc/zssk/routes.json --dry-run
  xvfb-run -a $APP_DIR/.venv/bin/python $APP_DIR/zssk_bulk.py --config /etc/zssk/routes.json --confirm

  then keep the window topped up weekly:
  systemctl enable --now zssk-topup.timer

Single ticket, patched upstream script:

  $APP_DIR/buy.sh --stop-before-pay        # dry run
  $APP_DIR/buy.sh                          # real run

Screenshots and page dumps from a failed or dry run land in $APP_DIR/debug-*.
MSG
