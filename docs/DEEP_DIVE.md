# beibeiwu.apk 深挖报告（CTF 静态逆向）

> **归档说明（2026-07-15）：** 本文为早期深挖稿，内容已拆分并入文档体系。  
> 请优先阅读：`README.md` 索引 → `01_STATIC_ANALYSIS.md` / `02_PROTOCOL.md` / `03_TEST_LOG.md` / `04_FINDINGS.md`。  
> 本文保留作对照，后续更新请改编号文档，勿只改本文。

> 目标包：`xin.banghua.beiyuan0` v148  
> 反编译：`jadx_out/`  
> 工具脚本：`analysis/bbw_client.py`

---

## 1. 鉴权链路完整还原

### 1.1 统一入口

```java
// OkHttpInstance.getUrl / startHttp
url = action.startsWith("http")
    ? action
    : "https://applet.banghua.xin/app/index.php?i=999999&c=entry&a=webapp&do=" + action + "&m=socialchat";

// 每个 startHttp 自动加三个 Header
AUTHOR-TOKEN  = SharedHelper.readAUTHORIZATION()   // 登录下发，默认 "0"
EXPIRE-TOKEN  = sha1("xiaobei" + ts) + ts          // 本地可算
SIGN-TOKEN    = MD5(uid + "socialchat" + uid)      // 本地可算
```

### 1.2 算法细节

| Token | 算法 | 盐/密钥 | 可本地伪造 |
|---|---|---|---|
| SIGN-TOKEN | MD5 | 固定串 `socialchat` | ✅ |
| EXPIRE-TOKEN | SHA1 + 时间戳拼接 | 固定串 `xiaobei` | ✅ |
| UNIQUE-LOGIN | SHA1 | `xiaobei` + uid | ✅ |
| AUTHOR-SIGNATURE | SHA1(SHA1(uid)+nonce+ts) | **无密钥** | ✅ |
| AUTHOR-TOKEN | 服务端下发 | — | ❌（需登录） |

### 1.3 登录后 token 落地

```java
// CommonUtil.signIn 成功后
SharedHelper.saveAUTHORIZATION(userInfoList.getToken());  // SP: AUTHORIZATION
SharedHelper.saveUserInfoID(userInfoList.getId());        // SP: userinfo
// 另存融云 token、腾讯 IM usersig
```

**结论：** 客户端“签名”只是防小学生级别；真正鉴权依赖 `AUTHOR-TOKEN`。未登录时 `uid=0`、`AUTHOR-TOKEN=0` 仍会带上可复现的 SIGN/EXPIRE。

---

## 2. 登录链路

### 2.1 账号密码

```
POST do=signin0
body: userAccount, userPassword, uniquelogintoken, phonebrand, pushregid, version_code
→ JSON { code:200, json: UserInfoList{ id, token, nickname, ... } }
→ saveAUTHORIZATION(token)
→ 注册融云 + 拉腾讯 IM sig + 进主页
```

### 2.2 短信登录

```
1) POST https://applet.banghua.xin/sms_beibeiwu.php
   body: phoneNumber=...
2) 客户端滑块验证码（纯前端 Captcha，非服务端）
3) POST do=smsVerify0  body: phone, code
4) POST do=SigninOneKeyLogin1  body: userAccount, uniquelogintoken
```

注意：`LoginSmsActivity` 里 `smscode = "0000"` 只是初始占位，真正校验走服务端 `smsVerify0`。

### 2.3 一键登录 / 微信

- 阿里云号码认证：`Constant.AUTH_SECRET` 硬编码（超长 base64）
- 微信：`do=weinxinregister`，WX AppId `wxf057dbbb960d9c39`

### 2.4 唯一登录设备

```
uniquelogintoken = sha1("xiaobei" + uid)
compare: do=uniquelogin  body: myid, token
```

---

## 3. 高价值接口清单

### 3.1 鉴权 / 账号

| do / URL | 说明 |
|---|---|
| `signin0` | 密码登录 |
| `SigninOneKeyLogin` / `SigninOneKeyLogin1` | 一键/短信登录 |
| `smsVerify0` / `smsVerifyByUid` | 短信校验 |
| `sms_beibeiwu.php` | **独立发短信脚本** |
| `uniquelogin` | 单设备校验 |
| `findpassword` / `signup` / `signupwx` / `weinxinregister` | 找回/注册 |
| `user_delete` | 删号 |
| `verifyPhone` / `verify_certNo` | 手机/身份证核验 |
| `testField` | **测试字段接口** |

### 3.2 支付 / VIP / 金币

| do | 说明 |
|---|---|
| `buyCoinWechatXBXX` / `buyCoinAlipayXBXX` | 充金币 |
| `Payunifiedorder2vipXBXX` / `Payunifiedorder2svipXBXX` | 微信 VIP/SVIP |
| `Alipayaddorder2vipXBXX` / `Alipayaddorder2svipXBXX` | 支付宝 VIP/SVIP |
| `SvipTry` | SVIP 试用 |
| `sendVip` | **送 VIP** |
| `RefundSvipAndVip` | **退 VIP/SVIP**（short action） |
| `moneyExchangeVip` | 余额兑 VIP |
| `incomeExchangeMoney` | 收益兑余额 |
| `withdraw` | 提现（支付宝账号+金额） |
| `vipLevelOrder` / `fansLevelOrder` / `recommendLevelOrder` | 等级订单 |
| `alipaybeiyuan2.php` | 支付宝 orderString 生成 |

支付金额与商品 ID 由服务端控制；客户端只传 `coinId` / `vipid` + `PackageName`。  
重点看：**`sendVip`、`RefundSvipAndVip`、`SvipTry`、`moneyExchangeVip` 是否缺服务端校验**。

### 3.3 IM / 音视频凭据

| 项 | 值 / 位置 |
|---|---|
| 腾讯 IM SDKAppID | `1600039823` |
| 腾讯 IM SECRETKEY | **完整硬编码**，可本地 gen UserSig |
| 融云 APP_KEY | `m7ua80gbmo0km` |
| 融云 token | 服务端 `userregister.php` 下发 |
| 声网 RTC/RTM | `RtcTokenBuilderSampleXiaobei.php` / `RtmTokenBuilderSampleXiaobei.php` |
| tximsign.php | 服务端也可发 UserSig（登录后回调） |

`loginTencent(usersig)`：用服务端返回的 sig 登录；但本地 `GenerateTestUserSig` 同样能生成任意 uid 的 sig。

### 3.4 其它敏感 short action

`DirectChat`, `AdvancedUserApply`, `applyManualVerify`, `GetflashphotoTencent`,  
`isCensor`, `AddModeration`, `setForbidReason`, `transferGroup`,  
`getGroupAdmin` / `isGroupAdmin`, `check_available`

---

## 4. 安全缺陷汇总（按利用价值）

### P0 — 腾讯 IM 密钥硬编码
任意 `identifier` 可生成合法 UserSig（7 天有效）。  
影响：冒充任意用户进腾讯 IM 会话（若服务端无额外业务鉴权）。

### P0 — 客户端签名可完全复现
SIGN / EXPIRE / UNIQUE / AUTHOR-SIGNATURE 均无密钥。  
攻击面：在拿到任意 `AUTHOR-TOKEN` 后可脚本化全量 API；未登录接口若只校验 SIGN/EXPIRE 则可直接打。

### P1 — 发短信接口裸奔路径
`https://applet.banghua.xin/sms_beibeiwu.php`  
仅靠客户端拼的弱 Header；需验证是否有频控/图形码服务端校验。

### P1 — VIP 业务动作可疑
`sendVip` / `RefundSvipAndVip` / `SvipTry` / `moneyExchangeVip`  
典型 CTF/审计点：参数篡改、越权给他人开 VIP、0 元兑。

### P1 — SSL 校验形同虚设
- 非 `banghua.xin`：hostnameVerifier 直接 true  
- `banghua.xin`：只检查证书 CN 是否含 `RapidSSL`  
- `proxy(NO_PROXY)` 挡系统代理，但不是 pinning

### P2 — Sophix 调试开着
```java
setEnableDebug(true).setEnableFullLog()
setSecretMetaData(null, null, null)  // 元数据空
setTags(["test"])
```
热修复调试模式 + test tag，补丁通道值得关注。

### P2 — OSS 上传签名走服务端
`getAliyunSignature&content=` 由服务端签；bucket=`newecs`，endpoint=`oss-cn-shanghai`。  
需看该接口是否鉴权不严导致任意 object 写入。

### P3 — 神秘 assets
`assets/408037528` 是 **穿山甲广告 SDK 的嵌套 APK/ZIP**，不是业务 payload。

---

## 5. 推荐 CTF 攻击路径（按顺序）

1. **本地签名验证**  
   跑 `python analysis/bbw_client.py demo`，确认 SIGN/EXPIRE 算法。

2. **未登录接口探测**  
   用 `uid=0` + `AUTHOR-TOKEN=0` 打：  
   `getVersion1`, `isShowAD1`, `getGiftList`, `testField`, `getSlide` 等，看哪些不强制 token。

3. **登录拿 AUTHOR-TOKEN**  
   账号密码 / 短信 / 自己注册；token 落在 SP `AUTHORIZATION`。

4. **水平越权**  
   固定自己的 token，改 body 里的 `userId` / `authid` / `id` / `you`：  
   `getUserAttributes`, `withdraw`, `sendVip`, `getUserRoomInfo`。

5. **VIP 逻辑**  
   抓正常买 VIP 包，重放 `SvipTry` / `sendVip` / `RefundSvipAndVip` / `moneyExchangeVip`。

6. **IM 侧**  
   本地 gen UserSig 登录腾讯 IM，看能否读他人会话（通常还要业务层校验）。

7. **短信接口**  
   评估 `sms_beibeiwu.php` 频控与是否可枚举。

---

## 6. 工具用法

```bash
# 打印算法 demo + curl 模板（不发网络请求）
python analysis/bbw_client.py demo

# 只算 headers
python analysis/bbw_client.py sign --uid 12345 --token YOUR_TOKEN

# 生成腾讯 IM UserSig
python analysis/bbw_client.py usersig --uid 12345

# 生成某个 action 的 curl 模板
python analysis/bbw_client.py url --action SvipTry --uid 12345 --token YOUR_TOKEN

# 列出内置高价值端点
python analysis/bbw_client.py list
```

---

## 7. 关键文件索引

| 路径 | 内容 |
|---|---|
| `jadx_out/sources/xin/banghua/beiyuan0/BuildConfig.java` | APP_KEY / BASE_SERVER |
| `.../TencentIM/signature/GenerateTestUserSig.java` | IM SECRETKEY |
| `.../utils/MD5Tool.java` + `cn/leyuan/.../MD5Tool.java` | SIGN-TOKEN |
| `cn/leyuan/.../CommonUtil.java` | 登录、EXPIRE、WX、OSS |
| `cn/leyuan/.../OkHttpInstance.java` | 全量 API 实现（1.3 万+ 行） |
| `cn/leyuan/.../SharedHelper.java` | token 本地存储 |
| `.../Signin/SigninActivity.java` / `LoginSmsActivity.java` | 登录 UI 流 |
| `.../SophixStubApplication.java` | 热修复 debug |
| `analysis/bbw_client.py` | 签名复现脚本 |
| `analysis/assets/` | 已提取配置/证书 |

---

## 8. 当前未做 / 下一步可选

- [ ] 真机/模拟器动态抓包（Frida 过 NO_PROXY，或改 APK 放代理）
- [ ] 对 `testField` / `sendVip` / `RefundSvipAndVip` 做**授权范围内**的接口探测
- [ ] 反编译融云/声网 PHP 路径是否目录遍历（`otherinterface/`）
- [ ] 分析 `libsecuritydevice.so` / `libdeviceid_607.so` 是否有额外设备指纹签名
- [ ] 从 `resources.arsc` / 字符串资源扫隐藏 URL
