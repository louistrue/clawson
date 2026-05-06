#!/usr/bin/env bash
# Install clawson-watcher.service on the robot. Run as root.
#
# Usage (over Tailscale SSH):
#   tailscale ssh pollen@reachy-mini "sudo bash -s" < deploy/install-watcher.sh
#
# Idempotent: safe to re-run.

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 1
fi

UNIT_SRC="/home/pollen/clawbody/deploy/clawson-watcher.service"
UNIT_DST="/etc/systemd/system/clawson-watcher.service"
SUDOERS_SRC="/home/pollen/clawbody/deploy/clawson-watcher.sudoers"
SUDOERS_DST="/etc/sudoers.d/clawson-watcher"
LOG_DIR="/var/log/clawson"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "expected $UNIT_SRC — pull latest clawson and retry" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
chown pollen:pollen "$LOG_DIR"

# 1. Sudoers drop-in. Install via visudo -c first to validate; reject
#    if the file is malformed (which would otherwise lock out sudo).
install -m 0440 -o root -g root "$SUDOERS_SRC" "$SUDOERS_DST.tmp"
if ! visudo -c -f "$SUDOERS_DST.tmp" >/dev/null; then
  rm -f "$SUDOERS_DST.tmp"
  echo "sudoers file failed validation — refusing to install" >&2
  exit 1
fi
mv "$SUDOERS_DST.tmp" "$SUDOERS_DST"
echo "installed sudoers drop-in: $SUDOERS_DST"

# 2. Unit file.
install -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable clawson-watcher.service
echo "installed clawson-watcher.service (enabled on boot)"

# 3. The main clawson.service must NOT be auto-started on boot — the
#    watcher decides. If it's currently enabled, disable.
if systemctl is-enabled clawson.service >/dev/null 2>&1; then
  systemctl disable clawson.service
  echo "disabled clawson.service from boot autostart (watcher controls it now)"
fi

echo
echo "start the watcher now:"
echo "  sudo systemctl start clawson-watcher"
echo
echo "logs:"
echo "  journalctl -u clawson-watcher -f"
echo "  tail -f /var/log/clawson/watcher.log"
