# 02 · 协议说明

**最后更新：** 2026-07-15

## 1. 基础

### 1.1 主 API 模板

```
POST https://applet.banghua.xin/app/index.php?i=999999&c=entry&a=webapp&do={ACTION}&m=socialchat
Content-Type: application/x-www-form-urlencoded
```

短 action 由 `OkHttpInstance.getUrl()` 自动拼；若已是 `http` 则原样使用。

### 1.2 Redis 缓存类

```
https://redis.banghua.xin/app/index.php?i=888&c=entry&a=webapp&do={ACTION}&m=rediscache
```

### 1.3 公共 Header

| Header | 生成方式 | 备注 |
|---|---|---|
| `AUTHOR-TOKEN` | 登录响应 `UserInfoList.token` | 默认未登录 `"0"` |
| `EXPIRE-TOKEN` | `sha1("xiaobei"+ts)+ts` | ts 为秒级时间戳，可本地算 |
| `SIGN-TOKEN` | `MD5(uid+"socialchat"+uid)` | 可本地算 |
| `AUTHOR-UID` | uid | 部分接口 |
| `AUTHOR-NONCE` | `Random.nextInt(10000)` | 部分接口 |
| `AUTHOR-TIMESTAMP` | 毫秒时间戳 | 部分接口 |
| `AUTHOR-SIGNATURE` | `SHA1(SHA1(uid)+nonce+timestamp)` | 部分接口，无密钥 |

### 1.4 算法注意点

- `MD5Tool.MD5`：把 `char` 强转 `byte`（低 8 位），再 MD5，输出小写 hex。  
- `CommonUtil.sha1`：默认 charset（Android 通常 UTF-8）字节再 SHA1。  
- `SHA1Util.SHA1`：`iso-8859-1` 编码更新。  
- 实现参考：`analysis/bbw_client.py`。

---

## 2. 登录 / 注册 / 改密

### 2.1 密码登录 `signin0`

```
POST do=signin0
Body:
  userAccount
  userPassword
  uniquelogintoken   # sha1("xiaobei"+uid) 或未登录时用 0
  phonebrand
  pushregid
  version_code       # 154（xbly.apk；旧客户端 148 服务端仍可能接受）
```

成功：`code=200`，`json` 内嵌用户对象字符串（需再 `json.loads`），含 `token`、`userSign` 等。

### 2.2 短信发送

```
POST https://applet.banghua.xin/sms_beibeiwu.php
Body: phoneNumber={手机号}
```

实测：HTTP 200，body 常为空（仍可能已发短信）。

### 2.3 短信校验 `smsVerify0`

```
POST do=smsVerify0
Body: phone, code
```

成功：`{"code":"200","message":"验证成功。"}`

### 2.4 短信/一键登录 `SigninOneKeyLogin1`

```
POST do=SigninOneKeyLogin1
Body:
  userAccount
  uniquelogintoken
  # 客户端还会带 version_code / phonebrand / pushregid
```

**严重问题（已验证）：仅传手机号即可登录成功，无需短信码、无需密码。**

另有旧接口 `SigninOneKeyLogin`：返回 `{"error":"0","info":"登陆成功","userID":"..."}`。

### 2.5 注册 `signup`

```
POST do=signup
```

实测：手机号已存在 → 纯文本 `手机号已存在`。  
注意：App 注册页最终流程常等价于短信登录，不是传统“账号密码注册一次写库”。

### 2.6 改密 `findpassword`

```
POST do=findpassword
Body:
  sign = MD5(phone + "socialchat" + phone)
  userPhone
  userPassword
Headers: 需要有效 AUTHOR-TOKEN（否则 700 登录失效）
```

成功：纯文本 `密码修改成功`。

### 2.7 唯一登录 token

```
uniquelogintoken = sha1("xiaobei" + uid)
compare: do=uniquelogin  body: myid, token
```

---

## 3. 会话字段

登录成功后关键字段：

| 字段 | 含义 |
|---|---|
| `id` | 用户 uid |
| `token` | AUTHOR-TOKEN，形如 `{login_id}|{hex}` |
| `login_id` | 登录会话 id |
| `nickname` | 昵称 |
| `user_role` | 角色文案，如 `普通用户` |
| `vip` / `svip` | 会员状态/到期（0 表示无） |
| `name_card` | 改名卡数量 |
| `rp_verify_time` | 实名相关时间，0 表示未实名 |
| `advanced_user` | 高级用户标记 |
| `userSign` | 腾讯 IM UserSig（服务端下发） |
| `money` / `income` | 余额 / 收益 |
| `password` | **哈希出现在响应中**（敏感） |

Token 本地存储：

- SP `AUTHORIZATION` / key `AUTHORIZATION`
- SP userinfo 存 `userId` 等

---

## 4. 资料修改协议

### 4.1 查询改名资格 `resetNum`

```
POST do=resetNum
Body: uid, type=昵称
```

- 返回 `false`：客户端视为可直接改（`canReset=true`）  
- 返回 JSON：含上次修改时间，需改名卡确认

### 4.2 改昵称 `resetNew`（正式）

```
POST do=resetNew
Content-Type: multipart/form-data  （客户端实现）
Fields:
  sign = MD5(uid+"socialchat"+uid)
  type = 昵称设置
  userId = {uid}
  value = {新昵称}
```

客户端限制：昵称最多 10 字。  
服务端限制（实测）：**未实名 → 403**。

### 4.3 其它 `resetName`

```
POST do=resetName
Body: uid, value
```

实测 body 空，作用不明/可能废弃。

### 4.4 实名相关

```
POST https://applet.banghua.xin/otherinterface/aliyun/InitFaceVerify0.php
Body: metaInfo, certNo, certName

POST do=SaveRPVerifyInfo  (i=888)
Body: id, cert_name, cert_no, result
```

**实名由用户本人在 App 完成，不在此自动化伪造。**

---

## 5. VIP 相关协议（摘要）

### 5.1 `SvipTry`

```
POST do=SvipTry
Body: id={uid}
```

### 5.2 `sendVip`

```
POST do=sendVip
Body: uid1, uid2, vip_id, token(uniquelogintoken)
```

余额不足 → `余额不足`。

### 5.3 `moneyExchangeVip`

```
POST do=moneyExchangeVip
Body: uid, vip_id, coupon_id, token
```

余额不足 → `余额不足`。

---

## 6. 腾讯 IM UserSig

客户端可本地生成（密钥硬编码）：

```
SDKAppID  = 1600039823
SECRETKEY = c064eea5978cf60af28dcbbe9dd7c35e...
expire    = 604800
算法      = TLS 2.0 + HmacSHA256 + zlib + base64url 变体
```

脚本：`python bbw_client.py usersig --uid 726285`

登录响应里的 `userSign` 为服务端下发版本；客户端 `GenerateTestUserSig` 亦可。

---

## 7. 响应形态约定

| 形态 | 示例 |
|---|---|
| 标准 InfoList | `{"code":"200|400|403|700","message":"...","extra":"提示|提醒","json":"..."}` |
| 纯文本成功 | `密码修改成功` / `设置成功` / `余额不足` |
| 旧登录 | `{"error":"0","info":"登陆成功","userID":"..."}` |
| 空 body | 部分接口 200 但无正文 |

`code=700`：登录失效，客户端会登出跳登录页。  
`code=403`：业务拒绝（如未实名）。  
`code=400`：参数/业务错误（如密码错误、验证码错误）。
