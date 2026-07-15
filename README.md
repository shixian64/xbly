# xbly — 小贝乐园 / 贝贝屋 协议分析与客户端

对 `xin.banghua.beiyuan0`（小贝乐园）做的 **静态逆向 + 协议还原 + 协议客户端 + 多用户 Web 壳**。

| 项 | 值 |
|---|---|
| 包名 | `xin.banghua.beiyuan0` |
| 当前客户端版本 | **154**（`xbly.apk`） |
| 基线 | 148（`beibeiwu.apk`） |
| 后端 | 微擎 `do=` @ `applet.banghua.xin` / `redis.banghua.xin` |

> 仅供安全研究 / CTF / 自有账号协议验证。勿用于未授权访问。

---

## 目录结构

```
xbly/
├── README.md                 # 本文件
├── .gitignore
├── bbw_protocol/             # 协议核（无 Web 依赖）
├── bbw_web/                  # 多用户 BFF + 前端（隔离于协议核）
├── docs/                     # 全部分析文档 + api_catalog.json
├── tools/                    # 早期探测 / 枚举脚本（可选）
├── session.json              # 本地 CLI 会话（gitignore）
├── sessions/                 # Web 多用户会话（gitignore）
└── *.apk                     # 本地保留，不入库
```

| 模块 | 职责 |
|---|---|
| **bbw_protocol** | 签名、会话、HTTP 业务、adapters（IM/face/pay 凭证） |
| **bbw_web** | Cookie `bbw_sid` 多用户、BFF、演示页 |
| **docs** | 01–13 分析文档、action 目录 |
| **tools** | 可选历史脚本，主路径请用 protocol CLI |

---

## 快速开始

```powershell
cd /path/to/xbly

# 协议 CLI
python -m bbw_protocol.cli login --phone YOUR_PHONE --password YOUR_PASS
python -m bbw_protocol.cli whoami
python -m bbw_protocol.cli bootstrap
python -m bbw_protocol.cli call getGiftList

# 多用户 Web（默认 127.0.0.1:8765）
python -m bbw_web --port 8765
```

```python
from bbw_protocol import BeibeiwuApp

app = BeibeiwuApp.load()
app.auth.login_password("phone", "password")
app.save()
print(app.whoami())
print(app.native.im.tim_login_payload())
```

---

## 文档入口

→ **[docs/README.md](docs/README.md)**（完整索引）

| 文档 | 内容 |
|---|---|
| [docs/00_OVERVIEW.md](docs/00_OVERVIEW.md) | 总览与进度 |
| [docs/02_PROTOCOL.md](docs/02_PROTOCOL.md) | 协议与签名 |
| [docs/04_FINDINGS.md](docs/04_FINDINGS.md) | 风险结论 |
| [docs/10_PROTOCOL_CLIENT.md](docs/10_PROTOCOL_CLIENT.md) | 协议客户端 |
| [docs/12_NATIVE_INTEGRATION.md](docs/12_NATIVE_INTEGRATION.md) | IM / 刷脸 / 支付 |
| [docs/13_APK_V154_DIFF.md](docs/13_APK_V154_DIFF.md) | v154 跟版 |

包内说明：

- [bbw_protocol/README.md](bbw_protocol/README.md)
- [bbw_web/README.md](bbw_web/README.md)

---

## 安全与隐私

- **APK、session、密码不入库**（见 `.gitignore`）。
- 文档中的测试账号手机号用于研究记录；公开仓库请勿写入明文密码。
- 客户端内硬编码的第三方密钥见 `docs/04_FINDINGS.md`（厂商侧问题，研究记录）。

---

## License

仅供学习与授权测试。使用后果自负。
