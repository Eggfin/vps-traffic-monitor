#!/usr/bin/env python3
"""VPS traffic agent — stdlib only, no dependencies.

Reads /proc/net/dev, keeps a persistent monthly counter (survives
reboots: counters reset to 0 on boot, the agent carries the pre-reboot
amount forward), and POSTs a sample to the hub every INTERVAL seconds.

Config (later source wins): /etc/vps-traffic/agent.conf, then env vars.
  HUB_URL   e.g. http://134.185.93.143:18080  (no trailing slash)
  TOKEN     shared secret, must match the hub
  NODE      node name, e.g. oracle-sg-0234 (default: hostname)
  INTERVAL  seconds between pushes, default 30
  IFACE     network interface, default auto (default-route iface)
"""
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

CONFIG_FILE = "/etc/vps-traffic/agent.conf"
STATE_FILE = "/var/lib/vps-traffic/state.json"


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("HUB_URL", "TOKEN", "NODE", "INTERVAL", "IFACE"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def detect_iface():
    # Default-route interface from /proc/net/route (destination 00000000).
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        pass
    return None


def read_dev():
    """Return {iface: (rx_bytes, tx_bytes)} from /proc/net/dev."""
    out = {}
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            fields = rest.split()
            if len(fields) >= 16:
                out[iface] = (int(fields[0]), int(fields[8]))
    return out


def pick_iface(cfg, counters):
    if cfg.get("IFACE") and cfg["IFACE"] != "auto":
        return cfg["IFACE"]
    auto = detect_iface()
    if auto and auto in counters:
        return auto
    for name in ("eth0", "ens3", "enp0s3", "venet0"):
        if name in counters:
            return name
    for name in counters:
        if name != "lo":
            return name
    raise RuntimeError("no usable network interface found in /proc/net/dev")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def main():
    cfg = load_config()
    hub = (cfg.get("HUB_URL") or "").rstrip("/")
    token = cfg.get("TOKEN") or ""
    node = cfg.get("NODE") or socket.gethostname()
    interval = int(cfg.get("INTERVAL") or 30)
    if not hub or not token:
        print("agent: HUB_URL and TOKEN are required "
              "(set in /etc/vps-traffic/agent.conf or env)", file=sys.stderr)
        return 2
    if not token.isascii():
        print("agent: TOKEN must be ASCII-only (it is sent as an HTTP header;"
              " check /etc/vps-traffic/agent.conf for non-ASCII characters,"
              " e.g. a placeholder that was never replaced)", file=sys.stderr)
        return 2

    counters = read_dev()
    iface = pick_iface(cfg, counters)
    print(f"agent: node={node} iface={iface} hub={hub} interval={interval}s",
          flush=True)

    state = load_state()
    month = time.strftime("%Y-%m")
    if state.get("month") != month:
        state = {"month": month, "base_rx": 0, "base_tx": 0,
                 "carried_rx": 0, "carried_tx": 0,
                 "last_rx": None, "last_tx": None, "last_ts": None}
        # base is set from the first reading below
    if state.get("base_rx") is None:
        state["base_rx"] = 0

    while True:
        try:
            rx, tx = read_dev()[iface]
            now = time.time()
            cur_month = time.strftime("%Y-%m")
            if cur_month != state["month"]:
                state = {"month": cur_month, "base_rx": rx, "base_tx": tx,
                         "carried_rx": 0, "carried_tx": 0,
                         "last_rx": None, "last_tx": None, "last_ts": None}
            if state.get("last_rx") is None:
                # First reading (or first after month rollover): anchor base.
                if state["month"] == month and state.get("base_rx", 0) == 0 \
                        and state.get("carried_rx", 0) == 0:
                    state["base_rx"], state["base_tx"] = rx, tx
                state["last_rx"], state["last_tx"], state["last_ts"] = rx, tx, now
                save_state(state)
                time.sleep(interval)
                continue

            last_rx, last_tx, last_ts = (state["last_rx"], state["last_tx"],
                                         state["last_ts"])
            # Counters reset on reboot: carry the pre-reboot monthly amount.
            if rx < last_rx:
                state["carried_rx"] += max(0, last_rx - state["base_rx"])
                state["base_rx"] = 0
            if tx < last_tx:
                state["carried_tx"] += max(0, last_tx - state["base_tx"])
                state["base_tx"] = 0
            dt = max(1, now - last_ts)
            rx_rate = max(0, rx - last_rx) / dt if rx >= last_rx else 0
            tx_rate = max(0, tx - last_tx) / dt if tx >= last_tx else 0
            rx_month = state["carried_rx"] + max(0, rx - state["base_rx"])
            tx_month = state["carried_tx"] + max(0, tx - state["base_tx"])

            payload = json.dumps({
                "node": node, "iface": iface, "ts": int(now),
                "rx_month": rx_month, "tx_month": tx_month,
                "rx_rate": rx_rate, "tx_rate": tx_rate,
            }).encode()
            req = urllib.request.Request(
                hub + "/api/push", data=payload,
                headers={"Content-Type": "application/json",
                         "X-Token": token}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
            except urllib.error.URLError as e:
                print(f"agent: push failed: {e}", flush=True)

            state.update({"last_rx": rx, "last_tx": tx, "last_ts": now})
            save_state(state)
        except Exception as e:  # never die on a transient read/push error
            print(f"agent: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
