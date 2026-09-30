//+------------------------------------------------------------------+
//|  GuardWatch.mq5 —— 授权看门狗（给「无源码加密 ex5」加限制的①号方案） |
//|                                                                  |
//|  用途：目标 EA 是加密 ex5、没有源码、改不了。                          |
//|        本 EA 独立挂在图上，只管一件事：                                 |
//|          授权有效  -> 什么都不做（静默）                                |
//|          授权失效  -> 按 magic 撤掉挂单、平掉持仓，并报警                 |
//|                                                                  |
//|  依赖：LicenseGuard.mqh（同目录/Include 目录）                        |
//|        协议：/api/v1/activate + /api/v1/heartbeat（HMAC-SHA256 验签）  |
//|                                                                  |
//|  ⚠ 这是「惩罚式」：拦不住目标 EA 下单，只会立刻抹掉结果。                  |
//|     客户可以把本 EA 从图上摘掉，所以生产上要和 guard_launcher.py 一起用。  |
//+------------------------------------------------------------------+
#property copyright "KVBP"
#property version   "1.00"
#property strict

#include <LicenseGuard.mqh>
#include <Trade/Trade.mqh>

//------------------------------------------------------------------ 输入
input group "=== 授权 ==="
input string InpLicUrl   = "http://your.host:8787";   // 授权服务地址
input string InpLicKey   = "";                        // 该客户的授权码
input string InpProduct  = "JinJi51";                 // 产品名（要和服务端一致）

input group "=== 看护目标 ==="
input long   InpTargetMagic      = 8888;    // 要管的目标 EA 的 magic
input bool   InpCloseOnInvalid   = true;    // 失效时是否平仓（false = 只撤挂单）
input int    InpEnforceDelayMin  = 30;      // 连续失效多少分钟后才动手（防误伤）
input bool   InpAffectAllMagic   = false;   // true = 不管 magic，清掉本账户全部仓位

input group "=== 显示 ==="
input bool   InpShowComment      = true;    // 图上显示授权状态
input bool   InpVerbose          = false;   // 详细日志

//------------------------------------------------------------------ 全局
CTrade   trade;
datetime g_badSince   = 0;        // 从什么时候开始持续失效
int      g_cleaned    = 0;        // 累计清理次数
datetime g_lastClean  = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(0);          // 本 EA 自己不下单，只做清理
   trade.SetAsyncMode(false);

   bool ok = LicInit(InpProduct, InpLicKey, InpLicUrl, LIC_SHARED_SECRET);

   PrintFormat("[GUARD] 启动 产品=%s magic=%s 授权=%s (%s) 到期=%d",
               InpProduct, (string)InpTargetMagic, (ok ? "OK" : "FAIL"), LicStatus(), g_lic_exp);
   if(!ok)
      Print("[GUARD] 注意：当前授权不通过。会在连续失效 ",
            InpEnforceDelayMin, " 分钟后开始清理目标仓位。");

   EventSetTimer(60);                      // 每分钟评估一次
   UpdateComment();
   return(INIT_SUCCEEDED);                 // 即使授权失败也照常运行，看门狗要能干活
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Comment("");
   if(reason == REASON_REMOVE || reason == REASON_CHARTCLOSE)
      Print("[GUARD] 看门狗被摘除（reason=", reason, "）—— 这会让限制失效，请确认是本人操作。");
}

//+------------------------------------------------------------------+
void OnTimer()
{
   bool ok = LicOk();                      // 内含心跳节流 + 断网宽限

   if(ok)
   {
      if(g_badSince != 0)
         Print("[GUARD] 授权已恢复（", LicStatus(), "），停止清理。");
      g_badSince = 0;
   }
   else
   {
      if(g_badSince == 0)
      {
         g_badSince = TimeCurrent();
         Print("[GUARD] 授权异常：", LicStatus(), " ，进入观察期 ", InpEnforceDelayMin, " 分钟。");
      }
      int waited = (int)((TimeCurrent() - g_badSince) / 60);
      if(waited >= InpEnforceDelayMin)
         Enforce();
   }
   UpdateComment();
}

//+------------------------------------------------------------------+
void OnTick()
{
   // 授权失败期间每 tick 都盯一下（防止新仓在被清理前跑太久）
   if(g_badSince != 0 && (TimeCurrent() - g_badSince) / 60 >= InpEnforceDelayMin)
      Enforce();
}

//+------------------------------------------------------------------+
bool MatchMagic(const long magic)
{
   if(InpAffectAllMagic)
      return(true);
   return(magic == InpTargetMagic);
}

//+------------------------------------------------------------------+
void Enforce()
{
   int delOrders = 0, closed = 0;

   // 1) 撤掉目标 magic 的挂单（从后往前，避免下标错位）
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong tk = OrderGetTicket(i);
      if(tk == 0)
         continue;
      if(!MatchMagic((long)OrderGetInteger(ORDER_MAGIC)))
         continue;
      if(trade.OrderDelete(tk))
      {
         delOrders++;
         if(InpVerbose)
            PrintFormat("[GUARD] 撤单 #%I64u %s", tk, OrderGetString(ORDER_SYMBOL));
      }
   }

   // 2) 平掉目标 magic 的持仓
   if(InpCloseOnInvalid)
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong tk = PositionGetTicket(i);
         if(tk == 0)
            continue;
         if(!MatchMagic((long)PositionGetInteger(POSITION_MAGIC)))
            continue;
         if(trade.PositionClose(tk))
         {
            closed++;
            if(InpVerbose)
               PrintFormat("[GUARD] 平仓 #%I64u %s %.2f", tk,
                           PositionGetString(POSITION_SYMBOL),
                           PositionGetDouble(POSITION_VOLUME));
         }
      }
   }

   if(delOrders + closed > 0)
   {
      g_cleaned++;
      g_lastClean = TimeCurrent();
      PrintFormat("[GUARD] 授权失效（%s）→ 已撤单 %d 笔、平仓 %d 笔（累计第 %d 次）",
                  LicStatus(), delOrders, closed, g_cleaned);
   }
}

//+------------------------------------------------------------------+
void UpdateComment()
{
   if(!InpShowComment)
      return;
   string line1 = StringFormat("GuardWatch  授权: %s  到期: %d", LicStatus(), g_lic_exp);
   string line2 = g_badSince == 0
                  ? StringFormat("目标 magic: %s  状态: 正常", (string)InpTargetMagic)
                  : StringFormat("目标 magic: %s  观察中 %d 分钟  已清理 %d 次",
                                 (string)InpTargetMagic,
                                 (int)((TimeCurrent() - g_badSince) / 60), g_cleaned);
   Comment(line1 + "\n" + line2);
}
//+------------------------------------------------------------------+
