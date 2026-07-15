# xbly

小贝乐园 / 贝贝屋（`xin.banghua.beiyuan0`）的 **协议逆向、HTTP 客户端与多用户 Web 壳**。

| | |
|---|---|
| 包名 | `xin.banghua.beiyuan0` |
| 客户端版本 | **154**（`xbly.apk`）· 基线 148（`beibeiwu.apk`） |
| 后端 | 微擎 `do=` · `applet.banghua.xin` / `redis.banghua.xin` |
| 依赖 | Python 3 · 仅标准库 |

> 仅供安全研究、CTF 与**自有账号**协议验证。禁止未授权访问、扫号、伪造实名/支付。

---

## 功能一览

| 能力 | 状态 | 入口 |
|---|---|---|
| 签名与会话（SIGN / EXPIRE / AUTHOR） | ✅ 可本地复现 | `bbw_protocol.sign` |
| 登录 / 短信 / 改密 / 一键登录 | ✅ | `cli login` · `auth` |
| ~403 个业务 action 通用调用 | ✅ | `cli call` · `app.call` |
| 匹配 / 任务 / 资料 / 社交 / 房间 | ✅ HTTP | 各 `modules/*` |
| IM 凭证（腾讯 UserSig / 融云） | ✅ 凭证 | `app.native.im` |
| IM 实时收发 | ⚠️ 需官方 SDK | `bbw_web` + TIM |
| 刷脸实名 | ⚠️ 仅 HTTP 编排 | `app.native.face` · 活体靠阿里云 |
| 支付下单 | ⚠️ 仅 order 参数 | `app.native.pay` · 收银官方 |
| 全功能 Web App（PC/手机） | ✅ | `python -m bbw_web` · 10 大模块对齐 APK |

---

## 仓库结构

```
xbly/
├── README.md              ← 本文件
├── .gitignore
├── bbw_protocol/          # 协议核（无 Web 依赖）
│   ├── sign / session / client / app / cli
│   ├── modules/           # auth profile social match …
│   └── adapters/          # IM · face · pay 边车
├── bbw_web/               # 多用户 BFF + 静态页（与核隔离）
│   ├── store.py           # web_sid → BeibeiwuApp
│   ├── bff_server.py
│   └── static/
├── docs/                  # 分析文档 + api_catalog.json
├── tools/                 # 可选早期探测脚本
├── session.json           # CLI 会话（本地，不入库）
└── sessions/              # Web 多用户会话（本地，不入库）
```

**隔离约定：** `bbw_protocol` 只做协议；`bbw_web` 负责 Cookie / 多租户 / 页面。核不依赖 Web。

---

## 快速开始

```powershell
git clone https://github.com/shixian64/xbly.git
cd xbly

# —— 协议 CLI ——
python -m bbw_protocol.cli login --phone YOUR_PHONE --password YOUR_PASS
python -m bbw_protocol.cli whoami
python -m bbw_protocol.cli bootstrap
python -m bbw_protocol.cli gifts
python -m bbw_protocol.cli me
python -m bbw_protocol.cli call getGiftList
python -m bbw_protocol.cli native-status
python -m bbw_protocol.cli im-tim
python -m bbw_protocol.cli repl

# —— 全功能 Web App（简洁 UI · PC 侧栏 / 手机底栏）——
python -m bbw_web --port 8765
# http://127.0.0.1:8765/
# 首页·广场·匹配·社交·房间·消息·钱包·任务·我的·协议台
```

```python
from bbw_protocol import BeibeiwuApp

app = BeibeiwuApp.load()
app.auth.login_password("phone", "password")
app.save()

print(app.whoami())
print(app.content.gift_list().message)
print(app.native.im.tim_login_payload())   # 给 TIM Web SDK
# app.call("AnyDoAction", foo="bar")       # 逃生舱，覆盖 catalog
```

会话默认写入仓库根目录 `session.json`（已 gitignore）。

---

## 架构（简）

```
Browser SPA（简洁浅色 · PC 侧栏 / 手机底栏）
        │ Cookie bbw_sid
        ▼
   bbw_web BFF（全功能语义 API + 多用户）
        │
        ▼
   BeibeiwuApp ── adapters ──► TIM / 刷脸 / 支付 SDK（可选）
        │
        ▼
   banghua HTTP（与 APK 相同 do= / 签名 / token）
```

- **Web 目标**：APK 主功能域（首页/广场/匹配/社交/房间/消息/钱包/任务/我的）均可在浏览器操作。  
- **协议台**：高级用户可直接调任意 `do=`（约 400+）。  
- **原生边界**：IM 长连接 / 刷脸活体 / 微信收银仍依赖厂商 SDK 或官方 App。

---

## 研究结论摘要

详见 [docs/04_FINDINGS.md](docs/04_FINDINGS.md)。

| ID | 要点 | 级别 |
|---|---|---|
| F-001 | `SigninOneKeyLogin1` 可仅凭手机号登录 | P0 |
| F-002 | 腾讯 IM SECRETKEY 硬编码，可本地 gen UserSig | P0 |
| F-003 | SIGN/EXPIRE 等客户端签名可完全复现 | P0 |
| — | 未实名硬门禁：改资料 / 提现等服务端 403 | 业务 |
| — | 假刷脸 certifyId 不改 `rp_verify_time` | 服务端有效 |
| — | v154 跟版：版本号 / `Id2MetaVerifyRequest` / 小说接口下线 | 见 [13](docs/13_APK_V154_DIFF.md) |

**不能指望协议完成的：** 真实刷脸通过、微信/支付宝资金到账、无 SDK 的 IM 长连接。

---

## 文档

完整索引 → **[docs/README.md](docs/README.md)**

| 文档 | 内容 |
|---|---|
| [00 总览](docs/00_OVERVIEW.md) | 目标、进度、结构 |
| [01 静态分析](docs/01_STATIC_ANALYSIS.md) | 结构、密钥、接口 |
| [02 协议](docs/02_PROTOCOL.md) | Header、登录、资料 |
| [03 测试日志](docs/03_TEST_LOG.md) | T01–T13 流水账 |
| [04 风险结论](docs/04_FINDINGS.md) | P0–P3 |
| [08 实名](docs/08_RP_VERIFY_BYPASS_ANALYSIS.md) | 刷脸链路与绕过面 |
| [09 游客矩阵](docs/09_GUEST_CAPABILITY_MATRIX.md) | L0/L1 能力 |
| [10 协议客户端](docs/10_PROTOCOL_CLIENT.md) | CLI / API 说明 |
| [11 功能与实名覆盖](docs/11_FEATURE_REALNAME_AND_COVERAGE.md) | 门禁与覆盖 |
| [12 原生集成](docs/12_NATIVE_INTEGRATION.md) | IM / 刷脸 / 支付 |
| [13 v154 diff](docs/13_APK_V154_DIFF.md) | 148→154 |
| [api_catalog.json](docs/api_catalog.json) | action 目录 |

包内手册：

- [bbw_protocol/README.md](bbw_protocol/README.md)
- [bbw_web/README.md](bbw_web/README.md)
- [docs/06_TOOLS.md](docs/06_TOOLS.md) — 命令速查

---

## 安全与隐私

- **不入库：** `*.apk`、`session.json`、`sessions/`、密码、有效 token（见 `.gitignore`）。
- 文档中的测试手机号/uid 仅作研究记录；公开仓库勿写明文密码。
- 第三方密钥硬编码为客户端侧问题，记录在 Findings，勿二次传播滥用。
- 默认 BFF 绑定 `127.0.0.1`，勿对公网裸奔。

---

## License

仅供学习与授权测试。使用后果自负。
