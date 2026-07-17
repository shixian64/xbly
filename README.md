# xbly

小贝乐园 / 贝贝屋（`xin.banghua.beiyuan0`）的 **协议逆向、HTTP 客户端与多用户 Web 壳**。

| | |
|---|---|
| 包名 | `xin.banghua.beiyuan0` |
| 客户端版本 | **154**（`xbly.apk`）· 基线 148（`beibeiwu.apk`） |
| 后端 | 微擎 `do=` · `applet.banghua.xin` / `redis.banghua.xin` |
| 依赖 | 协议核为 Python 3 标准库；生产 Web 依赖 FastAPI、PostgreSQL、Redis、R2 等 |

> 仅供安全研究、CTF 与**自有账号**协议验证。禁止未授权访问、扫号、伪造实名/支付。

---

## 功能一览

| 能力 | 状态 | 入口 |
|---|---|---|
| 签名与会话（SIGN / EXPIRE / AUTHOR） | ✅ 可本地复现 | `bbw_protocol.sign` |
| 登录 / 短信 / 改密 / 一键登录 | ✅ | `cli login` · `auth` |
| 398 个 v154 活跃 action（catalog 405，含 7 个历史下线项） | ✅ 名称面/调用器 | `app.call*` · `call_url` · multipart |
| 匹配 / 任务 / 资料 / 社交 / 房间 | ✅ HTTP | 各 `modules/*` |
| IM 凭证（腾讯 UserSig / 融云） | ✅ 凭证 | `app.native.im` |
| IM 实时收发 | ⚠️ 需官方 SDK | `bbw_web` + TIM |
| 刷脸实名 | ⚠️ 仅 HTTP 编排 | `app.native.face` · 活体靠阿里云 |
| 支付下单 | ⚠️ 仅 order 参数 | `app.native.pay` · 收银官方 |
| 产品化 Web App（PC/手机） | ✅ 主流程 | 本地：`python -m bbw_web`；生产：`compose.yaml` + `bbw_web.api` |
| 多用户持久化与管理端 | ✅ | PostgreSQL、Redis、私有 R2；管理入口 `/admin` |

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
│   ├── api.py             # FastAPI 生产入口
│   ├── admin_api.py       # 单超级管理员、TOTP、审计和数据管理 API
│   └── static/
├── bbw_prod/              # PostgreSQL 模型、加密、Session 和业务服务
├── migrations/            # Alembic 数据库迁移
├── compose.yaml           # App/PostgreSQL/Redis/RQ/Caddy 单机部署
├── docs/                  # 分析文档 + api_catalog.json
├── tools/                 # 可选早期探测脚本
├── session.json           # CLI 会话（本地，不入库）
└── sessions/              # Web 多用户会话（本地，不入库）
```

**隔离约定：** `bbw_protocol` 只做协议；`bbw_web` 负责 Cookie / 多租户 / 页面。核不依赖 Web。

生产部署、Secret、Cloudflare R2、资源预算和验收步骤见
[docs/14_PRODUCTION_DEPLOYMENT.md](docs/14_PRODUCTION_DEPLOYMENT.md)。不要把本地
`python -m bbw_web` 的内存会话模式直接暴露到公网。

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

# —— 产品化 Web App（PC 侧栏 / 手机五项底栏）——
python -m bbw_web --port 8765
# http://127.0.0.1:8765/
# 这是 memory-only 本地入口，不注册管理 API；只看管理页面可访问：
# http://127.0.0.1:8765/static/admin.html
# 身边·消息·匹配·动态·我的；侧栏按所属主模块展开二级入口
# 手机录音需要受信任的 HTTPS 安全上下文，部署说明见 bbw_web/README.md
# 授权研究时才使用：python -m bbw_web --enable-lab
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

## Windows 本地测试管理端

`py -3 -m bbw_web --port 8765` 启动的是旧的内存会话入口，只提供用户端和旧 BFF，不能登录管理端：

- 只查看管理界面：`http://127.0.0.1:8765/static/admin.html`
- 完整测试管理端：必须启动 `bbw_web.api:app`，并准备 PostgreSQL、Redis 和本地 Secret。

以下命令均在仓库根目录的同一个 PowerShell 窗口执行。先停止占用 8765 端口的旧服务。

### 1. 启动本地 PostgreSQL 和 Redis

首次运行：

```powershell
docker run -d --name bbw-pg-local `
  -p 127.0.0.1:55432:5432 `
  -e POSTGRES_USER=bbw `
  -e POSTGRES_PASSWORD=BbwLocalDb_2026 `
  -e POSTGRES_DB=bbw `
  -v bbw-pg-local-data:/var/lib/postgresql/data `
  postgres:16-alpine

docker run -d --name bbw-redis-local `
  -p 127.0.0.1:56379:6379 `
  -v bbw-redis-local-data:/data `
  redis:7.4-alpine redis-server --appendonly yes
```

容器已经创建过时直接启动：

```powershell
docker start bbw-pg-local bbw-redis-local
```

安装生产 Web 依赖：

```powershell
py -3 -m pip install -r requirements.txt
```

### 2. 生成仅用于本机的 Secret

本地 Secret 使用 `local_*` 文件名，位于已经被 Git 忽略的 `docker/secrets/`，不会覆盖生产文件：

```powershell
$secretDir = Join-Path $PWD "docker\secrets"
New-Item -ItemType Directory -Force $secretDir | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)

function New-LocalKeyFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) {
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        [IO.File]::WriteAllText($path, [Convert]::ToBase64String($bytes), $utf8)
    }
}

New-LocalKeyFile "$secretDir\local_app_master_key"
New-LocalKeyFile "$secretDir\local_phone_hmac_key"
New-LocalKeyFile "$secretDir\local_session_hmac_key"

if (-not (Test-Path "$secretDir\local_credential_keyring")) {
    [IO.File]::WriteAllText("$secretDir\local_credential_keyring", "{}", $utf8)
}
if (-not (Test-Path "$secretDir\local_admin_initial_password")) {
    [IO.File]::WriteAllText(
        "$secretDir\local_admin_initial_password",
        "BbwLocal#2026Test",
        $utf8
    )
}
```

不要在数据库已有密文后删除或重新生成这三个密钥文件，否则旧账号凭据和管理端 TOTP 将无法解密。

### 3. 配置本地运行环境

```powershell
$env:BBW_ENV = "development"
$env:BBW_DATABASE_URL = "postgresql+psycopg://bbw:BbwLocalDb_2026@127.0.0.1:55432/bbw"
$env:BBW_REDIS_URL = "redis://127.0.0.1:56379/0"

$env:BBW_CREDENTIAL_MASTER_KEY_FILE = "$secretDir\local_app_master_key"
$env:BBW_CREDENTIAL_KEYS_FILE = "$secretDir\local_credential_keyring"
$env:BBW_PHONE_HMAC_KEY_FILE = "$secretDir\local_phone_hmac_key"
$env:BBW_SESSION_HMAC_KEY_FILE = "$secretDir\local_session_hmac_key"

$env:BBW_ADMIN_INITIAL_USERNAME = "admin"
$env:BBW_ADMIN_INITIAL_PASSWORD_FILE = "$secretDir\local_admin_initial_password"

# 本地使用 HTTP，不能使用生产环境的 Secure/__Host- Cookie。
$env:BBW_COOKIE_SECURE = "false"
$env:BBW_USER_COOKIE_NAME = "bbw_sid"
$env:BBW_ADMIN_COOKIE_NAME = "bbw_admin_sid"
$env:BBW_TRUST_PROXY_HEADERS = "false"
```

这些环境变量只对当前 PowerShell 窗口有效；重新打开终端后需要再次设置。

### 4. 初始化数据库并启动完整 Web

```powershell
py -3 -m alembic upgrade head

py -3 -m uvicorn bbw_web.api:app `
  --host 127.0.0.1 `
  --port 8765 `
  --workers 1
```

打开：

```text
用户端：http://127.0.0.1:8765/
管理端：http://127.0.0.1:8765/admin
```

首次空数据库的管理账号：

```text
用户名：admin
密码：BbwLocal#2026Test
```

初始密码只在数据库不存在管理员时使用。修改管理密码后，后续登录应使用新密码。未配置 Cloudflare R2 时，可以测试管理员、邀请码、用户、聊天、关系、活动和审计功能，但媒体上传及临时访问地址不可用。

---

## Ubuntu 服务器部署

生产部署的完整说明、Cloudflare/R2 配置、安全边界、备份和上线检查表见
[docs/14_PRODUCTION_DEPLOYMENT.md](docs/14_PRODUCTION_DEPLOYMENT.md)。下面是首次部署的最短流程。

推荐环境：Ubuntu 24.04 LTS、3 vCPU、2 GB RAM、30 GB 磁盘并配置 Swap。服务器需要提前安装 Docker Engine 和 Docker Compose Plugin，并准备：

- 一个已经接入 Cloudflare DNS 代理的域名。
- 一个保持私有的 Cloudflare R2 Bucket。
- 仅限该 Bucket 的 R2 Access Key ID 和 Secret Access Key。
- 当前有效的腾讯 IM Secret Key 与 RoomKit Business Token。

### 1. 获取项目并填写非敏感配置

```bash
git clone https://github.com/shixian64/xbly.git
cd xbly

cp .env.example .env
nano .env
chmod 600 .env
```

`.env` 至少需要填写：

```dotenv
TZ=Asia/Shanghai
BBW_DATA_ROOT=/var/lib/bbw
APP_DOMAIN=你的正式域名
ACME_EMAIL=你的证书联系邮箱
POSTGRES_DB=bbw
POSTGRES_USER=bbw
R2_ACCOUNT_ID=你的Cloudflare账户ID
R2_BUCKET=你的私有Bucket名称
TURNSTILE_SITE_KEY=
ADMIN_INITIAL_USERNAME=admin
APP_IMAGE=bbw-app:local
```

密码、主密钥、协议密钥、R2 Secret 和 Turnstile Secret 禁止写入 `.env`。

### 2. 初始化 Docker Secret 和宿主机目录

```bash
chmod 700 docker/init-secrets.sh docker/prepare-host.sh
bash docker/init-secrets.sh
```

脚本会：

- 随机生成 PostgreSQL 密码、凭据主密钥、HMAC 密钥和初始管理员密码。
- 交互录入腾讯 IM、RoomKit、R2 凭据。
- 可选录入 Turnstile Secret；未启用时直接回车。

然后设置 Secret 文件权限并准备 Caddy 持久目录：

```bash
find docker/secrets -maxdepth 1 -type f \
  ! -name README.md ! -name .gitignore \
  -exec chmod 444 {} \;

sudo env BBW_DATA_ROOT=/var/lib/bbw bash docker/prepare-host.sh
```

`BBW_DATA_ROOT` 必须与 `.env` 中的值一致。

### 3. 检查、构建并启动

```bash
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

首次启动顺序为 PostgreSQL、Redis、数据库迁移、App/Worker/Scheduler、Caddy。查看日志：

```bash
docker compose logs --tail 100 postgres redis migrate app worker scheduler caddy
```

验证服务：

```bash
curl -fsS "https://你的正式域名/api/health"
docker compose exec postgres sh -c \
  'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose exec redis redis-cli ping
```

访问入口：

```text
用户端：https://你的正式域名/
管理端：https://你的正式域名/admin
```

初始管理员用户名来自 `.env` 的 `ADMIN_INITIAL_USERNAME`，初始密码位于：

```bash
cat docker/secrets/admin_initial_password
```

首次登录后应立即修改管理员密码、绑定身份验证器，并更换 bootstrap 初始密码文件。不要把初始密码输出到日志或发送到聊天工具。

### 4. 上线前必须确认

- 云安全组和防火墙不开放 `8000`、`5432`、`6379`，公网只开放 `80/443` 和受限 SSH。
- R2 Bucket 没有公共开发 URL，浏览器只能获得最长 5 分钟的签名读取地址。
- `/admin` 已完成修改密码、TOTP、邀请码、用户启停和敏感查看审计测试。
- 已生成至少一份 PostgreSQL 手工备份，并离线保存全部 Docker Secret。
- 已在上游控制台轮换历史上曾进入源码的协议密钥。

自动异地备份、自动主密钥轮换、真实 Cloudflare/R2/上游端到端联调仍需按照生产部署文档中的上线检查表执行。

---

## 架构（简）

```
Browser SPA（社交娱乐风 · PC 侧栏 / 手机五项底栏）
        │ HttpOnly + SameSite=Strict Cookie
        ▼
   bbw_web BFF（全功能语义 API + 多用户）
        │
        ▼
   BeibeiwuApp ── adapters ──► TIM / 刷脸 / 支付 SDK（可选）
        │
        ▼
   banghua HTTP（与 APK 相同 do= / 签名 / token）
```

- **Web 目标**：按 APK 的“身边 / 消息 / 匹配 / 动态 / 我的”组织主流程；匹配页顶部通过“匹配 / 语音房”标签切换下方功能区，关系中心、钱包与会员、任务与奖励归入我的。
- **产品与研究隔离**：默认关闭协议台、任意 action、会话列表和弱一键登录；仅 `--enable-lab` 显式开启。
- **会话安全**：SID 只存在 HttpOnly Cookie；CORS 默认关闭。本地入口默认使用内存会话，生产 FastAPI 入口使用 PostgreSQL 与 Redis 持久 Session。
- **原生边界**：IM 长连接 / 刷脸活体 / 微信收银仍依赖厂商 SDK 或官方 App。

---

## 研究结论摘要

详见 [docs/04_FINDINGS.md](docs/04_FINDINGS.md)。

| ID | 要点 | 级别 |
|---|---|---|
| F-001 | `SigninOneKeyLogin1` 可仅凭手机号登录 | P0 |
| F-002 | 历史客户端/源码曾包含腾讯 IM SECRETKEY，可本地 gen UserSig；生产运行时已迁移 Docker Secret，旧值仍需上游轮换 | P0 |
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
| [14 生产部署](docs/14_PRODUCTION_DEPLOYMENT.md) | Docker、PostgreSQL、Redis、R2、管理端与运维 |
| [api_catalog.json](docs/api_catalog.json) | action 目录 |

包内手册：

- [bbw_protocol/README.md](bbw_protocol/README.md)
- [bbw_web/README.md](bbw_web/README.md)
- [docs/06_TOOLS.md](docs/06_TOOLS.md) — 命令速查

---

## 安全与隐私

- **不进入 Git/镜像：** `*.apk`、`session.json`、`sessions/`、Docker Secret 和本地运行数据（见 `.gitignore`、`.dockerignore`）。
- 生产数据库会按已确认需求保存上游账号、密码和 Token，但仅保存 AES-256-GCM 信封密文；管理员查看明文必须通过密码、TOTP 和审计理由解锁。
- 文档和工具中的测试手机号/uid 已改为占位符；真实账号、密码和 Token 只能通过本地环境或 Secret 提供。
- 第三方密钥硬编码为客户端侧问题，记录在 Findings，勿二次传播滥用。
- 默认 BFF 绑定 `127.0.0.1`，勿对公网裸奔。

---

## License

仅供学习与授权测试。使用后果自负。
