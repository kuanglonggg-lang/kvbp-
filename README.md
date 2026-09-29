# 轻量 EA 授权套件 —— 部署说明

配套文档：`方案对比.md`（为什么选这个方案、和 A神 那套的差异）

---

## 一、组成

```
license_server.py    服务端（Python 3.8+，零第三方依赖）
LicenseGuard.mqh     EA 侧守卫  →  复制到 <数据目录>\MQL5\Include\
SampleEA.mq5         示例 EA    →  复制到 <数据目录>\MQL5\Experts\ 用 MetaEditor 编译
selftest.py          本机自检（13 项断言）
deploy/install.sh    服务器一键部署（Ubuntu/Debian，systemd + 防火墙 + 每日备份）
deploy/verify.py     部署后链路自检（发卡→激活→心跳→拒绝路径→解绑）
启动授权服务.bat       Windows 本地起服务
licenses.db          授权库（首次运行自动生成，注意备份）
```

文档：

| 文档 | 什么时候看 |
|---|---|
| `方案对比.md` | 先看这个：四个方案怎么选、为什么不自己造 A神 那套 |
| `部署清单.md` | 买服务器前 → 部署 → 运维 → 发卡，全流程 |
| `客户端安装说明.md` | **直接转发给客户**的安装说明（含错误对照表） |
| `README.md` | 本文件：套件怎么用、原理、验证记录 |

---

## 二、服务端：5 分钟上线

### 1) 初始化 + 发卡

```bash
python license_server.py init
python license_server.py issue --product GridMaster --days 365 --note "张三 50109999"
# 输出：YWP7-L6XG-PLSG-WGW2        ← 这就是给客户的授权码
```

其他管理命令：

```bash
python license_server.py list                       # 全部授权码 + 绑定账号 + 最后心跳
python license_server.py unbind  --key XXXX-XXXX    # 客户换电脑：解绑，重新激活即绑新号
python license_server.py extend  --key XXXX-XXXX --days 90
python license_server.py revoke  --key XXXX-XXXX    # 违约/退款：直接吊销
```

### 2) 起服务

**务必改 secret**（要和 EA 里的 `LIC_SHARED_SECRET` 完全一致）：

```bash
python license_server.py serve --host 0.0.0.0 --port 8787 --secret "你的长随机串"
```

想开放 HTTP 发卡（以后接自动收款用）：

```bash
python license_server.py serve --secret "..." --admin-token "另一个长随机串"
# POST /api/v1/admin/issue  带 header  X-Admin-Token: <token>
# body: {"product":"GridMaster","days":365,"note":"..."}
```

### 3) 让它能被公网访问

- 有云主机：直接开 8787（建议前面挂 Nginx/Caddy 上 HTTPS —— MT5 的 `WebRequest` 支持 https）。
- 只有家里的机器：用 **Cloudflare Tunnel** 或 **frp** 把 8787 暴露成 `lic.你的域名.com`。
- 自检：`curl http://127.0.0.1:8787/health` → 应返回 `v1|OK|20260930|`

---

## 三、EA 侧：3 处改动

### 1) 改 secret

打开 `LicenseGuard.mqh`，把

```mql5
#define LIC_SHARED_SECRET  "CHANGE_ME_please_run_server_with_same_secret"
```

改成和服务端 `--secret` 一模一样。**两边不一致 = 所有请求都报 `SIG_MISMATCH`。**

### 2) 接进你的 EA

```mql5
#include <LicenseGuard.mqh>

input string InpProduct = "GridMaster";              // 产品代号，必须与服务端一致
input string InpLicUrl  = "https://lic.你的域名.com"; // 不要带末尾斜杠
input string InpLicKey  = "";                        // 客户填授权码
input int    InpGraceHours = 72;                     // 断网宽限小时

int OnInit()
{
   if(!LicInit(InpProduct, InpLicKey, InpLicUrl, LIC_SHARED_SECRET, InpGraceHours))
   {
      Alert("授权未通过：", g_lic_state,
            "\n检查①授权码 ②工具→选项→EA→允许 WebRequest 的 URL 是否已加入 ", InpLicUrl);
      return(INIT_FAILED);
   }
   return(INIT_SUCCEEDED);
}

void OnTick()
{
   if(!LicOk()) return;      // ← 唯一一道门，策略代码写在它下面
   // ... 你的策略 ...
}
```

> 只想"失效后继续显示但不下单"？把 `OnInit` 里的 `return(INIT_FAILED)` 改成 `return(INIT_SUCCEEDED)`，
> 仅靠 `OnTick` 的门拦交易即可。**但要注意：`INIT_FAILED` 才是让客户立刻知道"缺授权码"的最省事做法。**

### 3) 给客户的安装说明就一句话

> 安装 EA 后，打开 `工具 → 选项 → EA` → 勾选 **「允许 WebRequest 的 URL」** →
> 加入 `lic.你的域名.com` → 确定 → 重挂 EA → 在输入框粘贴授权码。

这就是全部摩擦。**注意：MT5 默认禁止 WebRequest，忘了加白名单会报 `WEBREQUEST_BLOCKED(4014)`——**
把这条写进你的交付文档，能省掉 90% 的售后。

---

## 四、工作原理（30 秒看懂）

```
EA 启动
  │  ① GET /api/v1/activate?key=..&product=..&account=50109999&broker=..
  ▼
服务端查库：key 存在? 未吊销? 产品对? 未过期? 绑定账号 == 本机账号?
  │  首次激活 → 把 account 写进库里（一码一账号就地绑定）
  │  ② 返回  v1|OK|<到期日>|<HMAC-SHA256 签名>
  ▼
EA 验签（写死的 secret）→ 通过则放行，并把「到期日+签名」写到
  <数据目录>\MQL5\Files\LIC_<产品>_<账号>.lic
  │  ③ 每 10 分钟心跳一次；断网时用本地缓存 + 72h 宽限
  ▼
到期 / 被吊销 / 绑到别的账号  →  g_lic_ok=false  →  OnTick 直接 return
```

**几个设计取舍，说明为什么这么定：**

- **为什么服务端返回一行文本而不是 JSON？** MQL5 手写 JSON 解析又长又容易错。`v1|OK|exp|sig` 一个 `StringSplit` 搞定。
- **为什么要 HMAC 签名？** 否则客户装个假服务端、回一句 `OK 20991231` 就绕过了。签名的密钥在 ex5 里，配合 Cloud Protector 才有意义。
- **为什么要本地签名缓存？** 服务端临时挂了 / 客户网络抽风时，不能让客户整个 EA 停摆。缓存文件也被签名保护，改了就报 `CACHE_TAMPERED`。
- **为什么首次激活就绑定账号？** 这是防"一个授权码在 50 个账户上乱跑"的最省事手段，同时不引入任何客户端代码。
- **安全天花板（要说实话）**：这套挡得住转发、白嫖、超期，**挡不住专业逆向**（ex5 可被反编译/内存补丁）。
  想再上一个台阶就是 **方案 C：MQL5 Cloud Protector**（官方、一键、把 ex5 编译成原生机器码）。
  ⚠ 但它可能带来指令集要求（`金鸡v5.1_p9` 那个 `AVX2 required` 就是同类现象），**上线前必须在一台老 CPU 上实测**。

---

## 五、自检

```bash
python selftest.py
```

覆盖 13 条路径：健康检查 / 发卡 / HTTP 发卡鉴权 / 正常激活 / HMAC 验签 / 心跳 /
不存在 key / 产品不符 / 换账号被拒 / 解绑换机 / 过期 / 吊销 / 续期。

**本机实测：全部 PASS。**

MQL5 侧还有一条自检：`LicenseGuard.mqh` 的 `LicSelfTest()` 会在 EA 启动时打印

```
[LIC-SELFTEST] HMAC(key,'hello') = 9307b3b915efb5171ff14d8cb55fbcc798c6c0ef1456d66ded1a6aa723a58b7b  PASS
```

看到 PASS 说明 MQL5 侧的 HMAC 实现和 Python 侧一致（不一致会报 FAIL，那就别往下走了）。

---

## 五-b、实测验证记录（2026-09-30）

这一套**不是"看着应该能跑"，是跑过的**：

| 验证项 | 方法 | 结果 |
|---|---|---|
| MQL5 代码能不能编译 | 本机 `MetaEditor64.exe /compile:` 真编译 | **0 errors, 0 warnings**，产出 `SampleEA.ex5` 29,374 B |
| 服务端逻辑 | `selftest.py` 真起 HTTP 服务跑 13 条路径 | **13/13 PASS** |
| **MQL5 的 HMAC == Python 的 HMAC** | 把编译好的 EA 丢进 MT5 **策略测试器**实跑（XAUUSD.c M1，2026.09.01–09.03，DLSMarkets-Live2 / 账号 6019007） | **PASS**，日志原文见下 |

```
2026.09.01 00:00:00   [LIC-SELFTEST] HMAC(key,'hello') = 9307b3b915efb5171ff14d8cb55fbcc798c6c0ef1456d66ded1a6aa723a58b7b  PASS
2026.09.01 00:00:00   [LIC] 授权通过：产品=GridMaster 账号=6019007 到期日=0 服务端=DLSMarkets-Live2
2026.09.01 00:00:00   [GridMaster] 启动成功。LIC TESTER exp=0 acc=6019007
```

> 测试器里 `WebRequest` 不可用，代码会自动走 `TESTER` 分支放行——这是设计如此，不是漏测。
> 之所以要跨端比对 HMAC：如果两端算法不一致，**生产环境每一条激活都会失败**（报 `SIG_MISMATCH`），
> 而这是最容易踩、也最难从日志反推的坑。

---

## 六、上线前 checklist

- [ ] 服务端 `--secret` 和 EA 里的 `LIC_SHARED_SECRET` 一致（先看 `[LIC-SELFTEST] PASS`）
- [ ] 服务端已改用**长随机 secret**，不是默认值
- [ ] 已经在客户的 MT5 白名单里加过你的域名（写进交付文档）
- [ ] `licenses.db` 定期备份（这是你全部客户的绑定关系）
- [ ] 有一台老 CPU 的机器实测过 ex5 能加载（如果你用了 Cloud Protector）
- [ ] 有一份「账号 → 授权码 → 到期日」的台账（`python license_server.py list` 就是，记得导出）
