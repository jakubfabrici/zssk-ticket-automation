#!/usr/bin/env bash
# One-shot, idempotent bootstrap for the ZSSK ticket automation, meant to run
# ON the Proxmox host (pve). It creates a dedicated LXC and installs the
# automation inside it. Safe to re-run: an existing container is reused.
#
#   Usage (as root on pve, from a checkout of this repo):
#     bash bootstrap.sh
#
#   Or without a checkout on the host:
#     VMID=121 IP_CIDR=192.168.1.251/24 bash <(curl -fsSL <raw-url-of-this-file>)
#     # ...but then set KIT_URL so the patch and helper files can be fetched too.
set -euo pipefail

# ---- tunables --------------------------------------------------------------
VMID="${VMID:-121}"
HOSTNAME_="${HOSTNAME_:-zssk}"
BRIDGE="${BRIDGE:-vmbr0}"
IP_CIDR="${IP_CIDR:-192.168.1.251/24}"        # free static addr next to mcp (.250)
GATEWAY="${GATEWAY:-192.168.1.1}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORE="${TEMPLATE_STORE:-local}"
DISK_GB="${DISK_GB:-6}"
MEM_MB="${MEM_MB:-1024}"                      # Firefox needs more than the MCP box
CORES="${CORES:-2}"

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }

# ---- 1. LXC ----------------------------------------------------------------
if pct status "$VMID" &>/dev/null; then
  say "LXC $VMID already exists, reusing it"
  pct status "$VMID" | grep -q running || pct start "$VMID"
else
  say "creating LXC $VMID ($HOSTNAME_) at $IP_CIDR"
  TMPL=$(pveam list "$TEMPLATE_STORE" 2>/dev/null | awk '/debian-12.*amd64/{print $1}' | sort -V | tail -1)
  if [ -z "${TMPL:-}" ]; then
    pveam update
    pveam download "$TEMPLATE_STORE" "$(pveam available | awk '/debian-12-standard.*amd64/{print $2}' | tail -1)"
    TMPL=$(pveam list "$TEMPLATE_STORE" | awk '/debian-12.*amd64/{print $1}' | sort -V | tail -1)
  fi
  # nesting=1 so Firefox's own sandbox works inside an unprivileged container.
  pct create "$VMID" "$TMPL" \
    --hostname "$HOSTNAME_" --cores "$CORES" --memory "$MEM_MB" \
    --rootfs "${STORAGE}:${DISK_GB}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=${IP_CIDR},gw=${GATEWAY}" \
    --features nesting=1 --unprivileged 1 --onboot 1 --start 1
  sleep 5
fi

inct() { pct exec "$VMID" -- bash -lc "$*"; }

say "waiting for container network"
for _ in $(seq 1 30); do inct "getent hosts deb.debian.org >/dev/null 2>&1" && break; sleep 2; done

# ---- 2. push this kit and install inside the container ---------------------
say "copying the deploy kit into the container"
TARBALL=/tmp/zssk-kit.tar.gz
tar -C "$KIT_DIR" -czf "$TARBALL" .
inct "rm -rf /opt/zssk-kit && mkdir -p /opt/zssk-kit"
pct push "$VMID" "$TARBALL" /tmp/zssk-kit.tar.gz
inct "tar -C /opt/zssk-kit -xzf /tmp/zssk-kit.tar.gz"
rm -f "$TARBALL"

say "installing inside the container"
inct "bash /opt/zssk-kit/install.sh"

CT_IP="${IP_CIDR%%/*}"
cat <<MSG

Container $VMID ($HOSTNAME_, $CT_IP) is ready.

Next, from the Proxmox host:

  the web UI at http://$CT_IP:8080 (or nano /etc/zssk/person.txt)  # passenger data
  pct exec $VMID -- /opt/zssk/buy.sh --stop-before-pay   # dry run
  pct exec $VMID -- /opt/zssk/buy.sh                     # real run

Pull a debug screenshot out of the container with:

  pct pull $VMID /var/lib/zssk/debug/<debug-file>.png ./<debug-file>.png
MSG
