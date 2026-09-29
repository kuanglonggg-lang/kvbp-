#!/usr/bin/env bash
# ============================================================================
#  install.sh —— 一键部署 EA 授权服务（Ubuntu / Debian，需要 root）
#
#  用法（把整个交付包传到服务器后）：
#     sudo bash deploy/install.sh --port 8787
#     sudo bash deploy/install.sh --port 8787 --admin-token "你自己的长随机串"
#
#  做的事：建服务账号 → 装文件 → 生成/写入 secret → 注册 systemd → 开防火墙
#          → 起服务 → 装每日备份 → 自检并打印「要填进 EA 的东西」
#  幂等：重复执行只更新文件并重启，不会重复建用户/改 secret
# ============================================================================
set -euo pipefail

PORT=8787
ADMIN_TOKEN=""
SECRET=""
APP_DIR=/opt/ea-license
SVC=ea-license
LOG_DIR=/var/log/ea-license

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)        PORT="$2"; shift 2 ;;
    --admin-token) ADMIN_TOKEN="$2"; shift 2 ;;
    --secret)      SECRET="$2"; shift 2 ;;
    --dir)         APP_DIR="$2"; shift 2 ;;
    -h|--help)     sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "[x] 请用 root 运行（sudo bash $0）"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
if   [[ -f "$SRC/license_server.py"   ]]; then SRV="$SRC/license_server.py"
elif [[ -f "$SRC/../license_server.py" ]]; then SRV="$SRC/../license_server.py"
else echo "[x] 找不到 license_server.py，请把整个交付包一起传上来"; exit 1; fi

echo "=== 1/7 依赖检查 ==="
if ! command -v python3 >/dev/null; then
  echo "[i] 安装 python3 ..."
  (apt-get update -qq && apt-get install -y -qq python3) || { echo "[x] 装 python3 失败"; exit 1; }
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
PYBIN=$(command -v python3)
echo "    python3 = $PYV @ $PYBIN（本服务只用标准库，不需要 pip 装任何东西）"

echo "=== 2/7 建服务账号与目录 ==="
id -u ealic >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$APP_DIR" ealic
mkdir -p "$APP_DIR" "$LOG_DIR"
install -m 0755 "$SRV" "$APP_DIR/license_server.py"
if [[ -f "$SRC/verify.py" ]]; then
  install -m 0755 "$SRC/verify.py" "$APP_DIR/verify.py"
elif [[ -f "$SRC/../deploy/verify.py" ]]; then
  install -m 0755 "$SRC/../deploy/verify.py" "$APP_DIR/verify.py"
fi
chown -R ealic:ealic "$APP_DIR" "$LOG_DIR"

echo "=== 3/7 处理 secret 与 admin-token ==="
SEC_FILE="$APP_DIR/SECRET.txt"
if [[ -n "$SECRET" && -f "$SEC_FILE" ]]; then
  echo "    [i] 已存在 SECRET.txt，忽略 --secret（要改请先手工删掉它）"
fi
if [[ ! -f "$SEC_FILE" ]]; then
  [[ -n "$SECRET" ]] || SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
  [[ -n "$ADMIN_TOKEN" ]] || ADMIN_TOKEN=$(python3 -c 'import secrets;print(secrets.token_hex(24))')
  umask 077
  cat > "$SEC_FILE" <<EOF
LIC_SECRET=$SECRET
LIC_ADMIN_TOKEN=$ADMIN_TOKEN
EOF
  chown ealic:ealic "$SEC_FILE"
  echo "    [+] 已生成并写入 $SEC_FILE（chmod 600）"
else
  SECRET=$(sed -n 's/^LIC_SECRET=//p' "$SEC_FILE")
  ADMIN_TOKEN=$(sed -n 's/^LIC_ADMIN_TOKEN=//p' "$SEC_FILE")
  echo "    [=] 复用已有 secret"
fi
chmod 600 "$SEC_FILE"

echo "=== 4/7 注册 systemd 服务 ==="
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=EA License Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ealic
Group=ealic
WorkingDirectory=$APP_DIR
EnvironmentFile=$SEC_FILE
ExecStart=$PYBIN $APP_DIR/license_server.py serve --host 0.0.0.0 --port $PORT --secret \${LIC_SECRET} --admin-token \${LIC_ADMIN_TOKEN}
Restart=always
RestartSec=3
StandardOutput=append:$LOG_DIR/server.log
StandardError=append:$LOG_DIR/server.log
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null
systemctl restart "$SVC"
sleep 1.5

echo "=== 5/7 开防火墙 ==="
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null && echo "    [+] ufw 放行 $PORT/tcp"
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="$PORT"/tcp >/dev/null && firewall-cmd --reload >/dev/null
  echo "    [+] firewalld 放行 $PORT/tcp"
else
  echo "    [i] 没检测到活动的 ufw/firewalld"
fi
echo "    [!!] 云厂商控制台的「安全组」是另一道墙，必须去控制台放行 $PORT/tcp"

echo "=== 6/7 装每日备份 ==="
cat > /etc/cron.daily/ea-license-backup <<'EOF'
#!/bin/sh
D=/opt/ea-license/backups
mkdir -p "$D"
sqlite3 /opt/ea-license/licenses.db ".backup '$D/licenses-$(date +%F).db'" 2>/dev/null \
  || cp /opt/ea-license/licenses.db "$D/licenses-$(date +%F).db"
find "$D" -name 'licenses-*.db' -mtime +30 -delete
EOF
chmod 0755 /etc/cron.daily/ea-license-backup
echo "    [+] 每日备份到 $APP_DIR/backups（保留 30 天）"

echo "=== 7/7 自检 ==="
systemctl is-active --quiet "$SVC" || { echo "[x] 服务没起来，看：journalctl -u $SVC -n 50"; exit 1; }
H=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/health" || true)
echo "    /health -> ${H:-（无响应）}"
[[ "$H" == "v1|OK|"* ]] || { echo "[x] 自检失败，看：tail -n 50 $LOG_DIR/server.log"; exit 1; }

PUBIP=$(curl -s --max-time 5 https://api.ipify.org || curl -s --max-time 5 ifconfig.me || echo "<你的公网IP>")
cat <<EOF

============================================================
部署完成。
------------------------------------------------------------
服务端 secret（要原样填进 LicenseGuard.mqh 的 LIC_SHARED_SECRET）
  $(sed -n 's/^LIC_SECRET=//p' "$SEC_FILE")

发卡用的 admin-token（可选，接自动收款时才用）
  ${ADMIN_TOKEN:-（未设置）}

EA 里要填的授权服务地址 InpLicUrl
  http://$PUBIP:$PORT          ← 直接用 IP，免域名、免备案
  或 http://lic.你的域名.com    ← 有自己的域名时（记得 A 记录指向 $PUBIP）

客户要在 MT5 里加进白名单的（按 host 匹配，不是整条 URL）
  ${PUBIP}

本机自检
  curl http://127.0.0.1:$PORT/health
  python3 $APP_DIR/../deploy/verify.py http://127.0.0.1:$PORT --secret "\$(sed -n 's/^LIC_SECRET=//p' $SEC_FILE)"

日常命令
  systemctl status $SVC
  journalctl -u $SVC -f
  cd $APP_DIR && python3 license_server.py list
  cd $APP_DIR && python3 license_server.py issue --product GridMaster --days 365 --note "客户名 账号"
============================================================
EOF
