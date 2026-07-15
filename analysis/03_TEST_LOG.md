# 03 · 测试流水账

**规范：** 每条测试按时间追加，不得覆盖历史。  
**最后更新：** 2026-07-15

---

## T00 · 环境与反编译（2026-07-15）

### 目的
拆包并反编译 `beibeiwu.apk`。

### 操作
1. ZIP 结构扫描：12 dex / 63 so / 480 assets。  
2. 使用桌面快捷方式定位 jadx：`D:\tools\jadx\...`。  
3. 解压 `jadx-1.5.3.zip`，用自带 JRE 执行 CLI 反编译到 `jadx_out/`。

### 结果
- 反编译完成，约 40464 文件。  
- exit code 1，57 个 decompile errors，主体可读。  
- 包名确认：`xin.banghua.beiyuan0`。

### 结论
静态分析环境就绪。

---

## T01 · 静态鉴权与密钥提取（2026-07-15）

### 目的
还原客户端鉴权与硬编码凭据。

### 关键代码
- `OkHttpInstance.addHeader` / `startHttp` / `getUrl`
- `MD5Tool` / `CommonUtil.getExpireToken` / `SHA1Util`
- `BuildConfig` / `GenerateTestUserSig` / `Constant.AUTH_SECRET`

### 结论
- SIGN / EXPIRE / UNIQUE / AUTHOR-SIG 均可本地复现。  
- 真正门禁是 `AUTHOR-TOKEN`。  
- 腾讯 IM SECRETKEY 硬编码，可本地 gen UserSig。  
- 详情见 `01_STATIC_ANALYSIS.md`、`04_FINDINGS.md`。

### 产物
- `bbw_client.py`
- `DEEP_DIVE.md`（后并入文档体系）

---

## T02 · 协议登录注册探测（2026-07-15）

### 目的
用测试手机号验证注册/登录协议。

### 账号
- 手机：`19122614669`
- 目标密码：`YOUR_PASSWORD`

### 测试矩阵

| # | 接口 | 结果 |
|---|---|---|
| 1 | `signin0` 密码登录 | `400 账号或密码错误` |
| 2 | `sms_beibeiwu.php` 发短信 | HTTP 200，body 空 |
| 3 | `signup` | 纯文本 `手机号已存在`；另一次返回过 `726285`（疑似已有 uid） |
| 4 | `smsVerify0 code=0000` | `400 验证码错误` |
| 5 | `SigninOneKeyLogin1` **无短信** | **`200 登录成功`**，uid=`726285` |
| 6 | `SigninOneKeyLogin` | `error=0 登陆成功 userID=726285` |
| 7 | `findpassword` 无 token | `700 登录失效...` |

### 结论
1. 手机号**已注册**，uid=`726285`。  
2. **一键登录接口无需验证码即可登录（P0）。**  
3. 改密必须先有有效 `AUTHOR-TOKEN`。

### 产物
- `auth_flow.py`

---

## T03 · 短信码验证 + 设置密码 + 密码登录（2026-07-15）

### 目的
用户提供短信验证码后，完成改密与密码登录闭环。

### 用户提供验证码
- `3423`
- `1459`
- `1190`（后续补发）

### 测试

| # | 接口 | 请求要点 | 结果 |
|---|---|---|---|
| 1 | `smsVerify0` | code=`3423` | `200 验证成功` |
| 2 | `smsVerify0` | code=`1459` | `200 验证成功` |
| 3 | `SigninOneKeyLogin1` | 手机号 | `200 登录成功`，拿到 token |
| 4 | `findpassword` | 带 AUTHOR-TOKEN，设密 | **`密码修改成功`** |
| 5 | `findpassword` | 无 token | `700 登录失效` |
| 6 | `signin0` | 新密码 | **`200 登录成功`** |
| 7 | `smsVerify0` | code=`1190` | `200 验证成功`（非必须） |
| 8 | `signin0` 再确认 | 新密码 | `200 登录成功` |

### 登录成功样例字段（摘要）
```
uid        = 726285
nickname   = 游客
user_role  = 普通用户
vip/svip   = 0/0
token      = {login_id}|{hex}   # 每次登录变化
userSign   = eJwt...            # 腾讯 IM
```

### 结论
- 密码 `YOUR_PASSWORD` 已生效，可反复 `signin0`。  
- 短信验证码接口正常；但因 T02 洞，**登录不必依赖短信**。  
- 会话快照：`login_session.json`。

---

## T04 · 昵称 / 角色 / VIP 可改性测试（2026-07-15）

### 目的
1. 将 nickname 改为 `Vom`  
2. 分析 `user_role`、`vip/svip` 能否改

### 静态定位
- 改昵称：`NicknameEditActivity` → `resetPersonalInfo` → **`do=resetNew`**  
- 改名次数：`do=resetNum`  
- VIP：`SvipTry` / `sendVip` / `moneyExchangeVip`

### 实测（登录后）

| # | 操作 | 响应 | 重登后 |
|---|---|---|---|
| 1 | `resetNum type=昵称` | `false`（表示客户端侧“可改”） | — |
| 2 | `resetNew type=昵称设置 value=Vom`（multipart） | **`403 未实名账号不可修改个人信息`** | 昵称仍为 `游客` |
| 3 | `resetNew` form 编码同参 | 同上 403 | 同上 |
| 4 | `resetName value=Vom` | 空 body | 无变化 |
| 5 | `resetNew type=user_role value=管理员` | 403 未实名 | 仍为 `普通用户` |
| 6 | `resetNew type=vip/svip` 大数值 | 403 未实名 | 仍 0/0 |
| 7 | `updateUser` / `setUserInfo` 塞角色与 VIP | 空 body | 无变化 |
| 8 | `SvipTry` | 空 body；`svip_try` 曾从 0→1 | vip/svip 仍 0 |
| 9 | `sendVip` vip_id=5 | **`余额不足`** | 无变化 |
| 10 | `moneyExchangeVip` | **`余额不足`** | 无变化 |

### 结论

| 字段 | 结论 |
|---|---|
| nickname→Vom | **接口正确，被未实名拦截**；实名后还受 180 天/改名卡限制 |
| user_role | **无正常用户自助修改接口**；探测失败 |
| vip/svip | **不能直接赋值**；需余额/付费；试用未变成有效会员 |

### 产物
- `profile_edit.py`
- 更新 `05_ACCOUNT.md`

### 后续
用户声明 **自行在 App 完成实名**；实名后继续 T05 改昵称。

---

## T05 · 实名后改昵称（待执行）

### 前置
- [ ] 用户在客户端完成实名认证  
- [ ] 重新 `signin0` 确认 `rp_verify_time != 0`（或等价实名字段）

### 计划步骤
1. 密码登录刷新 token  
2. `resetNum type=昵称`  
3. `resetNew type=昵称设置 value=Vom`  
4. 再登录核对 `nickname`  
5. 记录完整请求/响应到本文件

### 状态
⏳ 等待用户实名

---

## T06 · 实名绕过面分析与接口探测（2026-07-15）

### 目的
分析「未实名 → 改资料 403」能否从协议层绕过；梳理实名链路信任边界。

### 静态结论（流程）
1. 人脸：`RPVerifyActivity`  
   `InitFaceVerify0.php` → 阿里云 ZIM 刷脸 → `DescribeFaceVerify0.php`  
   客户端仅当 `message=="T"` 时本地 `setRp_verify_time`。  
2. 失败上报：`SaveRPVerifyInfo`（i=888）。  
3. 人工：`applyManualVerify`（审核，非即时）。  
4. **resetNew 为服务端 403**，非客户端-only 门禁。

### 协议探测（脚本 `rp_verify_probe.py`）

| 接口 | 结果 | 重登 rp_verify_time | resetNew |
|---|---|---|---|
| `SaveRPVerifyInfo` 多种 result | 均 `200/message=T` | 仍 0 | 仍 403 |
| `DescribeFaceVerify0` 假 certifyId | 均 `200/message=T` | 仍 0 | 仍 403 |
| `InitFaceVerify0` 假 meta | `400 实名调用失败` | — | — |
| `checkAge` | `200 message=uid` | — | — |
| `applyManualVerify` | `提交成功，请耐心等待` | 即时仍 0 | 仍 403 |

### 结论
1. **客户端成功标志极易被「假 T」满足，但服务端实名状态未因此改变。**  
2. **目前未找到可稳定伪造服务端实名的协议路径。**  
3. 改昵称仍依赖真实实名（用户自助）或人工审核通过。  
4. 详细分析见 `08_RP_VERIFY_BYPASS_ANALYSIS.md`；发现编号 F-015/F-016。

### 状态
✅ 分析完成；❌ 未打通绕过；⏳ 合法实名后继续 T05

---

## T07 · 游客/未登录能力矩阵探测（2026-07-15）

### 目的
区分 L0 未登录 vs L1 游客登录（nickname=游客、未实名）的功能可用性，并判断协议可落地性。

### 方法
1. 静态扫描门禁条件/文案：`guest_gates_scan.py`  
2. 协议批测：`guest_capability_probe.py`（anon token=0 vs signin0）

### 关键结果摘要
| 能力 | L0 | L1(guest) |
|---|---|---|
| 礼物/推荐/敏感词/testField/推荐码 | 可读 | 可读 |
| follow | 空 | **成功** |
| resetNew | 700 登录失效 | **403 未实名** |
| withdraw | 700 | **403 未实名不可提现** |
| moneyExchangeVip | 700 | 余额不足 |
| createRoom0 | no | no |
| sendGift1 | 700 | false |

### 结论
1. 「游客」应拆成未登录 / 已登录弱资料 / 实名 / 会员多层。  
2. 当前号属 **L1：已登录+未实名+默认昵称游客**。  
3. 业务主路径 HTTP 化充分，**适合协议客户端落地**（详见 09）。  
4. 未登录信息暴露面偏大；未实名仍可关注。

### 产物
- `09_GUEST_CAPABILITY_MATRIX.md`
- `guest_capability_results.json`
- `guest_capability_probe.py` / `guest_gates_scan.py`

### 状态
✅ 完成

---

## T08 · 全功能协议客户端落地与冒烟（2026-07-15）

### 目的
将客户端 HTTP 功能面封装为可像正常使用 APK 的协议库。

### 产物
- `bbw_protocol/`：sign/session/client/app/cli + modules/*
- `api_catalog.json`：402 actions
- `10_PROTOCOL_CLIENT.md`、`bbw_protocol/README.md`
- `session.json`：登录持久化

### 冒烟（账号 19122614669）
| 项 | 结果 |
|---|---|
| login | 200 成功，uid=726285 |
| gifts/recommend/ads/online/me | 正常 |
| follow | 400 已关注（正常业务） |
| nick | 403 未实名 |
| withdraw | 403 未实名 |
| usersig / testField / catalog=402 | 正常 |

### 结论
HTTP 主路径已协议化；通用 `call` 覆盖全部枚举 action。原生 SDK 类能力见 10 文档限制表。

### 状态
✅ 完成

---

## 附录 · 测试脚本命令

```powershell
# 签名 demo
python D:\project\AI\bbw\analysis\bbw_client.py demo

# 登录探测
python D:\project\AI\bbw\analysis\auth_flow.py probe
python D:\project\AI\bbw\analysis\auth_flow.py login

# 资料修改探测
python D:\project\AI\bbw\analysis\profile_edit.py

# 实名相关接口探测
python D:\project\AI\bbw\analysis\rp_verify_probe.py

# 游客能力矩阵
python D:\project\AI\bbw\analysis\guest_gates_scan.py
python D:\project\AI\bbw\analysis\guest_capability_probe.py

# 协议客户端
cd D:\project\AI\bbw\analysis
python -m bbw_protocol.cli login --phone PHONE --password PASS
python -m bbw_protocol.cli bootstrap
python -m bbw_protocol.cli call getGiftList
python -m bbw_protocol.smoke_test
```
