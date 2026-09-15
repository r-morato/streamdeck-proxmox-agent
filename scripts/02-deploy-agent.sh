#!/usr/bin/env bash
# Run this ON THE PROXMOX HOST as root, after 01-host-setup.sh.
#
# Installs Python/deps inside the target LXC, copies the agent source and a
# config template into it, and enables the systemd service. It does NOT put
# any secret into the container -- you fill in the Proxmox API token and
# your own service URLs in config.yaml afterwards (see README).
#
#   CTID=110 ./02-deploy-agent.sh
set -euo pipefail

CTID="${CTID:?Set CTID to the container ID created by 01-host-setup.sh}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-streamdeck}"

echo "==> Installing OS packages in container $CTID"
pct exec "$CTID" -- bash -c "
  apt-get update -qq
  apt-get install -y -qq python3-venv python3-pip libhidapi-libusb0 libusb-1.0-0 \
    usbutils fonts-dejavu-core
"

echo "==> Creating service user and Python venv"
pct exec "$CTID" -- bash -c "
  id -u ${SERVICE_USER} &>/dev/null || useradd -r -m -d /opt/streamdeck-agent -s /usr/sbin/nologin ${SERVICE_USER}
  mkdir -p /opt/streamdeck-agent /etc/streamdeck-agent
  python3 -m venv /opt/streamdeck-agent/venv
  /opt/streamdeck-agent/venv/bin/pip install -q --upgrade pip
  /opt/streamdeck-agent/venv/bin/pip install -q streamdeck pillow requests pyyaml speedtest-cli
"

echo "==> Copying agent source and unit file"
pct push "$CTID" "$REPO_DIR/agent/agent.py" /opt/streamdeck-agent/agent.py
pct push "$CTID" "$REPO_DIR/agent/streamdeck-agent.service" /etc/systemd/system/streamdeck-agent.service

if pct exec "$CTID" -- test -f /etc/streamdeck-agent/config.yaml; then
  echo "==> config.yaml already exists in the container, leaving it as-is"
else
  echo "==> Seeding config.yaml from the example template (EDIT IT before starting the service)"
  pct push "$CTID" "$REPO_DIR/agent/config.example.yaml" /etc/streamdeck-agent/config.yaml
fi

pct exec "$CTID" -- bash -c "
  chown -R ${SERVICE_USER}:${SERVICE_USER} /opt/streamdeck-agent /etc/streamdeck-agent
  chmod 600 /etc/streamdeck-agent/config.yaml
  systemctl daemon-reload
  systemctl enable streamdeck-agent
"

echo "==============================================================="
echo "Deployed. Before starting the service:"
echo "  pct exec $CTID -- nano /etc/streamdeck-agent/config.yaml"
echo "Fill in your Proxmox node name, API token secret, and service"
echo "health-check URLs (see README for the config reference)."
echo "Then start it:"
echo "  pct exec $CTID -- systemctl start streamdeck-agent"
echo "  pct exec $CTID -- journalctl -u streamdeck-agent -f"
echo "==============================================================="
