#!/usr/bin/env bash
# Lock down wifi-LAN exposure on the Reachy Mini.
#
# Background: the upstream reachy_mini daemon listens on 0.0.0.0:8000
# and 0.0.0.0:8443 with no authentication, exposing the Reachy
# dashboard + admin API (cache reset, app install, update endpoints) to
# anyone on the same wifi network. SSH listens on 0.0.0.0:22 with
# password auth enabled. None of this is acceptable for a robot with a
# camera, mic, and write tokens to GitHub/Vercel/Todoist.
#
# This script:
#   1. Creates an iptables chain CLAWSON-FW that allows lo + tailscale0
#      + ESTABLISHED/RELATED, then DROPs tcp dst-ports 8000, 8443, 7860,
#      22 from anywhere else.
#   2. Hooks the chain at the head of INPUT, idempotent.
#   3. Persists rules via netfilter-persistent (installed on demand)
#      OR a fallback systemd unit, whichever the system has.
#
# Safe to re-run. Tailscale SSH and loopback access are explicitly
# preserved. If you get locked out somehow, plug in a keyboard/HDMI to
# the CM4 and run:  sudo iptables -F INPUT && sudo iptables -X CLAWSON-FW

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 1
fi

CHAIN="CLAWSON-FW"

# 1. Build/replace the chain.
iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"

iptables -A "$CHAIN" -i lo -j RETURN
iptables -A "$CHAIN" -i tailscale0 -j RETURN
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

# Drop dangerous TCP ports from non-loopback, non-Tailscale, non-
# established sources. Order doesn't matter inside a DROP block.
for port in 8000 8443 7860 22; do
  iptables -A "$CHAIN" -p tcp --dport "$port" -j DROP
done

iptables -A "$CHAIN" -j RETURN

# 2. Hook the chain at the start of INPUT, idempotent.
while iptables -C INPUT -j "$CHAIN" 2>/dev/null; do
  iptables -D INPUT -j "$CHAIN"
done
iptables -I INPUT 1 -j "$CHAIN"

# 3. Persist. Try netfilter-persistent (debian/ubuntu standard), fall
#    back to a one-shot systemd service that restores from a saved
#    ruleset on boot.
RULES_DIR="/etc/iptables"
RULES_FILE="$RULES_DIR/clawson-rules.v4"
mkdir -p "$RULES_DIR"
iptables-save > "$RULES_FILE"
chmod 0600 "$RULES_FILE"

if command -v netfilter-persistent >/dev/null 2>&1; then
  cp "$RULES_FILE" "$RULES_DIR/rules.v4"
  netfilter-persistent save || true
  echo "persisted via netfilter-persistent"
else
  cat > /etc/systemd/system/clawson-firewall.service <<EOF
[Unit]
Description=Clawson firewall (restore iptables rules)
DefaultDependencies=no
Before=network-pre.target tailscaled.service
Wants=network-pre.target
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/sbin/iptables-restore $RULES_FILE
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable clawson-firewall.service
  echo "persisted via systemd unit clawson-firewall.service"
fi

echo
echo "applied. CLAWSON-FW chain active. Tailscale SSH preserved."
echo "verify with:"
echo "  iptables -L CLAWSON-FW -nv --line-numbers"
