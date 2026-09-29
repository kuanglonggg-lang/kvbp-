//+------------------------------------------------------------------+
//|  LicenseGuard.mqh   v1.01                                        |
//|  轻量 EA 授权守卫（账户绑定 + 到期日 + 心跳 + 断网宽限）           |
//|  配套服务端：license_server.py（同一目录）                         |
//|                                                                  |
//|  用法：                                                           |
//|    #include <LicenseGuard.mqh>                                    |
//|    int OnInit()                                                   |
//|    {                                                              |
//|       if(!LicInit(InpProduct, InpLicKey, InpLicUrl,               |
//|                  LIC_SHARED_SECRET, InpGraceHours))               |
//|          return(INIT_FAILED);   // 想放宽可以改成放行+停手         |
//|       return(INIT_SUCCEEDED);                                     |
//|    }                                                              |
//|    void OnTick(){ if(!LicOk()) return;  /* 未授权就不干活 */ }     |
//+------------------------------------------------------------------+
#property copyright "KVBP lightweight licensing kit"
#property version   "1.01"

//--- 必须与 license_server.py 启动时的 --secret 完全一致
//--- （建议：源码里保留占位常量，发布前改成真值，再用 MQL5 Cloud Protector 保护 ex5）
#define LIC_SHARED_SECRET  "CHANGE_ME_please_run_server_with_same_secret"

#define LIC_API_VERSION    "v1"
#define LIC_TIMEOUT_MS     8000
#define LIC_BEAT_SEC       600      // 心跳间隔（秒）
#define LIC_RETRY_SEC      60       // 心跳失败后的重试间隔
#define LIC_FILE_PREFIX    "LIC_"   // 本地签名缓存文件名前缀

//--- 运行期状态
string   g_lic_product   = "";
string   g_lic_key       = "";
string   g_lic_url       = "";
string   g_lic_account   = "";
string   g_lic_server    = "";
int      g_lic_exp       = 0;       // 到期日 yyyymmdd
bool     g_lic_ok        = false;
bool     g_lic_ready     = false;
datetime g_lic_last_beat = 0;
int      g_lic_grace_h   = 72;
string   g_lic_state     = "INIT";

//+------------------------------------------------------------------+
//| 工具                                                              |
//+------------------------------------------------------------------+
string LicToHex(const uchar &b[])
{
   string h = "0123456789abcdef";
   string r = "";
   for(int i = 0, n = ArraySize(b); i < n; i++)
      r += StringSubstr(h, (b[i] >> 4) & 0x0F, 1) + StringSubstr(h, b[i] & 0x0F, 1);
   return(r);
}

int LicDateInt()
{
   MqlDateTime t;
   TimeToStruct(TimeLocal(), t);
   return(t.year * 10000 + t.mon * 100 + t.day);
}

// 小写化（MQL5 没有 StringToLowerCase，自己实现，避免大小写导致验签误判）
string LicLower(const string s)
{
   string r = "";
   for(int i = 0, n = StringLen(s); i < n; i++)
   {
      ushort u = StringGetCharacter(s, i);
      if(u >= 'A' && u <= 'Z') u = (ushort)(u + 32);
      r += ShortToString(u);
   }
   return(r);
}

// uchar[] → string（WebRequest 返回的是 uchar[]，应答是纯 ASCII）
string LicUcharToString(const uchar &b[])
{
   string r = "";
   for(int i = 0, n = ArraySize(b); i < n; i++)
      r += ShortToString((ushort)b[i]);
   return(r);
}

//+------------------------------------------------------------------+
//| SHA-256 → 小写 hex                                                |
//+------------------------------------------------------------------+
string LicSha256Hex(const string s)
{
   uchar data[], dummy[], out[];
   StringToCharArray(s, data, 0, StringLen(s));
   ArrayResize(dummy, 0);
   if(CryptEncode(CRYPT_HASH_SHA256, data, dummy, out) <= 0)
      return("");
   return(LicToHex(out));
}

//+------------------------------------------------------------------+
//| HMAC-SHA256 → 小写 hex（与 Python hmac.new(k, m, sha256) 等价）    |
//+------------------------------------------------------------------+
string LicHmac(const string secret, const string msg)
{
   uchar kraw[], key[64], ipad[64], opad[64], dummy[], kh[], inner[], outer[];
   uchar buf[];
   int   nk, i;

   StringToCharArray(secret, kraw, 0, StringLen(secret));
   nk = ArraySize(kraw);
   ArrayResize(dummy, 0);

   // 密钥超过 64 字节 → 先哈希
   if(nk > 64)
   {
      uchar tmp[], hashed[];
      ArrayResize(tmp, nk);
      ArrayCopy(tmp, kraw);
      if(CryptEncode(CRYPT_HASH_SHA256, tmp, dummy, hashed) <= 0) return("");
      nk = ArraySize(hashed);
      ArrayResize(kraw, nk);
      ArrayCopy(kraw, hashed);
   }

   ArrayInitialize(key, 0);
   for(i = 0; i < nk && i < 64; i++)
      key[i] = kraw[i];

   for(i = 0; i < 64; i++)
   {
      ipad[i] = (uchar)(key[i] ^ 0x36);
      opad[i] = (uchar)(key[i] ^ 0x5C);
   }

   uchar m[];
   StringToCharArray(msg, m, 0, StringLen(msg));

   // inner = SHA256(ipad || msg)
   ArrayResize(buf, 64 + ArraySize(m));
   ArrayCopy(buf, ipad, 0);
   ArrayCopy(buf, m, 64);
   if(CryptEncode(CRYPT_HASH_SHA256, buf, dummy, inner) <= 0) return("");

   // outer = SHA256(opad || inner)
   ArrayResize(buf, 64 + ArraySize(inner));
   ArrayCopy(buf, opad, 0);
   ArrayCopy(buf, inner, 64);
   if(CryptEncode(CRYPT_HASH_SHA256, buf, dummy, outer) <= 0) return("");

   return(LicToHex(outer));
}

//+------------------------------------------------------------------+
//| 自检：把 HMAC 与已知值对照，确认 MQL5 侧与 Python 侧一致           |
//| 期望: 9307b3b915efb5171ff14d8cb55fbcc798c6c0ef1456d66ded1a6aa723a58b7b
//+------------------------------------------------------------------+
bool LicSelfTest()
{
   string got = LicHmac("key", "hello");
   string want = "9307b3b915efb5171ff14d8cb55fbcc798c6c0ef1456d66ded1a6aa723a58b7b";
   bool ok = (got == want);
   PrintFormat("[LIC-SELFTEST] HMAC(key,'hello') = %s  %s", got, ok ? "PASS" : "FAIL <<< 两端 secret/实现不一致");
   return(ok);
}

//+------------------------------------------------------------------+
//| 本地签名缓存（离线也能重启；文件被改动则验签失败）                  |
//+------------------------------------------------------------------+
string LicCacheName()
{
   return(LIC_FILE_PREFIX + g_lic_product + "_" + g_lic_account + ".lic");
}

bool LicSaveCache()
{
   if(g_lic_exp <= 0) return(false);
   string sig = LicHmac(LIC_SHARED_SECRET, g_lic_product + "|" + g_lic_account + "|" + IntegerToString(g_lic_exp));
   int h = FileOpen(LicCacheName(), FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return(false);
   FileWriteString(h, IntegerToString(g_lic_exp) + "|" + sig + "\r\n");
   FileClose(h);
   return(true);
}

bool LicLoadCache()
{
   int h = FileOpen(LicCacheName(), FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return(false);
   string line = FileReadString(h);
   FileClose(h);

   string p[];
   if(StringSplit(line, '|', p) < 2) return(false);

   int    exp = (int)StringToInteger(p[0]);
   string sig = p[1];
   sig = LicLower(sig);
   string want = LicHmac(LIC_SHARED_SECRET, g_lic_product + "|" + g_lic_account + "|" + IntegerToString(exp));
   if(want == "" || sig != want)
   {
      g_lic_state = "CACHE_TAMPERED";
      return(false);
   }
   g_lic_exp = exp;
   return(true);
}

//+------------------------------------------------------------------+
//| HTTP GET（服务端应答是一行文本，MQL5 侧无需 JSON 解析）            |
//+------------------------------------------------------------------+
bool LicHttpGet(const string pathAndQuery, string &body)
{
   if(g_lic_url == "") { g_lic_state = "NO_URL"; return(false); }

   string url = g_lic_url + pathAndQuery;
   string headers = "User-Agent: MQL5-LicenseGuard\r\n";
   uchar  data[], result[];
   string rheaders = "";

   ResetLastError();
   int code = WebRequest("GET", url, headers, LIC_TIMEOUT_MS, data, result, rheaders);
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4014 || err == 4060 || err == 4061)
         g_lic_state = StringFormat("WEBREQUEST_BLOCKED(%d)", err);   // URL 不在白名单
      else
         g_lic_state = StringFormat("WEBREQUEST_ERR(%d)", err);       // 超时/断网/DNS
      return(false);
   }
   body = LicUcharToString(result);
   if(code != 200)
   {
      g_lic_state = StringFormat("HTTP_%d", code);
      return(false);
   }
   return(true);
}

//+------------------------------------------------------------------+
//| 调一次 activate / heartbeat                                       |
//+------------------------------------------------------------------+
bool LicCall(const string endpoint)
{
   // 测试器里没有 WebRequest，也没有实盘风险 → 直接放行
   if(MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION))
   {
      g_lic_ok = true;
      g_lic_state = "TESTER";
      return(true);
   }

   string q = StringFormat("%s?key=%s&product=%s&account=%s&broker=%s&terminal=%s&version=%s",
                           endpoint, g_lic_key, g_lic_product, g_lic_account,
                           g_lic_server, TerminalInfoString(TERMINAL_NAME),
                           IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)));

   string body = "";
   if(!LicHttpGet(q, body))
   {
      // 断网 / 服务端挂了：只要本地签名缓存仍在有效期内且在宽限窗口内，继续放行
      if(g_lic_exp > 0 && LicDateInt() <= g_lic_exp)
      {
         if(g_lic_last_beat == 0) { g_lic_ok = true; return(true); }
         int held = (int)((TimeLocal() - g_lic_last_beat) / 3600);
         if(held <= g_lic_grace_h) { g_lic_ok = true; return(true); }
         g_lic_state = "GRACE_EXPIRED";
      }
      g_lic_ok = false;
      return(false);
   }

   string p[];
   int n = StringSplit(body, '|', p);
   if(n < 3) { g_lic_state = "BAD_RESPONSE"; g_lic_ok = false; return(false); }
   if(p[1] == "ERR") { g_lic_state = p[2]; g_lic_ok = false; return(false); }
   if(p[1] != "OK" || n < 4) { g_lic_state = "BAD_RESPONSE"; g_lic_ok = false; return(false); }

   string sig = p[3];
   sig = LicLower(sig);
   string want = LicHmac(LIC_SHARED_SECRET, g_lic_product + "|" + g_lic_account + "|" + p[2]);
   if(want == "" || sig != want)
   {
      g_lic_state = "SIG_MISMATCH";   // 服务器被冒充 / 两端 secret 不一致
      g_lic_ok = false;
      return(false);
   }

   g_lic_exp = (int)StringToInteger(p[2]);
   g_lic_last_beat = TimeLocal();
   LicSaveCache();

   if(LicDateInt() > g_lic_exp)
   {
      g_lic_state = "EXPIRED";
      g_lic_ok = false;
      return(false);
   }
   g_lic_state = "OK";
   g_lic_ok = true;
   return(true);
}

//+------------------------------------------------------------------+
//| 对外 API                                                          |
//+------------------------------------------------------------------+
bool LicInit(const string product, const string key, const string url,
             const string secret, const int graceHours = 72)
{
   g_lic_product = product;
   g_lic_key     = key;
   g_lic_url     = url;
   StringTrimRight(g_lic_url);
   if(StringLen(g_lic_url) > 0 &&
      StringSubstr(g_lic_url, StringLen(g_lic_url) - 1, 1) == "/")
      g_lic_url = StringSubstr(g_lic_url, 0, StringLen(g_lic_url) - 1);

   g_lic_account = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   g_lic_server  = AccountInfoString(ACCOUNT_SERVER);
   g_lic_grace_h = graceHours;
   g_lic_ready   = true;

   if(product == "" || key == "" || url == "")
   {
      g_lic_state = "NOT_CONFIGURED";
      g_lic_ok = false;
      PrintFormat("[LIC] 未配置：product='%s' key='%s' url='%s'", product, key, url);
      return(false);
   }

   LicSelfTest();
   LicLoadCache();                       // 先读缓存：激活失败也能靠剩余有效期顶一阵
   bool r = LicCall("/api/v1/activate");

   if(r)
      PrintFormat("[LIC] 授权通过：产品=%s 账号=%s 到期日=%d 服务端=%s",
                  product, g_lic_account, g_lic_exp, g_lic_server);
   else
      PrintFormat("[LIC] 授权失败：%s（账号=%s 服务端=%s）→ 依次检查 ①授权码是否正确 "
                  "②工具→选项→EA→允许 WebRequest 的 URL 是否加了 %s "
                  "③这个 key 是否已绑了别的账号（要换机先解绑）",
                  g_lic_state, g_lic_account, g_lic_server, g_lic_url);
   return(r);
}

// 每 tick / 每 timer 调用；返回 false = 不要下单
bool LicOk()
{
   if(!g_lic_ready) return(false);
   if(MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION)) return(true);

   if(g_lic_exp > 0 && LicDateInt() > g_lic_exp)
   {
      g_lic_ok = false;
      g_lic_state = "EXPIRED";
      return(false);
   }

   if(g_lic_last_beat == 0 ||
      TimeLocal() - g_lic_last_beat >= (g_lic_ok ? LIC_BEAT_SEC : LIC_RETRY_SEC))
      LicCall("/api/v1/heartbeat");

   return(g_lic_ok);
}

string LicStatus()
{
   return(StringFormat("LIC %s exp=%d acc=%s", g_lic_state, g_lic_exp, g_lic_account));
}

//+------------------------------------------------------------------+
//| 部署提示：MT5 默认禁 WebRequest。                                  |
//| 工具 → 选项 → EA → 勾「允许 WebRequest 的 URL」→ 填入 g_lic_url 的  |
//| host（如 lic.example.com）。漏填会报 WEBREQUEST_BLOCKED(4014)。    |
//+------------------------------------------------------------------+
