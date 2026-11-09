#!/usr/bin/env bash
# One-line hub install (run as root / with sudo, on the hub machine only):
#   curl -fsSL https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main/install-hub.sh \
#     | sudo bash -s -- --token YOUR_TOKEN --port 18080
# Then open TCP <port> in the cloud firewall / security list of the hub
# machine (e.g. Oracle security list), otherwise agents cannot reach it.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main"
TOKEN="" PORT="18080"

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$TOKEN" ] || {
  echo "usage: install-hub.sh --token TOKEN [--port 18080]" >&2
  echo "generate one with: openssl rand -hex 16" >&2
  exit 2
}
[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v curl >/dev/null || command -v wget >/dev/null || { echo "curl or wget is required" >&2; exit 1; }

mkdir -p /etc/vps-traffic /var/lib/vps-traffic /usr/local/bin
cat > /etc/vps-traffic/hub.conf <<EOF
TOKEN=$TOKEN
PORT=$PORT
DB=/var/lib/vps-traffic/traffic.db
HOST=0.0.0.0
EOF
chmod 600 /etc/vps-traffic/hub.conf

if command -v curl >/dev/null; then
  curl -fsSL "$REPO_RAW/hub.py" -o /usr/local/bin/vps-traffic-hub.py
else
  wget -qO /usr/local/bin/vps-traffic-hub.py "$REPO_RAW/hub.py"
fi
chmod 755 /usr/local/bin/vps-traffic-hub.py

cat > /etc/systemd/system/vps-traffic-hub.service <<EOF
[Unit]
Description=VPS traffic hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/vps-traffic-hub.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now vps-traffic-hub
sleep 2
systemctl status vps-traffic-hub --no-pager || true
curl -fsS "http://127.0.0.1:$PORT/health" && echo
echo "hub installed on port $PORT"
echo "verify from another machine: curl 'http://<hub-ip>:$PORT/api/stats?token=$TOKEN'"
echo "logs: journalctl -u vps-traffic-hub -f"
