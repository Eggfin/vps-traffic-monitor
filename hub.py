#!/usr/bin/env python3
"""VPS traffic hub — stdlib only, no dependencies.

Receives samples from agents (POST /api/push, X-Token auth), stores them
in SQLite, and serves the dashboard API (GET /api/stats, /api/history).

Config: /etc/vps-traffic/hub.conf, then env vars.
  TOKEN  shared secret, must match the agents (required)
  PORT   listen port, default 18080
  DB     sqlite path, default /var/lib/vps-traffic/traffic.db
  HOST   bind address, default 0.0.0.0
"""
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CONFIG_FILE = "/etc/vps-traffic/hub.conf"


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
        rx_month INTEGER, tx_month INTEGER,
        rx_rate REAL, tx_rate REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT, ts INTEGER,
        rx_month INTEGER, tx_month INTEGER, rx_rate REAL, tx_rate REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_node_ts "
                 "ON samples(node, ts)")
    return conn


def prune(conn):
    cutoff = int(time.time()) - 7 * 86400  # keep 7 days of samples
    conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    conn.commit()


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
                "SELECT node, iface, last_seen, rx_month, tx_month,"
                " rx_rate, tx_rate FROM nodes ORDER BY node").fetchall()
            conn.close()
            nodes = [{"node": r[0], "iface": r[1], "last_seen": r[2],
                      "rx_month": r[3], "tx_month": r[4],
                      "total_month": r[3] + r[4],
                      "rx_rate": r[5], "tx_rate": r[6]} for r in rows]
            total = sum(n["total_month"] for n in nodes)
            return self._send(200, {"generated_at": int(time.time()),
                                    "nodes": nodes, "total_month": total})
        if parsed.path == "/api/history":
            node = query.get("node", [None])[0]
            hours = int(query.get("hours", ["24"])[0])
            if not node:
                return self._send(400, {"error": "node is required"})
            since = int(time.time()) - hours * 3600
            conn = db()
            rows = conn.execute(
                "SELECT ts, rx_month, tx_month, rx_rate, tx_rate"
                " FROM samples WHERE node=? AND ts>=? ORDER BY ts",
                (node, since)).fetchall()
            conn.close()
            return self._send(200, {"node": node, "samples": [
                {"ts": r[0], "rx_month": r[1], "tx_month": r[2],
                 "rx_rate": r[3], "tx_rate": r[4]} for r in rows]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/push":
            return self._send(404, {"error": "not found"})
        if not TOKEN or self.headers.get("X-Token") != TOKEN:
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            node = str(data["node"])
            rx_month = int(data["rx_month"])
            tx_month = int(data["tx_month"])
            rx_rate = float(data.get("rx_rate") or 0)
            tx_rate = float(data.get("tx_rate") or 0)
            ts = int(data.get("ts") or time.time())
            iface = str(data.get("iface") or "")
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"error": "bad payload"})
        conn = db()
        conn.execute(
            "INSERT INTO nodes(node, iface, last_seen, rx_month, tx_month,"
            " rx_rate, tx_rate) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(node) DO UPDATE SET iface=excluded.iface,"
            " last_seen=excluded.last_seen, rx_month=excluded.rx_month,"
            " tx_month=excluded.tx_month, rx_rate=excluded.rx_rate,"
            " tx_rate=excluded.tx_rate",
            (node, iface, ts, rx_month, tx_month, rx_rate, tx_rate))
        conn.execute(
            "INSERT INTO samples(node, ts, rx_month, tx_month, rx_rate,"
            " tx_rate) VALUES(?,?,?,?,?,?)",
            (node, ts, rx_month, tx_month, rx_rate, tx_rate))
        conn.commit()
        prune(conn)
        conn.close()
        return self._send(200, {"ok": True})


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
