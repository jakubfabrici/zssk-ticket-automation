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
if ! command -v geckodriver >/dev/null 2>&1; then
  GD_VER="${GECKODRIVER_VERSION:-0.36.0}"
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

say "passenger data"
install -m 0600 /dev/null "$APP_DIR/person.txt.new"
if [ -s "$APP_DIR/person.txt" ]; then
  rm -f "$APP_DIR/person.txt.new"
  echo "   $APP_DIR/person.txt kept (not overwritten)"
else
  cat "$KIT_DIR/person.example.txt" > "$APP_DIR/person.txt.new"
  mv "$APP_DIR/person.txt.new" "$APP_DIR/person.txt"
  chmod 0600 "$APP_DIR/person.txt"
  echo "   wrote template $APP_DIR/person.txt -- fill it in before buying"
fi

install -m 0755 "$KIT_DIR/buy.sh" "$APP_DIR/buy.sh"

cat <<MSG

Installed into $APP_DIR

  1. edit the passenger data:   nano $APP_DIR/person.txt
  2. dry run (stops before the order is confirmed):
       $APP_DIR/buy.sh --stop-before-pay
  3. real run:
       $APP_DIR/buy.sh

Screenshots and page dumps from a failed or dry run land in $APP_DIR/debug-*.
MSG
