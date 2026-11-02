#!/usr/bin/env python3
"""Small root-only bridge between RDnet Bot and Alex Server VPN tools."""
import base64
import grp
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB = "/var/lib/rdnet-vpn-stats/stats.db"
NAME = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def fail(message, code=1):
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def valid(value, label="name"):
    if not NAME.fullmatch(value or ""):
        fail(f"invalid {label}: use A-Z, a-z, 0-9, dot, dash or underscore")
    return value


def run(args, stdin=None):
    p = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=20)
    if p.returncode:
        fail((p.stderr or p.stdout or "command failed").strip())
    return p.stdout


def conn():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS wg_totals(
      name TEXT PRIMARY KEY, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0,
      last_rx INTEGER NOT NULL DEFAULT 0, last_tx INTEGER NOT NULL DEFAULT 0,
      last_handshake INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS l2tp_totals(
      name TEXT PRIMARY KEY, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0,
      sessions INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS l2tp_active(
      interface TEXT PRIMARY KEY, name TEXT NOT NULL, started_at INTEGER NOT NULL);
    """)
    c.commit()
    os.chown(DB, 0, grp.getgrnam("rdnet-bot").gr_gid)
    os.chmod(DB, 0o640)
    return c


def wg_users():
    conf = Path("/etc/wireguard/wg-clients.conf").read_text()
    result, name = {}, None
    for line in conf.splitlines():
        if line.startswith("# name="):
            name = line.split("=", 1)[1].strip()
        elif name and line.startswith("PublicKey ="):
            result[line.split("=", 1)[1].strip()] = name
            name = None
    return result


def sample_wg():
    users = wg_users()
    rows = run(["wg", "show", "wg-clients", "dump"]).splitlines()[1:]
    now = int(time.time())
    live = {}
    with conn() as c:
        for line in rows:
            f = line.split("\t")
            if len(f) < 8 or f[0] not in users:
                continue
            name, hs, rx, tx = users[f[0]], int(f[4]), int(f[5]), int(f[6])
            old = c.execute("SELECT * FROM wg_totals WHERE name=?", (name,)).fetchone()
            add_rx = rx if not old or rx < old["last_rx"] else rx - old["last_rx"]
            add_tx = tx if not old or tx < old["last_tx"] else tx - old["last_tx"]
            c.execute("""INSERT INTO wg_totals(name,rx,tx,last_rx,last_tx,last_handshake,updated_at)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
              rx=rx+excluded.rx,tx=tx+excluded.tx,last_rx=excluded.last_rx,last_tx=excluded.last_tx,
              last_handshake=excluded.last_handshake,updated_at=excluded.updated_at""",
              (name, add_rx, add_tx, rx, tx, hs, now))
            live[name] = {"handshake": hs, "online": bool(hs and now - hs <= 180)}
        c.commit()
    return live


def l2tp_users():
    users = []
    for line in Path("/etc/ppp/chap-secrets").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.replace('"', '').split()
        if len(fields) >= 3 and fields[1] == "l2tpd":
            users.append(fields[0])
    return sorted(users, key=str.lower)


def iface_bytes(iface):
    try:
        rx = int(Path(f"/sys/class/net/{iface}/statistics/rx_bytes").read_text())
        tx = int(Path(f"/sys/class/net/{iface}/statistics/tx_bytes").read_text())
        return rx, tx
    except (OSError, ValueError):
        return 0, 0


def emit(data):
    print(json.dumps({"ok": True, **data}, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        fail("missing operation")
    op, args = sys.argv[1], sys.argv[2:]
    if op == "l2tp-add" and len(args) == 2:
        valid(args[0], "username")
        run(["/usr/local/sbin/l2tp-user", "add", args[0], args[1]])
        emit({"name": args[0]})
    elif op == "l2tp-del" and len(args) == 1:
        valid(args[0]); run(["/usr/local/sbin/l2tp-user", "del", args[0]])
        emit({"name": args[0]})
    elif op == "l2tp-list" and not args:
        emit({"users": l2tp_users()})
    elif op == "l2tp-status" and not args:
        now, result = int(time.time()), []
        with conn() as c:
            active = {r["name"]: r for r in c.execute("SELECT * FROM l2tp_active")}
            totals = {r["name"]: r for r in c.execute("SELECT * FROM l2tp_totals")}
            for name in l2tp_users():
                a, t = active.get(name), totals.get(name)
                crx, ctx = iface_bytes(a["interface"]) if a else (0, 0)
                result.append({"name": name, "online": bool(a),
                  "connected": now-a["started_at"] if a else 0,
                  "rx": (t["rx"] if t else 0)+crx, "tx": (t["tx"] if t else 0)+ctx,
                  "last_seen": t["last_seen"] if t else 0})
        emit({"users": result})
    elif op == "l2tp-up" and len(args) == 2:
        valid(args[0], "interface"); valid(args[1], "username")
        with conn() as c:
            c.execute("INSERT OR REPLACE INTO l2tp_active(interface,name,started_at) VALUES(?,?,?)", (args[0], args[1], int(time.time()))); c.commit()
        emit({"name": args[1]})
    elif op == "l2tp-down" and len(args) == 2:
        valid(args[0], "interface"); valid(args[1], "username")
        rx, tx = iface_bytes(args[0]); now = int(time.time())
        with conn() as c:
            c.execute("""INSERT INTO l2tp_totals(name,rx,tx,sessions,last_seen) VALUES(?,?,?,?,?)
              ON CONFLICT(name) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx,
              sessions=sessions+1,last_seen=excluded.last_seen""", (args[1], rx, tx, 1, now))
            c.execute("DELETE FROM l2tp_active WHERE interface=?", (args[0],)); c.commit()
        emit({"name": args[1]})
    elif op == "wg-add" and len(args) == 1:
        name = valid(args[0]); run(["/usr/local/sbin/wg-user", "add", name])
        config = Path(f"/etc/alex-server/wg-clients/{name}.conf").read_text()
        qr = subprocess.run(["qrencode", "-t", "PNG", "-o", "-"], input=config.encode(), capture_output=True, timeout=10)
        if qr.returncode: fail("could not create QR")
        sample_wg(); emit({"name": name, "config": config, "qr": base64.b64encode(qr.stdout).decode()})
    elif op == "wg-del" and len(args) == 1:
        name = valid(args[0]); sample_wg(); run(["/usr/local/sbin/wg-user", "del", name]); emit({"name": name})
    elif op == "wg-list" and not args:
        emit({"users": sorted(wg_users().values(), key=str.lower)})
    elif op == "wg-status" and not args:
        live = sample_wg(); result = []
        with conn() as c:
            totals = {r["name"]: r for r in c.execute("SELECT * FROM wg_totals")}
            for name in sorted(wg_users().values(), key=str.lower):
                t, li = totals.get(name), live.get(name, {})
                result.append({"name": name, "online": li.get("online", False),
                  "handshake": li.get("handshake", t["last_handshake"] if t else 0),
                  "rx": t["rx"] if t else 0, "tx": t["tx"] if t else 0})
        emit({"users": result})
    elif op == "wg-sample" and not args:
        sample_wg(); emit({})
    else:
        fail("unsupported operation or arguments")


if __name__ == "__main__":
    main()




