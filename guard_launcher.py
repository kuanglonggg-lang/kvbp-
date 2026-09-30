#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guard_launcher.py —— 授权启动器（给「无源码加密 ex5」加限制的②号方案）

它在客户机上做的事，只有三件，全是"环境级"，不碰 ex5、不注入、不写 PE：

  1) 读终端数据目录里的 config\\common.ini，取出当前登录的【账号号】
  2) 调 /api/v1/activate 做 账号绑定 + 到期 + 吊销 三重校验（HMAC-SHA256 验签）
  3) 通过才拉起 MT5；之后每 N 分钟心跳一次，一旦被吊销/过期 → 优雅关闭终端

优点：不受 build / CPU 指令集 / 杀软 影响（这三样正是运行期注入的三大死因）
缺点：客户能绕过（自己双击 terminal64.exe）。所以它是"劝退层"，不是"锁"。

用法：
    python guard_launcher.py --config guard.json            # 正常：校验通过就启动
    python guard_launcher.py --config guard.json --check    # 只校验不启动（排障）
    python guard_launcher.py --config guard.json --account 12345678   # 手动指定账号

guard.json 示例见 CONFIG_SAMPLE（或同目录 guard.sample.json）
"""

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from ctypes import wintypes

API_VERSION = "v1"
TERMINAL_DIRS = [
    r"C:\Program Files\MetaTrader 5",
    r"C:\Program Files\KVB Prime MT5 Terminal",
    r"C:\Program Files\MetaTrader 5 EXNESS",
]
DATA_ROOT = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal")

CONFIG_SAMPLE = {
    "url": "http://your.host:8787",
    "secret": "CHANGE_ME_please_run_server_with_same_secret",
    "product": "JinJi51",
    "key": "XXXX-XXXX-XXXX-XXXX",
    "terminal_exe": "",          # 留空 = 自动找
    "heartbeat_min": 10,
    "grace_min": 30,             # 心跳连续失败多久才关终端（防网络抖动误杀）
    "on_revoke": "close",        # close = 关终端 / warn = 只提示
}


# ----------------------------------------------------------------- 工具
def read_ini_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-16", "utf-8-sig", "utf-8", "gbk"):
        try:
            t = raw.decode(enc)
            if t.count("\x00") > 3:
                continue
            return t
        except Exception:
            continue
    return ""


def read_account_from_datadir(datadir):
    ini = os.path.join(datadir, "config", "common.ini")
    if not os.path.exists(ini):
        return None
    m = re.search(r"(?m)^Login=(\d+)", read_ini_text(ini))
    return m.group(1) if m else None


def find_datadirs():
    """返回 [(datadir, 安装目录, Login)]"""
    out = []
    if not os.path.isdir(DATA_ROOT):
        return out
    for d in os.listdir(DATA_ROOT):
        p = os.path.join(DATA_ROOT, d)
        if not os.path.isdir(p) or d in ("Common", "Community", "Help"):
            continue
        org = os.path.join(p, "origin.txt")
        origin = read_ini_text(org).strip() if os.path.exists(org) else ""
        login = read_account_from_datadir(p)
        out.append((p, origin, login))
    return out


def pick_terminal_exe(cfg, datadir=None):
    if cfg.get("terminal_exe") and os.path.exists(cfg["terminal_exe"]):
        return cfg["terminal_exe"]
    if datadir:
        org = os.path.join(datadir, "origin.txt")
        if os.path.exists(org):
            exe = os.path.join(read_ini_text(org).strip(), "terminal64.exe")
            if os.path.exists(exe):
                return exe
    for d in TERMINAL_DIRS:
        exe = os.path.join(d, "terminal64.exe")
        if os.path.exists(exe):
            return exe
    return None


def sign(secret, product, account, exp):
    msg = "%s|%s|%s" % (product, account, exp)
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def http_get(url, timeout=8):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "GuardLauncher/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"").decode("utf-8", "replace").strip()
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


def call(cfg, endpoint, account, extra=""):
    q = urllib.parse.urlencode({
        "product": cfg["product"], "key": cfg["key"], "account": account,
        "version": "1.0", "broker": "", "terminal": "",
    })
    url = cfg["url"].rstrip("/") + endpoint + "?" + q + extra
    return http_get(url)


def verify_ok(body, secret, product, account):
    """校验服务端应答：v1|OK|exp|sig"""
    parts = (body or "").split("|")
    if len(parts) != 4 or parts[0] != API_VERSION or parts[1] != "OK":
        return None, (parts[2] if len(parts) > 2 else body)
    exp, sig = parts[2], parts[3]
    want = sign(secret, product, account, exp)
    if not hmac.compare_digest(sig.lower(), want.lower()):
        return None, "SIG_MISMATCH"
    return exp, ""


# ----------------------------------------------------------------- 终端控制
user32 = ctypes.WinDLL("user32", use_last_error=True)
CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def terminal_pids():
    r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                       text=True, errors="replace")
    out = []
    for l in r.stdout.splitlines():
        if "terminal64" in l.lower():
            try:
                out.append(int(l.split('","')[1]))
            except Exception:
                pass
    return out


def launch_terminal(exe):
    """优先 explorer 代启（能绕开某些环境下 0xC0000139 的加载失败），失败再直启"""
    try:
        subprocess.run(["explorer.exe", exe], capture_output=True, timeout=30)
    except Exception:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    t0 = time.time()
    while time.time() - t0 < 45:
        time.sleep(3)
        if terminal_pids():
            return True
    return False


def close_terminal():
    cur = set(terminal_pids())
    if not cur:
        return True
    hs = []

    def cb(h, l):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in cur and user32.IsWindowVisible(h):
            c = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, c, 256)
            if c.value.startswith("MetaQuotes"):
                hs.append(h)
        return True

    user32.EnumWindows(CB(cb), 0)
    for h in hs:
        user32.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE，优雅退出（别 taskkill，配置不落盘）
    for _ in range(20):
        time.sleep(1.5)
        if not terminal_pids():
            return True
    return False


POPUP = True          # --check / --no-popup 时置 False，避免弹窗阻塞自动化


def alert(text):
    print(text)
    if not POPUP:
        return
    try:
        # 弹窗会阻塞到用户点击；无人值守场景请用 --no-popup
        ctypes.windll.user32.MessageBoxW(0, text, "授权提示", 0x40)
    except Exception:
        pass


# ----------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--check", action="store_true", help="只校验，不启动终端")
    ap.add_argument("--account", default="", help="手动指定账号（默认从终端配置读）")
    ap.add_argument("--datadir", default="", help="手动指定数据目录")
    ap.add_argument("--no-popup", action="store_true", help="不弹窗（无人值守/自动化）")
    a = ap.parse_args()
    global POPUP
    if a.check or a.no_popup:
        POPUP = False

    cfg = dict(CONFIG_SAMPLE)
    cfg.update(json.load(open(a.config, encoding="utf-8")))

    # ---- 1) 账号
    datadir = a.datadir
    account = a.account
    exe = None
    if not account:
        dirs = find_datadirs()
        if datadir:
            dirs = [x for x in dirs if os.path.normcase(x[0]) == os.path.normcase(datadir)] or dirs
        # 优先挑有 Login 的
        for p, origin, login in dirs:
            if login and (not datadir or os.path.normcase(p) == os.path.normcase(datadir)):
                datadir, account = p, login
                break
        if not account and dirs:
            datadir = dirs[0][0]
    if not account:
        alert("找不到终端账号。请先正常启动一次 MT5 并登录，或加 --account 参数。")
        return 3
    exe = pick_terminal_exe(cfg, datadir)
    print("数据目录:", datadir or "(未知)")
    print("账号    :", account)
    print("终端    :", exe or "(未找到 terminal64.exe)")

    # ---- 2) 健康检查 + 激活
    st, body = http_get(cfg["url"].rstrip("/") + "/health")
    print("健康检查:", st, body[:80])
    if st != 200:
        alert("授权服务连不上（%s）。请检查网络或联系售后。" % st)
        return 4

    st, body = call(cfg, "/api/v1/activate", account,
                    extra="&broker=&terminal=")
    print("激活应答:", st, body[:120])
    exp, err = verify_ok(body, cfg["secret"], cfg["product"], account)
    if not exp:
        hint = {
            "UNKNOWN_KEY": "授权码不存在，请核对是否输错/发错。",
            "REVOKED": "该授权已被吊销。",
            "EXPIRED": "授权已到期。",
            "ACCOUNT_MISMATCH": "这个授权码已绑到别的账号，不能在本机使用。",
            "SIG_MISMATCH": "服务端签名校验失败（secret 不一致或应答被篡改）。",
            "BAD_REQUEST": "参数不全。",
        }.get(err, "授权被拒绝。")
        alert("授权不通过：%s（%s）" % (err, hint))
        return 5
    print("授权通过，到期日:", exp)

    if a.check:
        print("[check] 只校验模式，未启动终端。")
        return 0

    # ---- 3) 启动终端
    if not exe:
        alert("没找到 terminal64.exe，请在 guard.json 里填 terminal_exe。")
        return 6
    print("启动终端 ...")
    if not launch_terminal(exe):
        alert("终端启动失败。")
        return 7
    print("终端已启动 pid=%s" % terminal_pids())

    # ---- 4) 心跳看门狗
    beat = max(1, int(cfg["heartbeat_min"])) * 60
    grace = max(0, int(cfg["grace_min"])) * 60
    bad_since = None
    while True:
        time.sleep(beat)
        if not terminal_pids():
            print("终端已退出，启动器结束。")
            return 0
        st, body = call(cfg, "/api/v1/heartbeat", account)
        exp, err = verify_ok(body, cfg["secret"], cfg["product"], account)
        if exp:
            if bad_since:
                print("授权已恢复，到期日 %s" % exp)
            bad_since = None
            continue
        if bad_since is None:
            bad_since = time.time()
            print("心跳异常: %s (%s)，进入宽限期 %d 分钟" % (err, st, grace // 60))
            continue
        if time.time() - bad_since >= grace:
            if cfg.get("on_revoke", "close") == "close":
                print("宽限期结束，正在关闭终端 ...")
                close_terminal()
                alert("授权已失效（%s），交易终端已被关闭。请联系续期。" % err)
                return 8
            else:
                alert("授权已失效（%s），请尽快续期。" % err)
                bad_since = time.time()   # 重置，避免反复弹窗


if __name__ == "__main__":
    sys.exit(main())
