# 01 · 静态逆向分析

**最后更新：** 2026-07-15

## 1. APK 结构

| 类别 | 数量/说明 |
|---|---|
| ZIP entries | 7487 |
| classes*.dex | 12 个（最大约 10MB 级） |
| native .so | 63（arm64 为主） |
| assets | 480 |
| res | 大量 UI 资源 |

### 1.1 主要 SDK / 组件（从 so / 包名推断）

- 融云 IM / RTC：`libRongIMLib.so`、`libRongRTCLib.so` 等
- 腾讯 IM / 音视频相关
- 穿山甲广告（`assets/408037528` 为嵌套 APK/ZIP）
- 阿里 Sophix 热修复
- 阿里云 OSS、号码认证、HA 监控
- 高德 / 腾讯地图相关 so
- ffmpeg / ijkplayer 系
- Bmob、OkHttp、Retrofit 等

### 1.2 神秘 assets `408037528`

- 大小约 6.3MB，魔数 `PK` → ZIP
- 解压后为 **穿山甲广告 SDK 嵌套包**（含 classes.dex、so、res）
- **非业务隐藏 payload**，见 `analysis/asset_408037528/`

## 2. 业务包结构

### 2.1 主包 `xin.banghua.beiyuan0`

约 716 个 Java 文件，关键入口：

| 文件 | 作用 |
|---|---|
| `App.java` | 真 Application：IM、广告、推送初始化 |
| `BuildConfig.java` | APP_KEY / BASE_SERVER 等 |
| `LaunchActivity.java` | 启动、唯一登录校验、热修复查询 |
| `SophixStubApplication.java` | Sophix 壳，`setEnableDebug(true)` |
| `Signin/*` | 登录注册找回密码 |
| `TencentIM/signature/GenerateTestUserSig.java` | **IM SECRETKEY 硬编码** |
| `me/setting/NicknameEditActivity.java` | 改昵称 UI |

### 2.2 基础库 `cn.leyuan.base_library`

| 文件 | 作用 |
|---|---|
| `utils/OkHttpInstance.java` | **全量 API 实现**（上万行） |
| `utils/CommonUtil.java` | 登录成功处理、EXPIRE、WX、OSS |
| `utils/MD5Tool.java` | SIGN-TOKEN |
| `utils/SHA1Util.java` | AUTHOR-SIGNATURE |
| `utils/SharedHelper.java` | SP 存 token / uid |
| `utils/Uniquelogin.java` | uniquelogintoken |
| `list/UserInfoList.java` | 用户字段模型 |

## 3. BuildConfig 常量

来源：`xin.banghua.beiyuan0.BuildConfig`

| 字段 | 值 |
|---|---|
| APPLICATION_ID | `xin.banghua.beiyuan0` |
| APP_KEY（融云） | `m7ua80gbmo0km` |
| BASE_SERVER_ADDRES | `https://redis.banghua.xin:8080/` |
| BUSINESS_TOKEN | `lymM6dNKREIknE5VJGskfU` |
| VERSION_CODE | `154`（`xbly.apk`；旧 `beibeiwu.apk` 为 148） |
| FLAVOR | `official` |
| DEBUG | `false` |

## 4. 硬编码密钥 / 第三方凭据

| 类型 | 值 / 位置 |
|---|---|
| 腾讯 IM SDKAppID | `1600039823`（`GenerateTestUserSig`） |
| 腾讯 IM SECRETKEY | `c064eea5978cf60af28dcbbe9dd7c35e110734fcf1a5662ead3e87e2eb8a554e` |
| 微信 APP_ID（本包） | `wxf057dbbb960d9c39` |
| 微信 APP_ID（旧包名） | `wxb8adb92718082e0b` |
| 穿山甲 appId | `5435573` |
| 阿里 HA appKey | `333510424` |
| 阿里 HA appSecret | `ac4d390a63d645b79340e3b89efd8d3a` |
| 阿里号码认证 AUTH_SECRET | `xin.banghua.onekeylogin.Constant` 超长 base64 |
| 小米推送 | `2882303761520239799` / `5112023917799` |
| 魅族推送 | `124945` / `399a4bb4701046ffbff85a5505251abb` |
| OPPO 推送 | `66634f0066b3427694b7c468f0263e8e` / `33ef499149634128a2a7e7fa1c0e824a` |
| OSS bucket | `newecs`，endpoint `oss-cn-shanghai.aliyuncs.com` |
| OSS 签名 | 服务端 `do=getAliyunSignature` |

## 5. 鉴权算法（静态）

详见 [02_PROTOCOL.md](./02_PROTOCOL.md)。摘要：

```
SIGN-TOKEN    = MD5(uid + "socialchat" + uid)
EXPIRE-TOKEN  = sha1("xiaobei" + ts) + ts
UNIQUE-LOGIN  = sha1("xiaobei" + uid)
AUTHOR-SIG    = SHA1(SHA1(uid) + nonce + timestamp)   // 无密钥
AUTHOR-TOKEN  = 登录后服务端下发，SP 名 AUTHORIZATION
```

URL 拼装：

```
若 action 非 http:
  https://applet.banghua.xin/app/index.php?i=999999&c=entry&a=webapp&do={action}&m=socialchat
```

## 6. 登录相关代码路径

| 流程 | 关键代码 |
|---|---|
| 密码登录 | `SigninActivity` → `CommonUtil.signIn(map, "signin0")` |
| 短信登录 | `LoginSmsActivity`：`sendCode` → `smsVerify0` → `SigninOneKeyLogin1` |
| 注册页 | `SignupActivity`：UI 有密码，**提交实际走短信 + SigninOneKeyLogin1** |
| 改密 | `FindPasswordActivity` → `smsVerify0` → `findpassword` |
| 资料初始化 | `Userset` → `do=signup` / `signupwx` |

### 6.1 登录成功后副作用（`CommonUtil.signIn`）

1. 解析 `UserInfoList`，`saveUserInfoID` / `setUserInfoList`
2. `saveAUTHORIZATION(userInfoList.getToken())`
3. 注册融云：`.../rongyun/.../userregister.php`
4. 拉腾讯 IM：`https://applet.banghua.xin/tximsign.php`
5. 定位、跳转主页、`GetSameIdentityCard`

## 7. 资料修改相关

| 功能 | 接口 / 方法 |
|---|---|
| 改昵称 | `OkHttpInstance.resetPersonalInfo` → `do=resetNew`，`type=昵称设置` |
| 改名次数查询 | `do=resetNum`，`type=昵称` |
| 改名卡 | 字段 `name_card`；不足跳商城 |
| 限制文案 | 普通用户 180 天一次；会员/改名卡另算 |
| 实名 | 阿里云人脸 `InitFaceVerify0.php` + `SaveRPVerifyInfo` |

## 8. VIP / 支付相关接口（静态枚举）

| do | 说明 |
|---|---|
| `buyCoinWechatXBXX` / `buyCoinAlipayXBXX` | 充金币 |
| `Payunifiedorder2vipXBXX` / `Payunifiedorder2svipXBXX` | 微信 VIP/SVIP |
| `Alipayaddorder2vipXBXX` / `Alipayaddorder2svipXBXX` | 支付宝 VIP/SVIP |
| `SvipTry` | SVIP 试用 |
| `sendVip` | 送 VIP（需余额） |
| `moneyExchangeVip` | 余额兑 VIP |
| `RefundSvipAndVip` | 退 VIP/SVIP（short action） |
| `withdraw` | 提现 |
| `alipaybeiyuan2.php` | 支付宝 orderString |

`vip` 字段在客户端常按 **到期时间戳** 处理（`CommonUtil` 内对 vip/svip 做秒数加减）。

## 9. SSL / 网络

- `OkHttpClient.proxy(Proxy.NO_PROXY)`：挡系统代理抓包
- `hostnameVerifier`：非 `banghua.xin` 直接 `true`；`banghua.xin` 仅粗查证书 CN 是否含 `RapidSSL`
- **非真正 Certificate Pinning**

## 10. Sophix

```java
// SophixStubApplication
setEnableDebug(true)
setEnableFullLog()
setSecretMetaData(null, null, null)
setTags(["test"])
```

## 11. 高价值 action 列表（部分）

完整枚举脚本：`enum_short.py`。静态从 `OkHttpInstance` 抽出约 **250+** 个 `do=`，**90+** 个 short `startHttp` action。

鉴权/账号：`signin0`、`SigninOneKeyLogin`、`SigninOneKeyLogin1`、`smsVerify0`、`findpassword`、`signup`、`uniquelogin`、`testField`…

VIP/钱：`sendVip`、`SvipTry`、`moneyExchangeVip`、`withdraw`、`buyCoin*`…

资料：`resetNew`、`resetNum`、`resetName`、`getUserAttributes*`…

## 12. 反编译命令备忘

```powershell
$env:JAVA_HOME = 'D:\tools\jadx\win\jadx-gui-1.5.5-with-jre-win\jre'
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
& 'D:\tools\jadx\jadx-1.5.3\bin\jadx.bat' -d 'D:\project\AI\bbw\jadx_out' --show-bad-code --deobf 'D:\project\AI\bbw\beibeiwu.apk'
```

退出码 1、57 个 decompile errors，主体源码可用。
