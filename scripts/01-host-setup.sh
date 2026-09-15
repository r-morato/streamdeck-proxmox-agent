#!/usr/bin/env bash
# Run this ON THE PROXMOX HOST as root.
#
# Creates an unprivileged LXC container with USB passthrough for an Elgato
# Stream Deck, plus a read-only Proxmox API token the agent (running inside
# the container) uses to query host/guest stats. Nothing here is specific to
# any one homelab: every value is a variable you can override via environment
# variables before running the script, e.g.:
#
#   CTID=150 STATIC_IP=192.168.1.50/24 GATEWAY=192.168.1.1 ./01-host-setup.sh
#
# Idempotent: safe to re-run (it skips steps whose target already exists).
set -euo pipefail

# ---- configuration (override any of these via environment variables) ----
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
HOSTNAME="${HOSTNAME:-streamdeck-agent}"
STATIC_IP="${STATIC_IP:?Set STATIC_IP, e.g. STATIC_IP=192.168.1.50/24}"
GATEWAY="${GATEWAY:?Set GATEWAY, e.g. GATEWAY=192.168.1.1}"
BRIDGE="${BRIDGE:-vmbr0}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
CORES="${CORES:-1}"
MEMORY_MB="${MEMORY_MB:-512}"
SWAP_MB="${SWAP_MB:-512}"
DISK_GB="${DISK_GB:-4}"

PVE_API_USER="${PVE_API_USER:-streamdeck@pve}"
PVE_TOKEN_NAME="${PVE_TOKEN_NAME:-agent}"

ELGATO_VENDOR_ID="0fd9"  # constant: USB vendor ID for all Elgato Stream Deck models

echo "==> Ensuring a Debian 12 container template is available"
TEMPLATE=$(pveam list "$TEMPLATE_STORAGE" | awk '/debian-12-standard/ {print $1}' | tail -1)
if [ -z "$TEMPLATE" ]; then
  pveam update
  LATEST=$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)
  pveam download "$TEMPLATE_STORAGE" "$LATEST"
  TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${LATEST}"
fi
echo "    using template: $TEMPLATE"

echo "==> Writing udev rule so the Stream Deck is world-accessible for passthrough"
cat > /etc/udev/rules.d/99-streamdeck.rules << EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="${ELGATO_VENDOR_ID}", MODE="0666"
KERNEL=="hidraw*", ATTRS{idVendor}=="${ELGATO_VENDOR_ID}", MODE="0666"
EOF
udevadm control --reload-rules
udevadm trigger --attr-match=idVendor="${ELGATO_VENDOR_ID}" || true

echo "==> Creating read-only Proxmox API token for the agent (if missing)"
if ! pveum user list --output-format json | grep -q "\"${PVE_API_USER}\""; then
  pveum user add "$PVE_API_USER" --comment "Stream Deck LXC agent (read-only)"
fi
pveum aclmod / -user "$PVE_API_USER" -role PVEAuditor
if ! pveum user token list "$PVE_API_USER" --output-format json | grep -q "\"${PVE_TOKEN_NAME}\""; then
  echo "----------------------------------------------------------------------"
  echo "Creating API token. COPY THE SECRET BELOW -- it is shown only once and"
  echo "is never stored in this repo. Paste it into config.yaml on the agent."
  echo "----------------------------------------------------------------------"
  pveum user token add "$PVE_API_USER" "$PVE_TOKEN_NAME" --privsep 0
else
  echo "    token ${PVE_API_USER}!${PVE_TOKEN_NAME} already exists, not recreating"
fi

echo "==> Creating LXC $CTID ($HOSTNAME)"
if pct status "$CTID" &>/dev/null; then
  echo "    container $CTID already exists, skipping create"
else
  pct create "$CTID" "$TEMPLATE" \
    --hostname "$HOSTNAME" \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --swap "$SWAP_MB" \
    --rootfs "${STORAGE}:${DISK_GB}" \
    --net0 "name=eth0,bridge=${BRIDGE},gw=${GATEWAY},ip=${STATIC_IP},type=veth" \
    --unprivileged 1 \
    --features nesting=0,keyctl=0 \
    --onboot 1 \
    --tags streamdeck

  CONF="/etc/pve/lxc/${CTID}.conf"
  if ! grep -q "lxc.mount.entry: /dev/bus/usb" "$CONF"; then
    cat >> "$CONF" << 'EOF'
# USB passthrough for Elgato Stream Deck (udev rule sets 0666 perms on host)
lxc.cgroup2.devices.allow: c 189:* rwm
lxc.mount.entry: /dev/bus/usb dev/bus/usb none bind,optional,create=dir
EOF
  fi
fi

pct start "$CTID" 2>/dev/null || true

echo "==> Done. Container $CTID ($HOSTNAME) is running at ${STATIC_IP%/*}."
echo "    Next: run scripts/02-deploy-agent.sh CTID=$CTID"
