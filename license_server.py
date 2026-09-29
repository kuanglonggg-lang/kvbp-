#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
license_server.py —— 最小可用的 EA 授权服务（只用 Python 标准库，零依赖）

设计目标：替换掉「登录器 + 设备指纹 + Ed25519 + DPAPI + 加密盘 + DLL 注入」那一整套，
只保留 3 件事：
  1) 一个 key  → 一个 MT5 账号（首次激活时绑定）
  2) 到期日    → 服务端说了算，并用 HMAC 签名，客户端改不了
  3) 心跳      → EA 定期回报，断了给宽限，久了就停手

协议（故意做成一行文本，MQL5 侧不用写 JSON 解析）：
  GET/POST /api/v1/activate   参数: key product account broker terminal version
  POST     /api/v1/heartbeat  同上
  POST     /api/v1/deactivate 参数: key account        （自助解绑 / 换机）
  GET      /health
  应答:  v1|OK|<exp>|<sig>
         v1|ERR|<CODE>
  sig = HMAC_SHA256(secret, "product|account|exp")  的小写 hex

用法：
  python license_server.py init
  python license_server.py issue --product GridMaster --days 365 --note "张三 50109999"
  python license_server.py list
  python license_server.py unbind --key XXXX-XXXX
  python license_server.py serve --host 0.0.0.0 --port 8787
  python license_server.py serve --host 0.0.0.0 --port 8787 --admin-token XXX   # 开 HTTP 发卡
"""

import argparse
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import socketserver
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "licenses.db")
DEFAULT_SECRET = "CHANGE_ME_please_run_server_with_same_secret"
API_VERSION = "v1"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉 I O 0 1，避免手抄错


# ---------------------------------------------------------------- 基础设施
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS licenses (
            key        TEXT PRIMARY KEY,
            product    TEXT NOT NULL,
            exp        TEXT NOT NULL,          -- yyyymmdd
            account    TEXT DEFAULT '',        -- 绑定的 MT5 账号（空=未绑定）
            broker     TEXT DEFAULT '',
            status     TEXT DEFAULT 'active',  -- active / revoked
            note       TEXT DEFAULT '',
            created    TEXT,
            last_seen  TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT,
            key    TEXT,
            account TEXT,
            event  TEXT,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_key ON events(key);
        """
    )
    conn.commit()
    conn.close()


def log_event(conn, key, account, event, detail=""):
    conn.execute(
        "INSERT INTO events(ts,key,account,event,detail) VALUES(?,?,?,?,?)",
        (now_str(), key or "", account or "", event, detail or ""),
    )
    conn.commit()


def new_key():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))   # XXXX-XXXX-XXXX-XXXX


def sign(secret, product, account, exp):
    msg = "%s|%s|%s" % (product, account, exp)
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def ok_body(secret, product, account, exp):
    return "%s|OK|%s|%s" % (API_VERSION, exp, sign(secret, product, account, exp))


def err_body(code):
    return "%s|ERR|%s" % (API_VERSION, code)


def today():
    return datetime.now().strftime("%Y%m%d")


# ---------------------------------------------------------------- 业务逻辑
def do_activate(secret, product, key, account, broker, terminal, version, bind=True):
    """返回 (http_status, body)"""
    if not key or not product or not account:
        return 400, err_body("BAD_REQUEST")
    conn = db()
    try:
        row = conn.execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
        if row is None:
            log_event(conn, key, account, "activate_denied", "unknown_key")
            return 404, err_body("UNKNOWN_KEY")
        if row["status"] != "active":
            log_event(conn, key, account, "activate_denied", "revoked")
            return 403, err_body("REVOKED")
        if row["product"] != product:
            log_event(conn, key, account, "activate_denied", "product_mismatch")
            return 403, err_body("PRODUCT_MISMATCH")
        if row["exp"] < today():
            log_event(conn, key, account, "activate_denied", "expired:" + row["exp"])
            return 403, err_body("EXPIRED")

        bound = (row["account"] or "").strip()
        if bound and bound != account:
            # 已绑到别的账号。想换机必须先解绑（dashboard / CLI）。
            log_event(conn, key, account, "activate_denied", "bound_to:" + bound)
            return 403, err_body("ACCOUNT_MISMATCH")
        if not bound and bind:
            conn.execute(
                "UPDATE licenses SET account=?, broker=?, last_seen=? WHERE key=?",
                (account, broker or "", now_str(), key),
            )
            conn.commit()
            log_event(conn, key, account, "bound", "broker=%s terminal=%s ver=%s" % (broker, terminal, version))
        else:
            conn.execute("UPDATE licenses SET last_seen=?, broker=? WHERE key=?", (now_str(), broker or row["broker"], key))
            conn.commit()
            log_event(conn, key, account, "activate_ok", "broker=%s" % broker)

        return 200, ok_body(secret, row["product"], account, row["exp"])
    finally:
        conn.close()


def do_heartbeat(secret, product, key, account, version):
    if not key or not product or not account:
        return 400, err_body("BAD_REQUEST")
    conn = db()
    try:
        row = conn.execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
        if row is None:
            return 404, err_body("UNKNOWN_KEY")
        if row["status"] != "active":
            log_event(conn, key, account, "heartbeat_denied", "revoked")
            return 403, err_body("REVOKED")
        if row["product"] != product:
            return 403, err_body("PRODUCT_MISMATCH")
        if (row["account"] or "").strip() and row["account"] != account:
            return 403, err_body("ACCOUNT_MISMATCH")
        if row["exp"] < today():
            log_event(conn, key, account, "heartbeat_denied", "expired")
            return 403, err_body("EXPIRED")
        conn.execute("UPDATE licenses SET last_seen=? WHERE key=?", (now_str(), key))
        conn.commit()
        return 200, ok_body(secret, row["product"], account, row["exp"])
    finally:
        conn.close()


def do_deactivate(key, account):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
        if row is None:
            return 404, err_body("UNKNOWN_KEY")
        if (row["account"] or "").strip() and row["account"] != account:
            log_event(conn, key, account, "deactivate_denied", "not_owner")
            return 403, err_body("ACCOUNT_MISMATCH")
        conn.execute("UPDATE licenses SET account='', broker='' WHERE key=?", (key,))
        conn.commit()
        log_event(conn, key, account, "unbound", "self_service")
        return 200, "%s|OK|UNBOUND|" % API_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    secret = DEFAULT_SECRET
    admin_token = None
    server_version = "LicSrv/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_str(), fmt % args))

    # --- 工具
    def _params(self):
        parsed = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "json" in ctype and raw:
                try:
                    q.update(json.loads(raw.decode("utf-8", "replace")))
                except Exception:
                    pass
            elif raw:
                q.update(dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace"))))
        return parsed.path, q

    def _send(self, status, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _dispatch(self):
        path, p = self._params()
        p = {k: (v.strip() if isinstance(v, str) else v) for k, v in p.items()}
        key = p.get("key", "")
        product = p.get("product", "")
        account = re.sub(r"\D", "", str(p.get("account", "")))   # 只留数字

        if path == "/health":
            return self._send(200, "%s|OK|%s|" % (API_VERSION, today()))

        if path == "/api/v1/activate":
            st, body = do_activate(self.secret, product, key, account,
                                   p.get("broker", ""), p.get("terminal", ""), p.get("version", ""))
            return self._send(st, body)

        if path == "/api/v1/heartbeat":
            st, body = do_heartbeat(self.secret, product, key, account, p.get("version", ""))
            return self._send(st, body)

        if path == "/api/v1/deactivate":
            st, body = do_deactivate(key, account)
            return self._send(st, body)

        if path == "/api/v1/admin/issue":
            if not self.admin_token:
                return self._send(404, err_body("NOT_FOUND"))
            got = self.headers.get("X-Admin-Token") or p.get("token", "")
            if got != self.admin_token:
                return self._send(403, err_body("FORBIDDEN"))
            days = int(p.get("days") or 365)
            k = issue(product or "Default", days, p.get("note", ""))
            return self._send(200, json.dumps({"key": k, "product": product, "days": days,
                                               "exp": (datetime.now() + timedelta(days=days)).strftime("%Y%m%d")},
                                              ensure_ascii=False),
                              ctype="application/json; charset=utf-8")

        return self._send(404, err_body("NOT_FOUND"))

    do_GET = _dispatch
    do_POST = _dispatch


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host, port, secret, admin_token=None):
    Handler.secret = secret
    Handler.admin_token = admin_token
    if not os.path.exists(DB):
        init_db()
    httpd = ThreadingServer((host, port), Handler)
    print("授权服务已启动: http://%s:%d" % (host, port))
    print("  secret         : %s%s" % (secret[:6], "*" * max(0, len(secret) - 6)))
    print("  发卡接口       : %s" % ("已开启 (X-Admin-Token)" if admin_token else "关闭"))
    print("  自检           : curl http://127.0.0.1:%d/health" % port)
    print("  EA 里要填的 URL: http://<你的公网域名或IP>:%d" % port)
    print("  记得把上面这个 host 加进 MT5 的 工具→选项→EA→允许 WebRequest 的 URL")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


# ---------------------------------------------------------------- CLI
def issue(product, days, note, count=1):
    init_db()
    conn = db()
    exp = (datetime.now() + timedelta(days=int(days))).strftime("%Y%m%d")
    made = []
    for _ in range(count):
        k = new_key()
        conn.execute(
            "INSERT INTO licenses(key,product,exp,status,note,created) VALUES(?,?,?,'active',?,?)",
            (k, product, exp, note or "", now_str()),
        )
        made.append(k)
    conn.commit()
    log_event(conn, made[0] if len(made) == 1 else "*", "", "issue", "product=%s days=%s n=%d" % (product, days, len(made)))
    conn.close()
    for k in made:
        print(k)
    return made[0]


def cmd_list(_):
    init_db()
    conn = db()
    rows = conn.execute("SELECT * FROM licenses ORDER BY created DESC").fetchall()
    if not rows:
        print("(还没有授权码)")
    print("%-21s %-14s %-10s %-12s %-9s %-8s %s" % ("KEY", "PRODUCT", "EXP", "ACCOUNT", "STATUS", "LAST", "NOTE"))
    print("-" * 104)
    for r in rows:
        print("%-21s %-14s %-10s %-12s %-9s %-8s %s" % (
            r["key"], r["product"], r["exp"], r["account"] or "-", r["status"],
            (r["last_seen"] or "")[-8:], r["note"] or ""))
    conn.close()


def cmd_revoke(key):
    conn = db()
    conn.execute("UPDATE licenses SET status='revoked' WHERE key=?", (key,))
    conn.commit()
    log_event(conn, key, "", "revoke", "cli")
    conn.close()
    print("已吊销", key)


def cmd_unbind(key):
    conn = db()
    conn.execute("UPDATE licenses SET account='', broker='' WHERE key=?", (key,))
    conn.commit()
    log_event(conn, key, "", "unbound", "cli")
    conn.close()
    print("已解绑", key, "（可以换到新账号了）")


def cmd_extend(key, days):
    conn = db()
    row = conn.execute("SELECT exp,product FROM licenses WHERE key=?", (key,)).fetchone()
    if not row:
        print("没有这个 key"); conn.close(); return
    base = datetime.strptime(row["exp"], "%Y%m%d")
    if base < datetime.now():
        base = datetime.now()
    new_exp = (base + timedelta(days=int(days))).strftime("%Y%m%d")
    conn.execute("UPDATE licenses SET exp=? WHERE key=?", (new_exp, key))
    conn.commit()
    log_event(conn, key, "", "extend", "->" + new_exp)
    conn.close()
    print("续期到", new_exp)


def main():
    ap = argparse.ArgumentParser(description="最小 EA 授权服务")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("init")

    p = sub.add_parser("issue"); p.add_argument("--product", required=True); p.add_argument("--days", default=365); p.add_argument("--note", default=""); p.add_argument("--count", default=1)
    p = sub.add_parser("list")
    p = sub.add_parser("revoke"); p.add_argument("--key", required=True)
    p = sub.add_parser("unbind"); p.add_argument("--key", required=True)
    p = sub.add_parser("extend"); p.add_argument("--key", required=True); p.add_argument("--days", default=30)
    p = sub.add_parser("serve"); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", default=8787); p.add_argument("--secret", default=DEFAULT_SECRET); p.add_argument("--admin-token", default=None)

    a = ap.parse_args()
    if a.cmd == "init":
        init_db(); print("已初始化", DB)
    elif a.cmd == "issue":
        issue(a.product, a.days, a.note, int(a.count))
    elif a.cmd == "list":
        cmd_list(a)
    elif a.cmd == "revoke":
        cmd_revoke(a.key)
    elif a.cmd == "unbind":
        cmd_unbind(a.key)
    elif a.cmd == "extend":
        cmd_extend(a.key, a.days)
    elif a.cmd == "serve":
        serve(a.host, int(a.port), a.secret, a.admin_token)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
