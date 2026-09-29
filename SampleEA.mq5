//+------------------------------------------------------------------+
//|  SampleEA.mq5                                                    |
//|  演示：把你的策略套进"授权门"里 —— 只有 LicOk() 为真才下单        |
//|  编译：放进 <数据目录>\MQL5\Experts\  用 MetaEditor 编译           |
//+------------------------------------------------------------------+
#property copyright "KVBP lightweight licensing kit"
#property version   "1.00"
#property strict

#include <LicenseGuard.mqh>

//--- 授权参数（发给客户时只改 InpLicKey 的默认值，或让客户自己填）
input string InpProduct    = "GridMaster";                        // 产品代号（必须与服务端一致）
input string InpLicUrl     = "http://lic.example.com";            // 授权服务地址（不含末尾斜杠）
input string InpLicKey     = "";                                  // 授权码 XXXX-XXXX-XXXX-XXXX
input int    InpGraceHours = 72;                                  // 断网宽限小时

//--- 策略参数（示例）
input double InpLots       = 0.10;
input int    InpMagic      = 8888;
input int    InpStopPoints = 0;                                   // 0 = 不下止损

//--- 内部
bool     g_blocked_reported = false;

//+------------------------------------------------------------------+
int OnInit()
{
   // 授权不通过就直接拒绝加载 —— 最省心的做法，客户立刻知道"要授权码"
   if(!LicInit(InpProduct, InpLicKey, InpLicUrl, LIC_SHARED_SECRET, InpGraceHours))
   {
      Alert("[", InpProduct, "] 授权未通过：", g_lic_state,
            "\n请检查授权码、以及 工具→选项→EA→允许 WebRequest 的 URL 是否已加入 ",
            InpLicUrl);
      return(INIT_FAILED);
   }
   EventSetTimer(60);      // 每分钟跑一次心跳检查（OnTimer）
   Print("[", InpProduct, "] 启动成功。", LicStatus());
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
void OnTimer()
{
   // 心跳在这里也跑一次，保证即使没行情也能维持授权状态
   if(!LicOk() && !g_blocked_reported)
   {
      g_blocked_reported = true;
      Print("[", InpProduct, "] 授权已失效，暂停交易：", g_lic_state);
      Comment(LicStatus(), "  ← 授权失效，已暂停");
   }
   else if(LicOk())
   {
      g_blocked_reported = false;
      Comment(LicStatus());
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   // 唯一的"门"：授权不过就什么都不做。策略代码写在下面。
   if(!LicOk())
   {
      if(!g_blocked_reported)
      {
         g_blocked_reported = true;
         Print("[", InpProduct, "] 授权失效，暂停交易：", g_lic_state);
      }
      return;
   }

   // ---------- 以下是你自己的策略逻辑（示例：每根 M1 开一次多单） ----------
   static datetime lastBar = 0;
   datetime t = iTime(_Symbol, PERIOD_M1, 0);
   if(t == lastBar) return;
   lastBar = t;
   if(PositionsTotal() > 0) return;

   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action   = TRADE_ACTION_DEAL;
   req.symbol   = _Symbol;
   req.volume   = InpLots;
   req.type     = ORDER_TYPE_BUY;
   req.price    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   req.deviation= 20;
   req.magic    = InpMagic;
   req.comment  = "SampleEA";
   if(OrderSend(req, res))
      Print("开仓 ", res.order, " 价=", res.price);
   else
      Print("开仓失败 retcode=", res.retcode, " ", res.comment);
}
//+------------------------------------------------------------------+
