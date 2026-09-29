#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify.py —— 部署后自检：对一个正在运行的授权服务跑一遍完整链路

  python3 deploy/verify.py http://127.0.0.1:8787
  python3 deploy/verify.py http://1.2.3.4:8787 --admin-token <token>

在服务器本机跑时可不带 --admin-token（自动走本地 CLI 发卡）；
在别处跑时必须带 --admin-token（走 HTTP 发卡接口）。

用到的账号是**测试账号**，产生的授权码 note 会标成 selftest，方便事后清理：
  python3 license_server.py list          # 找到 SELFTEST 那条
  sqlite3 licenses.db "delete from licenses where note='selftest'"
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
import urllib.request

OK = '\033[32mPASS\033[0m'
NG = '\033[31mFAIL\033[0m'
FAILED = []


def call(base, path, params, token=None, timeout=8):
    url = base.rstrip('/') + path + ('?' + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'verify.py')
    if token:
        req.add_header('X-Admin-Token', token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace').strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace').strip()
    except Exception as e:
        return 0, 'EXC:%s' % type(e).__name__


def check(name, cond, detail=''):
    print('  %-42s %s  %s' % (name, OK if cond else NG, str(detail)[:80]))
    if not cond:
        FAILED.append(name)
    return cond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('base')
    ap.add_argument('--admin-token', default=None)
    ap.add_argument('--secret', default=None,
                    help='仅当你想顺便核对签名时给出（服务器上在 /opt/ea-license/SECRET.txt）')
    ap.add_argument('--product', default='GridMaster')
    a = ap.parse_args()
    base = a.base.rstrip('/')

    print('== 目标 %s ==' % base)
    st, body = call(base, '/health', None)
    check('GET /health 可达', body.startswith('v1|OK|'), body)

    # 发卡
    key = None
    if a.admin_token:
        st, body = call(base, '/api/v1/admin/issue',
                        {'product': a.product, 'days': 1, 'note': 'selftest'}, token=a.admin_token)
        try:
            key = json.loads(body)['key']
            check('HTTP 发卡接口', True, key)
        except Exception:
            check('HTTP 发卡接口', False, body)
    else:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.exists(os.path.join(here, 'license_server.py')):
            sys.path.insert(0, here)
            import license_server as L
            key = L.issue(a.product, 1, 'selftest')
            check('本地 CLI 发卡', True, key)
        else:
            check('发卡（需要 --admin-token 或在本机跑）', False,
                  '当前目录下没有 license_server.py')

    if not key:
        print('\n发不出卡，后面没法测。')
        return 1

    acc = '50109999'
    st, body = call(base, '/api/v1/activate',
                    {'key': key, 'product': a.product, 'account': acc,
                     'broker': 'selftest', 'terminal': 'verify', 'version': '1'})
    check('正常激活', '|OK|' in body, body)
    p = body.split('|')
    if len(p) == 4 and a.secret:
        want = hmac.new(a.secret.encode(), ('%s|%s|%s' % (a.product, acc, p[2])).encode(),
                        hashlib.sha256).hexdigest()
        check('HMAC 签名与 secret 一致', p[3] == want, p[3][:24] + '…')
    elif len(p) == 4:
        check('应答格式 v1|OK|<exp>|<sig>', True, body)

    st, body = call(base, '/api/v1/heartbeat',
                    {'key': key, 'product': a.product, 'account': acc})
    check('心跳', '|OK|' in body, body)

    st, body = call(base, '/api/v1/activate',
                    {'key': 'AAAA-BBBB-CCCC-DDDD', 'product': a.product, 'account': acc})
    check('伪造授权码被拒', 'UNKNOWN_KEY' in body, body)

    st, body = call(base, '/api/v1/activate',
                    {'key': key, 'product': a.product, 'account': '50999999'})
    check('换账号被拒（一码一号）', 'ACCOUNT_MISMATCH' in body, body)

    st, body = call(base, '/api/v1/deactivate', {'key': key, 'account': acc})
    check('解绑', 'UNBOUND' in body or '|OK|' in body, body)

    st, body = call(base, '/api/v1/activate',
                    {'key': key, 'product': a.product, 'account': '50999999'})
    check('解绑后换机可用', '|OK|' in body, body)

    print()
    if FAILED:
        print('结果：%d 项失败 -> %s' % (len(FAILED), '，'.join(FAILED)))
        return 1
    print('结果：全部 PASS。这个服务可以发给客户了。')
    print('清理测试卡：sqlite3 licenses.db "delete from licenses where note=\'selftest\'"')
    return 0


if __name__ == '__main__':
    sys.exit(main())
