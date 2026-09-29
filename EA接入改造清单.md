# EA 接入改造清单：要不要"全部重新加限制"？

**答案：不用全部重做。但也不是"统一加一层"就行——要按"你有没有源码"分三类处理。**

一句话原理：**`.ex5` 是编译产物，没有源码就加不进任何东西。**
所以从"能不能加"这个角度，你手上的 EA 只有三种：

| 类型 | 判断 | 能不能加授权门 | 工作量 |
|---|---|---|---|
| **A 有源码**（.mq5） | 你自己写的 / 买断源码的 | ✅ **能，加约 10 行** | 每个 EA 5 分钟 |
| **B 无源码，但本身带本地状态文件协议** | 例如鲲鹏对冲那类 | ⚠ **能，但要照抄你已有的"本地心跳"模式**，且每个产品都要单独逆一次协议 | 每个产品半天~数天 |
| **C 无源码，也没有任何协议** | 网上买的裸 ex5 | ❌ **加不了** | — |

---

## 一、类型 A：有源码 → 加约 10 行，策略逻辑一行不动

以你自己的成品 `成品\策略大师EA_v1.62_完整套件\MT5版\StrategyMaster_CleanRoom_源码.mq5`
（460 行）为实例，它现在的骨架是：

```
  12 | #include <Trade\Trade.mqh>
  13 | #include <Trade\PositionInfo.mqh>
  14 | #include <Trade\AccountInfo.mqh>
  15 | #include <Trade\SymbolInfo.mqh>
 ...
  36 | input group "=== 基础与风控参数 ==="
 ...
 185 | int OnInit()
 ...
 222 | void OnDeinit(const int reason)
 ...
 368 | void OnTick()
```

### 改动一：第 15 行后面加 1 行 + 2 个输入项

```mql5
#include <Trade\SymbolInfo.mqh>
#include <LicenseGuard.mqh>                                   // ← 新增

input group "=== 授权 ==="                                      // ← 新增
input string InpLicUrl = "http://lic.你的域名.com";              // ← 新增（不带末尾斜杠）
input string InpLicKey = "";                                    // ← 新增（客户填授权码）
```

### 改动二：`OnInit()` 开头插 6 行

```mql5
int OnInit()
{
   if(!LicInit("StrategyMaster", InpLicKey, InpLicUrl, LIC_SHARED_SECRET))   // ← 新增
   {                                                                          // ← 新增
      Alert("授权未通过：", g_lic_state,                                       // ← 新增
            "\n请检查授权码，以及 工具→选项→EA→允许 WebRequest 的 URL 是否已加入 ",
            InpLicUrl);
      return(INIT_FAILED);                                                    // ← 新增
   }                                                                          // ← 新增

   Print("=== [Clean-Room] Strategy Master v1.62 初始化启动 ===");   // 原有代码，不动
   ...
}
```

### 改动三：`OnTick()` 开头插 1 行

```mql5
void OnTick()
{
   if(!LicOk()) return;      // ← 新增：唯一一道门。策略逻辑一行不动
   ...
}
```

### 就这些

- **不加 `OnTimer` 也行**——`LicOk()` 里已经带了心跳节流，`OnTick` 每次调用都会自动按 10 分钟间隔去 ping 服务端。
- **不加在 `OnDeinit`**——不需要。
- **不动任何策略、风控、手数、信号逻辑**。
- 编译 → 一个带门的 ex5 就出来了。

> ⚠ `LIC_SHARED_SECRET` 是**编译期常量**（在 `LicenseGuard.mqh` 里），不能在运行时改——否则客户能自己改。
> 所以流程是：**服务器生成 secret → 写进 `LicenseGuard.mqh` → 再编译所有 EA**。一套服务端一个 secret。

---

## 二、类型 B：无源码，但本身带本地状态文件协议

**这类你已经做过一次了** —— `成品\鲲鹏对冲-本地授权版\`：

| 文件 | 角色 |
|---|---|
| `SMH_LocalHeartbeat.ex5` | **指标**（挂在 `MQL5\Indicators`），维护 `AUTH`、`AUTH_TS`、`CMD_TS` 状态文件 |
| `鲲鹏对冲-本地量化EA.ex5` | 原始交易 EA，**字节未改动** |
| 本地控制台 | 读状态文件、下发命令，只监听 `127.0.0.1:18765` |

你的验证记录（2026-08-11）写得很清楚：

> 原下载策略 EX5 能直接加载，限制点位于原网页桥接器的**授权和命令分发层**。
> 本地控制台已实现 SMH_INTRO 状态发现、命令写入和 ACK 校验，**不修改原 EX5 字节**。

**这就是"给无源码 ex5 加门"的唯一干净做法**：不改字节，而是**替代它原本依赖的那个"授权/命令下发组件"**——
原厂用网页桥接器 + 服务端心跳，你换成本地心跳指示器。

**但这个模式的边界必须说清**：

- ✅ 适用：**原 ex5 本身要靠外部组件提供授权/命令**（像鲲鹏那样，原设计就有桥接 EA）。
- ❌ 不适用：**原 ex5 自带授权逻辑、或者根本没授权**。前者你改不了它的判定（那正是 金鸡v5.1_p9 走到 DLL 注入的原因）；
  后者没有"外部组件"可以替代，客户直接挂上就能跑。
- 💰 成本：**每个产品都要单独逆一次它的状态文件协议**（字段名、命名空间、心跳格式、ACK 规则）。
  鲲鹏那套是做出来了，但你不会想为每个第三方产品都做一遍。
- 🎯 用途：**适合"你自己要用"的产品**，不适合当"发给下线的钩子"——
  因为下线的机器上你得同时发原版 ex5 + 你的心跳组件，客户一旦知道原理就能自己找原厂桥接器。

---

## 三、类型 C：无源码、无协议 → 加不了

**不要在它上面想办法。** 唯一的"办法"是给 ex5 外面套一层包装器（运行时改内存/注入 DLL），
而这正是 `金鸡v5.1_p9` 走的路，代价你已经亲眼见过：

```
journal: DLL loading is not allowed                     ← 终端默认关着 DLL 导入
MQL5   : AVX2 required, you have AVX only               ← 老 CPU 直接被挡
Experts: loading of 金鸡v5.1_p9 failed [568]
```

**DLL 导入被拦、CPU 指令集要求、杀软查杀、终端 LiveUpdate 打崩**——这四个坑一个都躲不掉。
你不想复刻 A神 那套复杂度，那这条路就整个不要走。

**结论**：想让某个 EA 带门，**先拿到它的源码**。拿不到就换一个能拿到源码的 EA，别在 ex5 上硬改。

---

## 四、你本机实测扫到的"可以直接加门"的自家 EA

扫描结果（`E:\量化`、`D:\360MoveData`、各 MT5 数据目录，共 491 个源码文件）中，
属于**你自己、有完整源码**的 EA：

| 产品 | 源文件 | 大小 | 备注 |
|---|---|---|---|
| **策略大师 MT5** | `成品\策略大师EA_v1.62_完整套件\MT5版\StrategyMaster_CleanRoom_源码.mq5` | 18.9 KB / 460 行 | **最成熟的产品**，配套 13 个预设 `.set` |
| **US30/US500 利差套件** | `us30_us500_spread_suite\US30_US500_Spread_EA.mq5` | 167 KB | 主 EA；配套 `SpreadFeed` / `Spread_Chart` |
| **XAU 黄金系列** | `XAU_Gold_Manual_CN_v2~v5.mq5`、`XAU_Gold_Manual_Signal.mq5` | 20~24 KB | 手册/信号版 |
| **XAU TriFlow** | `XAU_TriFlow_Manual_CN.mq5` | 14.6 KB | |
| **XAU 爆仓篮子** | `XAU_BurstBasketScalper.mq5` | 33.2 KB | |
| **XAU 对冲/状态机** | `XAU_HedgeRecovery.mq5`、`XAU_M1_StateMachine.mq5`、`XAU_M5_StopReverse.mq5` | 14~24 KB | |
| **CentGold 核心** | `CentGold_Core_Recovered.mq5` | 34.0 KB | |
| **SMC 系列** | `smc_live_trader.mq5`、`smc_watch.mq5`、`smc_capture.mq5` | 4.7~11.1 KB | 分析用 |

**这 8 组就是你能真正"发出去带门"的资产。** 每个加约 10 行，一个下午能全部改完。

---

## 五、productId 命名规范（有技术约束，别随手起名）

`LicenseGuard` 拼 URL 时**不做 URL 编码**，所以 **productId 必须纯 ASCII**（字母/数字/下划线，不要空格和中文）。

建议一张表，一个 EA 一个 ID：

| productId | 对应 EA |
|---|---|
| `StrategyMaster` | 策略大师 MT5 v1.62 |
| `US30US500_Spread` | 利差套件主 EA |
| `XAU_TriFlow` | XAU TriFlow |
| `XAU_BurstBasket` | XAU 爆仓篮子 |
| `XAU_GoldManual` | XAU 黄金手册版 |
| `XAU_HedgeRecovery` | XAU 对冲恢复 |

发卡时按产品分开：

```bash
python3 license_server.py issue --product XAU_TriFlow --days 365 --note "张三 50109999"
```

**好处**：同一个授权码池按产品隔离——客户到期或违约时，只停那一个产品，不影响别的。
（授权码本身不绑产品池是服务端校验的，见 `do_activate()` 里的 `product_mismatch`。）

---

## 六、改完之后必须走的四步验证

1. **编译**：`MetaEditor` 编译 → 必须 `0 errors`。
   本机可用现成工具：`python tools\mql5_run.py compile <你的EA.mq5> --inc <含Include子目录的路径>`
2. **不填授权码测一次**：挂到图上，应该看到 `[LIC] 授权失败：NOT_CONFIGURED` 且 **EA 被移除**（证明门真的在）。
3. **填一张真卡测一次**：服务端发一张测试卡 → 挂上 → 专家日志应出现
   ```
   [LIC-SELFTEST] HMAC(key,'hello') = 9307b3b9…8b7b  PASS
   [LIC] 授权通过：产品=XAU_TriFlow 账号=your_account 到期日=20271001
   ```
4. **吊销测一次**：服务端 `revoke --key` → EA 应在下一次心跳（≤10 分钟）后停止交易。
   这一步是证明"门真的听服务端的话"，别省。

---

## 七、一条红线

**不要把带别人授权的 ex5 发给你的下线。**

A神 平台下载的那些（神谕·SMC、SMC FUSION、重粒子、天空之城、货币对、金枪客……）都带着
**他自己的**设备指纹 + 服务器校验。你转手发出去意味着：

1. 你的客户要过**他的**验证 → 你完全没有控制权；
2. 他随时能停（你已经见过 `ACCOUNT_NOT_FOUND` / 服务端拒绝是什么样）；
3. 你是在拿**别人的付费产品**给**你自己的 IB 客户**做钩子 —— 客户一旦查到来源，你的 IB 关系就崩了。

**你自己有 8 组 EA 源码**（第四节那张表），用它们做钩子够用了，不需要碰别人的东西。
