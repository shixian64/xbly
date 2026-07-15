# 05 · 测试账号与会话状态

**最后更新：** 2026-07-15  
**用途：** CTF 本地协议测试。勿对外传播。

---

## 1. 账号凭据

| 项 | 值 |
|---|---|
| 手机号 | `19122614669` |
| 登录密码 | `YOUR_PASSWORD`（协议 `findpassword` 设置成功） |
| 用户 uid | `726285` |
| 包名 | `xin.banghua.beiyuan0` |
| version_code | `148` |

---

## 2. 当前资料快照

> 来源：最近一次 `signin0` 成功响应（见 `login_session.json`）

| 字段 | 当前值 | 目标 | 能否协议修改 | 备注 |
|---|---|---|---|---|
| nickname | `游客` | `Vom` | **受限** | 接口 `resetNew` 正确，**未实名 403** |
| user_role | `普通用户` | — | **基本否** | 无自助改角色接口 |
| vip | `0` | — | **否（直接写）** | 需付费/兑换/后台 |
| svip | `0` | — | **否（直接写）** | 同上 |
| name_card | `0` | — | 未测获取途径 | 改名卡 |
| advanced_user | `0` | — | 未找到自助接口 | |
| money | `0.00` | — | — | sendVip 因此失败 |
| svip_try | 曾变为 `1` | — | `SvipTry` 有副作用 | 未变成有效 svip |
| rp_verify / 实名 | 未实名 | 用户自行完成 | App 内操作 | **阻断改资料** |
| portrait | 默认图路径 | — | 未测 | `resetNew type=头像设置` |
| signature | 默认签名 | — | 未测 | 可能同类 resetNew |

---

## 3. 会话 Token

`AUTHOR-TOKEN` **每次登录会变**，格式：

```
{login_id}|{40位hex}
```

示例（历史，可能已失效）：

```
LOGIN_ID|TOKEN_HEX
LOGIN_ID|TOKEN_HEX
```

刷新方式：

```powershell
python D:\project\AI\bbw\analysis\auth_flow.py login
# 或 profile_edit.py / 自定义 signin0
```

完整用户 JSON：`analysis/login_session.json`

---

## 4. 游客态能力（本号 = L1）

完整矩阵见 `09_GUEST_CAPABILITY_MATRIX.md`。

**可用（协议已测）：** 登录/改密、公开列表读取、关注 follow、心跳、改名次数查询、testField 等。  

**不可用：** 改昵称/提现（403 未实名）、兑 VIP（余额不足）、创建房间（no）、VIP 特权、卡类玩法（卡=0）。  

**身份标签：** nickname=游客，但 phone 已绑定（返回脱敏），并非「未绑定手机的纯游客」。

## 5. 已验证可用的登录方式

| 方式 | 命令/接口 | 状态 |
|---|---|---|
| 密码登录 | `signin0` + 上表密码 | ✅ |
| 无短信一键登录 | `SigninOneKeyLogin1` + 手机号 | ✅（漏洞） |
| 短信验证 | `smsVerify0` | ✅（验证码由用户提供） |
| 改密 | `findpassword`（需 token） | ✅ |

---

## 6. 改昵称状态机

```
[未实名] --resetNew--> 403 未实名账号不可修改个人信息
    |
    | 用户在 App 完成实名
    v
[已实名] --resetNum--> false=免费次数可用 / JSON=需改名卡
    |
    +-- 有次数或改名卡 --resetNew type=昵称设置 value=Vom--> 期望: 设置成功
    |
    +-- 无次数且无卡 --> 改名卡不足 / 引导商城
```

**当前卡在：未实名。**  
用户声明：**实名认证自己操作。**

协议侧尝试伪造实名（Save/Describe 假参）：**失败**（详见 `08_RP_VERIFY_BYPASS_ANALYSIS.md` / T06）。  
`DescribeFaceVerify0` / `SaveRPVerifyInfo` 虽常返回 `message=T`，但重登后 `rp_verify_time` 仍为 `0`。

实名完成后检查清单：

1. 重新登录，记录 `rp_verify_time` / 实名相关字段  
2. 执行 `resetNew` 改 `Vom`  
3. 更新本节表格 + `03_TEST_LOG.md` T05

---

## 7. VIP 状态机（简）

```
vip/svip=0
  ├─ SvipTry        → 可能改 svip_try，未必给会员时长
  ├─ moneyExchangeVip → 需 money>0
  ├─ sendVip          → 需 money 足够（按 vip_id 扣费）
  └─ 微信/支付宝下单   → 真支付
```

当前 money=0 → 付费类失败。

---

## 8. 安全注意

- 本文件含测试账号密码与历史 token，仅限本机 CTF。  
- 生产环境请轮换密码；IM 密钥泄露问题见 `04_FINDINGS.md` F-002。
