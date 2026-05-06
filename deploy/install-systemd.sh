#!/usr/bin/env bash
# Install the Clawson systemd unit on the robot. Run as root via sudo.
#
# Usage (over Tailscale SSH):
#   tailscale ssh pollen@reachy-mini "sudo bash -s" < deploy/install-systemd.sh
#
# Idempotent: safe to re-run.

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 1
fi

UNIT_SRC="/home/pollen/clawbody/deploy/clawson.service"
UNIT_DST="/etc/systemd/system/clawson.service"
LOG_DIR="/var/log/clawson"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "expected unit file at $UNIT_SRC — pull latest clawson and retry" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
chown pollen:pollen "$LOG_DIR"

install -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable clawson.service
echo "installed clawson.service (enabled on boot)"
echo "start with:   sudo systemctl start clawson"
echo "logs:         journalctl -u clawson -f   OR   tail -f /var/log/clawson/clawson.log"
