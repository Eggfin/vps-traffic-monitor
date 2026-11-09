#!/usr/bin/env python3
"""VPS traffic hub v2 — stdlib only, no dependencies.

Receives cumulative samples from agents (POST /api/push, X-Token auth),
stores them in SQLite, and serves the dashboard API.

v2 design: agents report cumulative rx_total/tx_total since install. The hub
derives every window from that series, so per-node settings can change in
the panel without touching any agent:
  - cycle_day: billing-cycle anchor day (1-31, default 1). The monthly
    window runs from that day 00:00 to the same day next month. E.g. a VPS
    opened on the 8th uses cycle_day 8: Nov 8 00:00 -> Dec 8 00:00.
  - display_name: name shown in the panel (default: node id).
  - count_mode: "both" (rx+tx, the default — most VPS bill bidirectional),
    "out" (tx only) or "in" (rx only), for providers that bill one direction.

Windows (all rolling except month): month = current billing cycle,
week = last 7 days, day = last 24 hours, hour = last 1 hour. Window
boundaries use the hub server's local time. Samples are kept 40 days so a
full billing cycle plus a week of history always fits.

Config: /etc/vps-traffic/hub.conf, then env vars.
  TOKEN  shared secret, must match the agents (required)
  PORT   listen port, default 18080
  DB     sqlite path, default /var/lib/vps-traffic/traffic.db
  HOST   bind address, default 0.0.0.0

Endpoints (all but /health need the token, via X-Token header or ?token=):
  GET  /health
  GET  /api/stats            per-node month/week/day/hour + live rates
  GET  /api/history?node=X&hours=N
  POST /api/node             {node, display_name?, cycle_day?, count_mode?}
"""
import calendar
import json
import os
import sqlite3
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CONFIG_FILE = "/etc/vps-traffic/hub.conf"
RETENTION_DAYS = 40


def load_config():
    cfg = {"PORT": "18080", "DB": "/var/lib/vps-traffic/traffic.db",
           "HOST": "0.0.0.0"}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TOKEN", "PORT", "DB", "HOST"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = load_config()
TOKEN = CFG.get("TOKEN") or ""
DB_PATH = CFG["DB"]


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS nodes(
        node TEXT PRIMARY KEY, iface TEXT, last_seen INTEGER,
        rx_total INTEGER, tx_total INTEGER,
        rx_rate REAL, tx_rate REAL,
        display_name TEXT, cycle_day INTEGER DEFAULT 1,
        count_mode TEXT DEFAULT 'both')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT, ts INTEGER,
        rx_total INTEGER, tx_total INTEGER, rx_rate REAL, tx_rate REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_node_ts "
                 "ON samples(node, ts)")
    # Migrations for databases created by v1 (natural-month columns).
    for col, ddl in (("display_name", "ALTER TABLE nodes ADD COLUMN display_name TEXT"),
                     ("cycle_day", "ALTER TABLE nodes ADD COLUMN cycle_day INTEGER DEFAULT 1"),
                     ("count_mode", "ALTER TABLE nodes ADD COLUMN count_mode TEXT DEFAULT 'both'"),
                     ("rx_total", "ALTER TABLE nodes ADD COLUMN rx_total INTEGER"),
                     ("tx_total", "ALTER TABLE nodes ADD COLUMN tx_total INTEGER")):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    for col in ("rx_total", "tx_total"):
        try:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {col} INTEGER")
        except sqlite3.OperationalError:
            pass
    return conn


def prune(conn):
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    conn.commit()


def cycle_start_ts(now, cycle_day):
    """Start of the current billing cycle (hub local time).

    The cycle containing `now` starts on cycle_day of this month, or of last
    month when today is before cycle_day. Short months clamp the anchor to
    the month's last day (e.g. 31 -> Feb 28).
    """
    dt = datetime.fromtimestamp(now)
    day = min(cycle_day, calendar.monthrange(dt.year, dt.month)[1])
    if dt.day >= day:
        start = dt.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    else:
        year, month = (dt.year, dt.month - 1) if dt.month > 1 else (dt.year - 1, 12)
        day = min(cycle_day, calendar.monthrange(year, month)[1])
        start = dt.replace(year=year, month=month, day=day,
                           hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def window_total(conn, node, since_ts):
    """(rx, tx) accumulated since since_ts, from the cumulative series.

    Base = the last sample at or before since_ts; if the agent was installed
    after since_ts, base is its first sample (count from install). Returns
    (0, 0) when the node has no usable samples yet.
    """
    row = conn.execute(
        "SELECT rx_total, tx_total FROM samples"
        " WHERE node=? AND ts<=? AND rx_total IS NOT NULL"
        " ORDER BY ts DESC LIMIT 1", (node, since_ts)).fetchone()
    if row is not None:
        base_rx, base_tx = row
    else:
        first = conn.execute(
            "SELECT rx_total, tx_total FROM samples"
            " WHERE node=? AND rx_total IS NOT NULL"
            " ORDER BY ts ASC LIMIT 1", (node,)).fetchone()
        if first is None:
            return (0, 0)
        base_rx, base_tx = first
    latest = conn.execute(
        "SELECT rx_total, tx_total FROM samples"
        " WHERE node=? AND rx_total IS NOT NULL"
        " ORDER BY ts DESC LIMIT 1", (node,)).fetchone()
    if latest is None:
        return (0, 0)
    return (max(0, latest[0] - base_rx), max(0, latest[1] - base_tx))


def apply_mode(rx, tx, mode):
    if mode == "out":
        return tx
    if mode == "in":
        return rx
    return rx + tx


def node_windows(conn, node, cycle_day, count_mode):
    now = int(time.time())
    out = {}
    for name, since in (("month", cycle_start_ts(now, cycle_day)),
                        ("week", now - 7 * 86400),
                        ("day", now - 86400),
                        ("hour", now - 3600)):
        rx, tx = window_total(conn, node, since)
        out[name] = {"rx": rx, "tx": tx, "total": apply_mode(rx, tx, count_mode),
                     "since": since}
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, query):
        if self.headers.get("X-Token") == TOKEN:
            return True
        return query.get("token", [None])[0] == TOKEN

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            return self._send(200, {"ok": True, "ts": int(time.time())})
        if not TOKEN:
            return self._send(500, {"error": "hub TOKEN not configured"})
        if not self._auth(query):
            return self._send(401, {"error": "unauthorized"})
        if parsed.path == "/api/stats":
            conn = db()
            rows = conn.execute(
                "SELECT node, iface, last_seen, rx_total, tx_total,"
                " rx_rate, tx_rate, display_name, cycle_day, count_mode"
                " FROM nodes ORDER BY node").fetchall()
            nodes = []
            total_month = 0
            for r in rows:
                node, iface, last_seen = r[0], r[1], r[2]
                rx_total, tx_total = r[3] or 0, r[4] or 0
                cycle_day = r[8] or 1
                count_mode = r[9] or "both"
                windows = node_windows(conn, node, cycle_day, count_mode)
                total_month += windows["month"]["total"]
                nodes.append({
                    "node": node,
                    "display_name": r[7] or node,
                    "iface": iface, "last_seen": last_seen,
                    "rx_total": rx_total, "tx_total": tx_total,
                    "rx_rate": r[5], "tx_rate": r[6],
                    "cycle_day": cycle_day, "count_mode": count_mode,
                    "month": windows["month"], "week": windows["week"],
                    "day": windows["day"], "hour": windows["hour"],
                })
            conn.close()
            return self._send(200, {"generated_at": int(time.time()),
                                    "nodes": nodes,
                                    "total_month": total_month})
        if parsed.path == "/api/history":
            node = query.get("node", [None])[0]
            hours = int(query.get("hours", ["24"])[0])
            if not node:
                return self._send(400, {"error": "node is required"})
            since = int(time.time()) - hours * 3600
            conn = db()
            rows = conn.execute(
                "SELECT ts, rx_total, tx_total, rx_rate, tx_rate"
                " FROM samples WHERE node=? AND ts>=? AND rx_total IS NOT NULL"
                " ORDER BY ts", (node, since)).fetchall()
            conn.close()
            return self._send(200, {"node": node, "samples": [
                {"ts": r[0], "rx_total": r[1], "tx_total": r[2],
                 "rx_rate": r[3], "tx_rate": r[4]} for r in rows]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/push":
            if not TOKEN or self.headers.get("X-Token") != TOKEN:
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                node = str(data["node"])
                ts = int(data.get("ts") or time.time())
                iface = str(data.get("iface") or "")
                rx_rate = float(data.get("rx_rate") or 0)
                tx_rate = float(data.get("tx_rate") or 0)
                # v2 agents send cumulative totals; v1 agents sent monthly
                # totals, which are accepted as-is (their monthly reset will
                # show up as a small step in the series, not a crash).
                rx_total = data.get("rx_total", data.get("rx_month"))
                tx_total = data.get("tx_total", data.get("tx_month"))
                rx_total = int(rx_total)
                tx_total = int(tx_total)
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "bad payload"})
            conn = db()
            conn.execute(
                "INSERT INTO nodes(node, iface, last_seen, rx_total, tx_total,"
                " rx_rate, tx_rate) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(node) DO UPDATE SET iface=excluded.iface,"
                " last_seen=excluded.last_seen, rx_total=excluded.rx_total,"
                " tx_total=excluded.tx_total, rx_rate=excluded.rx_rate,"
                " tx_rate=excluded.tx_rate",
                (node, iface, ts, rx_total, tx_total, rx_rate, tx_rate))
            conn.execute(
                "INSERT INTO samples(node, ts, rx_total, tx_total, rx_rate,"
                " tx_rate) VALUES(?,?,?,?,?,?)",
                (node, ts, rx_total, tx_total, rx_rate, tx_rate))
            conn.commit()
            prune(conn)
            conn.close()
            return self._send(200, {"ok": True})
        if parsed.path == "/api/node":
            if not TOKEN or self.headers.get("X-Token") != TOKEN:
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                node = str(data["node"])
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "bad payload"})
            updates, params = [], []
            if "display_name" in data:
                updates.append("display_name=?")
                params.append(str(data["display_name"])[:64] or None)
            if "cycle_day" in data:
                day = int(data["cycle_day"])
                if not 1 <= day <= 31:
                    return self._send(400, {"error": "cycle_day must be 1-31"})
                updates.append("cycle_day=?")
                params.append(day)
            if "count_mode" in data:
                mode = str(data["count_mode"])
                if mode not in ("both", "out", "in"):
                    return self._send(400, {"error": "count_mode must be both/out/in"})
                updates.append("count_mode=?")
                params.append(mode)
            if not updates:
                return self._send(400, {"error": "nothing to update"})
            conn = db()
            exists = conn.execute(
                "SELECT 1 FROM nodes WHERE node=?", (node,)).fetchone()
            if not exists:
                conn.close()
                return self._send(404, {"error": "unknown node"})
            params.append(node)
            conn.execute(
                f"UPDATE nodes SET {', '.join(updates)} WHERE node=?", params)
            conn.commit()
            conn.close()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main():
    if not TOKEN:
        raise SystemExit("hub: TOKEN is required "
                         "(set in /etc/vps-traffic/hub.conf or env)")
    db().close()
    port = int(CFG["PORT"])
    server = ThreadingHTTPServer((CFG["HOST"], port), Handler)
    print(f"hub: listening on {CFG['HOST']}:{port}, db={DB_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
