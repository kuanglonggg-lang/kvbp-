#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py —— 端到端自检：起服务 → 发卡 → 激活 → 心跳 → 各种拒绝路径"""
import hashlib
import hmac
import http.client
import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import license_server as L

TEST_DB = os.path.join(HERE, "licenses.db")
SECRET = L.DEFAULT_SECRET
PORT = 8788


def req(path, params=None, token=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=8)
    url = path
    body = None
    headers = {}
    if method == "GET":
        from urllib.parse import urlencode
        if params:
            url += "?" + urlencode(params)
    else:
        import json
        body = json.dumps(params or {}, ensure_ascii=False)
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    conn.request(method, url, body=body, headers=headers)
    r = conn.getresponse()
    data = r.read().decode("utf-8", "replace")
    conn.close()
    return r.status, data


def verdict(name, got, want_sub):
    ok = want_sub in got
    print("  %-46s %s   %s" % (name, "PASS" if ok else "FAIL", got[:90]))
    return ok


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    L.init_db()

    srv = L.ThreadingServer(("127.0.0.1", PORT), L.Handler)
    L.Handler.secret = SECRET
    L.Handler.admin_token = "admintok"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    time.sleep(0.4)

    allok = True
    print("=== 1. 健康检查 ===")
    st, body = req("/health")
    allok &= verdict("GET /health", body, "|OK|")

    print("=== 2. 发卡 ===")
    key = L.issue("GridMaster", 365, "selftest")
    print("  新 key =", key)
    allok &= verdict("key 格式 XXXX-XXXX-XXXX-XXXX", key, "-")

    print("=== 3. 管理员 HTTP 发卡 ===")
    st, body = req("/api/v1/admin/issue", {"product": "GridMaster", "days": 30}, token="admintok")
    allok &= verdict("POST /admin/issue (带 token)", body, '"key"')
    st, body = req("/api/v1/admin/issue", {"product": "X"}, token="wrong")
    allok &= verdict("POST /admin/issue (错 token 应 403)", body, "FORBIDDEN")

    print("=== 4. 正常激活 + 验签 ===")
    acc = "50109999"
    st, body = req("/api/v1/activate", {"key": key, "product": "GridMaster", "account": acc,
                                        "broker": "KVBPrimeLimited-Real", "terminal": "KVB", "version": "6231"})
    allok &= verdict("activate → v1|OK|...", body, "|OK|")
    parts = body.split("|")
    if len(parts) == 4:
        exp, sig = parts[2], parts[3]
        want = hmac.new(SECRET.encode(), ("GridMaster|%s|%s" % (acc, exp)).encode(),
                        hashlib.sha256).hexdigest()
        print("  %-46s %s" % ("HMAC 验签与 Python 一致", "PASS" if sig == want else "FAIL"))
        allok &= (sig == want)
    else:
        allok = False

    print("=== 5. 心跳 ===")
    st, body = req("/api/v1/heartbeat", {"key": key, "product": "GridMaster", "account": acc})
    allok &= verdict("heartbeat → OK", body, "|OK|")

    print("=== 6. 拒绝路径 ===")
    st, body = req("/api/v1/activate", {"key": "AAAA-BBBB-CCCC-DDDD", "product": "GridMaster", "account": acc})
    allok &= verdict("不存在的 key → UNKNOWN_KEY", body, "UNKNOWN_KEY")
    st, body = req("/api/v1/activate", {"key": key, "product": "OtherProduct", "account": acc})
    allok &= verdict("产品不符 → PRODUCT_MISMATCH", body, "PRODUCT_MISMATCH")
    st, body = req("/api/v1/activate", {"key": key, "product": "GridMaster", "account": "50999999"})
    allok &= verdict("换账号 → ACCOUNT_MISMATCH", body, "ACCOUNT_MISMATCH")

    print("=== 7. 解绑后换机 ===")
    st, body = req("/api/v1/deactivate", {"key": key, "account": acc})
    allok &= verdict("deactivate → UNBOUND", body, "UNBOUND")
    st, body = req("/api/v1/activate", {"key": key, "product": "GridMaster", "account": "50999999"})
    allok &= verdict("换到新账号 → OK", body, "|OK|")

    print("=== 8. 到期 / 吊销 ===")
    k2 = L.issue("GridMaster", -1, "expired")            # 昨天到期
    st, body = req("/api/v1/activate", {"key": k2, "product": "GridMaster", "account": acc})
    allok &= verdict("过期 key → EXPIRED", body, "EXPIRED")
    k3 = L.issue("GridMaster", 30, "to revoke")
    L.cmd_revoke(k3)
    st, body = req("/api/v1/activate", {"key": k3, "product": "GridMaster", "account": acc})
    allok &= verdict("吊销 key → REVOKED", body, "REVOKED")

    print("=== 9. 续期 ===")
    st, body = req("/api/v1/activate", {"key": k2, "product": "GridMaster", "account": acc})
    allok &= verdict("续期前仍是 EXPIRED", body, "EXPIRED")
    L.cmd_extend(k2, 30)
    st, body = req("/api/v1/activate", {"key": k2, "product": "GridMaster", "account": acc})
    allok &= verdict("续期后 → OK", body, "|OK|")

    srv.shutdown()
    print()
    print("总体：%s" % ("全部 PASS" if allok else "有 FAIL"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
