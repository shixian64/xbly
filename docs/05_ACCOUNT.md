# 05 · 测试账号与会话状态

**最后更新：** 2026-07-15  
**用途：** 协议测试状态记录。密码与有效 token **不入库**。

---

## 1. 账号（公开研究字段）

| 项 | 值 |
|---|---|
| 手机号（研究用） | `YOUR_PHONE` |
| 登录密码 | **本地自管，勿写入仓库** |
| 用户 uid | `YOUR_UID` |
| 包名 | `xin.banghua.beiyuan0` |
| version_code | `154` |

登录：

```powershell
python -m bbw_protocol.cli login --phone YOUR_PHONE --password "YOUR_PASSWORD"
python -m bbw_protocol.cli whoami
```

---

## 2. 当前资料快照

| 字段 | 值 | 备注 |
|---|---|---|
| nickname | `游客` | 目标 `Vom`；未实名 → `resetNew` 403 |
| user_role | `普通用户` | 无自助改角色 |
| vip / svip | `0` / `0` | 需付费或后台 |
| money | `0` | |
| 实名 | 未实名 | 用户自行在 App 完成 |

---

## 3. 会话

- `AUTHOR-TOKEN` 每次登录变化，格式 `{login_id}|{hex}`  
- 持久化：`session.json`（gitignore）  
- Web 多用户：`sessions/{uid}.json`（gitignore）

---

## 4. 已验证登录方式

| 方式 | 接口 | 状态 |
|---|---|---|
| 密码 | `signin0` | ✅ |
| 无短信一键 | `SigninOneKeyLogin1` | ✅（漏洞，见 Findings F-001） |
| 短信校验 | `smsVerify0` | ✅ |
| 改密 | `findpassword`（需 token） | ✅ |

---

## 5. 改昵称状态

未实名 → 403。实名后：`resetNum` → `resetNew type=昵称设置`。  
伪造 `Describe/Save` 不能改 `rp_verify_time`（见 08）。

---

## 6. 安全

- 勿在公开仓库写密码 / 有效 token。  
- IM SECRETKEY 等客户端硬编码见 [04_FINDINGS.md](./04_FINDINGS.md)。
