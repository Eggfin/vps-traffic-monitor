#!/usr/bin/env bash
# One-line agent install (run as root / with sudo):
#   curl -fsSL https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main/install-agent.sh \
#     | sudo bash -s -- --hub http://HUB_IP:18080 --token YOUR_TOKEN --name my-node
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main"
HUB="" TOKEN="" NODE="" INTERVAL="30" IFACE="auto"

while [ $# -gt 0 ]; do
  case "$1" in
    --hub) HUB="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --name) NODE="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$HUB" ] && [ -n "$TOKEN" ] && [ -n "$NODE" ] || {
  echo "usage: install-agent.sh --hub http://HUB_IP:18080 --token TOKEN --name NODE [--interval 30] [--iface auto]" >&2
  exit 2
}
if printf '%s' "$TOKEN" | LC_ALL=C grep -q '[^ -~]'; then
  echo "error: --token must be ASCII-only (no Chinese characters or placeholders like 你的TOKEN); use the same token as the hub" >&2
  exit 2
fi
[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v curl >/dev/null || command -v wget >/dev/null || { echo "curl or wget is required" >&2; exit 1; }

mkdir -p /etc/vps-traffic /var/lib/vps-traffic /usr/local/bin
cat > /etc/vps-traffic/agent.conf <<EOF
HUB_URL=$HUB
TOKEN=$TOKEN
NODE=$NODE
INTERVAL=$INTERVAL
IFACE=$IFACE
EOF
chmod 600 /etc/vps-traffic/agent.conf

if command -v curl >/dev/null; then
  curl -fsSL "$REPO_RAW/agent.py" -o /usr/local/bin/vps-traffic-agent.py
else
  wget -qO /usr/local/bin/vps-traffic-agent.py "$REPO_RAW/agent.py"
fi
chmod 755 /usr/local/bin/vps-traffic-agent.py

cat > /etc/systemd/system/vps-traffic-agent.service <<EOF
[Unit]
Description=VPS traffic agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/vps-traffic-agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now vps-traffic-agent
sleep 2
systemctl status vps-traffic-agent --no-pager || true
echo "agent installed: node=$NODE hub=$HUB"
echo "logs: journalctl -u vps-traffic-agent -f"
