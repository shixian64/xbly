# Web 与协议核生产部署方案

本文记录本项目已经确认的生产部署决策、Docker 拓扑、安全边界和上线步骤。目标环境是 Ubuntu 24.04 LTS、3 vCPU、2 GB RAM、30 GB 磁盘，服务器已经配置 Swap；当前规模为不超过 100 个测试用户、同时在线不超过 20 人。

## 1. 已确认范围

- Web 和 `bbw_protocol` 位于同一个应用镜像，由 FastAPI/Uvicorn 提供服务。
- 单机运行 Caddy、应用、PostgreSQL、Redis、RQ Worker 和调度器。
- Uvicorn 固定 1 个 worker；媒体归档和同步工作由单独 RQ Worker 执行。
- PostgreSQL 与 Redis 仅连接内部 Docker 网络，不向宿主机发布端口。
- Cloudflare DNS 开启代理，Caddy 只接受 Cloudflare 网段来源的业务请求。
- Cloudflare R2 使用私有 Bucket，禁止公开开发 URL；媒体不做额外应用层加密。
- 上游账号、密码和 Token 使用应用主密钥加密后保存；数据库不保存明文凭据。
- 网站采用邀请码；一个 Web 用户只绑定一个上游账号。
- 管理端位于 `/admin`，只允许一个超级管理员。日常 TOTP 可选，但查看明文密码或 Token 必须重新验证管理员密码和 TOTP，解锁有效 15 分钟。
- 普通聊天、媒体和闪图的服务器端保存期均为 180 天；上游原始响应脱敏并加密保存 7 天；管理员审计日志保存 180 天。
- 用户自助注销、停用和删除语义本轮不实现，不能把普通退出描述为数据删除。

## 2. 运行拓扑

```text
浏览器
  |
  | HTTPS / WebSocket
  v
Cloudflare Proxy
  |
  v
Caddy（只发布宿主机 80/443）
  |
  v
FastAPI + Uvicorn（1 worker，端口仅容器内 8000）
  |                    |
  |                    +--> 上游业务接口 / 腾讯 TIM / Cloudflare R2
  |
  +--> PostgreSQL：用户、凭据密文、会话、聊天、关系、审计、归档元数据
  +--> Redis：Session 辅助状态、限流、幂等、RQ 队列、调度锁

RQ Worker --> 上游媒体下载、文件验证、EXIF 清理、R2 归档、同步补漏
Transcode Worker --> 动态视频 H.264/AAC 兼容转换（独立单并发队列）
Scheduler --> 每分钟派发同步/清理任务
```

Docker 网络划分：

- `public`：只有 Caddy 和 App，用于反向代理以及 App 出站访问。
- `database`：内部数据库网络，仅 PostgreSQL、App、普通 Worker 和一次性迁移容器接入；转码 Worker 不接入。
- `queue`：内部队列网络，仅 Redis、App、Worker 和 Scheduler 接入；Scheduler 在网络层也无法访问 PostgreSQL。
- `egress`：仅普通 Worker 与转码 Worker 使用的出站网络，不包含数据库公网入口；转码 Worker 只挂载 R2 凭据，不挂载数据库、应用主密钥或腾讯 IM Secret。

## 3. 仓库中的部署文件

| 文件 | 作用 |
|---|---|
| `Dockerfile` | Python 3.12 非 root 应用镜像，只复制运行时所需文件 |
| `.dockerignore` | 默认全部排除，再显式允许源码；APK、Git、Session、Secret 永不进入上下文 |
| `compose.yaml` | 完整单机服务拓扑、健康检查、资源限制和 Docker secrets |
| `Caddyfile` | HTTPS、Cloudflare 来源限制、可信客户端 IP 和安全响应头 |
| `.env.example` | 非敏感环境变量模板 |
| `requirements.txt` | FastAPI、数据库、Redis/RQ、R2、加密和媒体依赖 |
| `docker/entrypoint.sh` | 从数据库密码 Secret 构造 SQLAlchemy URL |
| `docker/redis.conf` | AOF、RDB、96 MB 上限和 `noeviction` 策略 |
| `docker/init-secrets.sh` | 生成本地密钥并录入 R2/Turnstile Secret |
| `docker/prepare-host.sh` | 预创建非 root Caddy 的证书和配置持久目录 |
| `docker/healthcheck.py` | App 容器健康检查 |

镜像不会包含 `beibeiwu.apk`、`xbly.apk`、`.git`、本地 `session.json`、`sessions/`、`bbw_web/data/` 或 `docker/secrets/`。

## 4. Cloudflare 配置

### 4.1 DNS 和 TLS

1. 为 `APP_DOMAIN` 创建 A 记录；服务器有可用 IPv6 时再创建 AAAA 记录。
2. DNS 记录开启 Cloudflare 代理。
3. Cloudflare 的 SSL/TLS 模式设置为 `Full (strict)`，不能使用 `Flexible`。
4. 开启 WebSocket 支持；不要缓存 `/api/*` 和管理端页面。
5. Caddy 自动申请和续期源站公开证书，80/443 必须能够从 Cloudflare 节点到达。

`Caddyfile` 同时维护 Cloudflare 官方 IPv4/IPv6 网段。Cloudflare 公布网段变更时，必须同步更新 Caddyfile 和云厂商安全组。推荐在云厂商防火墙中：

- TCP 80、TCP/UDP 443 只允许 Cloudflare 网段。
- SSH 只允许管理员固定 IP，禁止向全网开放。
- 8765、8000、5432、6379 不得加入安全组公网入站规则。
- 出站至少允许上游 HTTPS；使用 RoomKit 功能时还需允许其 TCP 8080，最好按目标地址收敛。

Caddy 会覆盖传给应用的 `CF-Connecting-IP`、`X-Real-IP` 和 `X-Forwarded-For`，应用不能直接信任浏览器伪造的同名请求头。

### 4.2 R2

创建一个专用私有 Bucket，并创建只对该 Bucket 具有对象读写权限的 S3 API Token：

- 禁止开启 R2 Public Development URL。
- 不给 Token 账户管理、其他 Bucket 或 Cloudflare 全局权限。
- Access Key ID 和 Secret Access Key 只写入 Docker secret。
- 用户归档媒体由数据库保存随机对象键、类型、大小、哈希和归属；动态兼容视频使用不可逆 URL 摘要生成的确定性私有缓存键。
- 用户归档下载签名当前为 300 秒；最长 10 分钟的动态兼容视频签名为 900 秒。
- 预签名 URL 的路径会被已授权浏览器看到，这是直接访问私有 R2 的必要例外；用户归档键保持随机、兼容缓存键只含摘要，且签名 URL 不得进入访问日志、审计详情或 Referrer。
- 应用会按最近访问时间清理 `compat/moments/h264-main-1280-v1/`，默认保留 30 天、最多 2 GiB/2000 个对象；仍建议在 R2 为该前缀配置 30 天生命周期规则，作为 Redis 索引丢失时的兜底。

如果前端需要通过 `fetch` 跨域读取签名 URL，再为 Bucket 配置只允许正式域名的 `GET`、`HEAD` CORS；不能使用 `*` 同时开放写入。

## 5. 首次部署

以下命令在 Ubuntu 24.04 服务器的项目目录执行。Docker Engine 和 Compose Plugin 应使用 Docker 官方仓库安装的稳定版本。

### 5.1 准备非敏感配置

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

至少修改：

- `APP_DOMAIN`
- `ACME_EMAIL`
- `R2_ACCOUNT_ID`
- `R2_BUCKET`
- `TURNSTILE_SITE_KEY`，暂未配置时可留空
- `BBW_DATA_ROOT`，默认 `/var/lib/bbw`

`.env` 不能保存数据库密码、主密钥、管理员密码、R2 Secret 或 Turnstile Secret。
生产配置会主动拒绝把凭据主密钥、HMAC 密钥、腾讯 IM Secret 或 RoomKit Token 直接放进容器环境变量，必须使用对应的 `*_FILE` Docker Secret；这样这些值不会出现在 `docker inspect` 的环境块中。

### 5.2 生成和录入 Secret

```bash
chmod 700 docker/init-secrets.sh docker/prepare-host.sh
bash docker/init-secrets.sh
```

脚本会生成：

- PostgreSQL 随机密码。
- 32 字节凭据加密主密钥。
- 空的版本化凭据旧密钥 keyring；后续轮换会把旧主密钥写入该文件。
- 独立手机号 HMAC 密钥和 Session HMAC 密钥。
- 随机初始管理员密码。
- 交互录入腾讯 IM Secret Key 和 RoomKit Business Token。
- 交互录入 R2 Access Key ID、R2 Secret Access Key。
- 可选录入 Turnstile Secret Key。

Secret 目录权限应为 `0700`，file-backed Compose Secret 文件应为只读 `0444`：

```bash
find docker/secrets -maxdepth 1 -type f ! -name README.md ! -name .gitignore -exec chmod 444 {} \;
```

主密钥、版本化 keyring、两个 HMAC 密钥丢失会分别导致凭据无法解密或索引/会话验证失效。至少在独立安全介质保存离线副本，但不能把它们和数据库备份放在同一公开位置。

为非 root Caddy 创建可写且不公开的证书目录：

```bash
sudo env BBW_DATA_ROOT=/var/lib/bbw bash docker/prepare-host.sh
```

这里的路径必须与 `.env` 中的 `BBW_DATA_ROOT` 一致。不要让 Docker 自动创建 root 所有的空目录，否则 Caddy 无法保存或续期证书。

### 5.3 配置检查和启动

```bash
docker compose config --quiet
docker compose build --pull app
docker compose up -d
docker compose ps
```

`app`、`migrate`、`worker`、`transcode-worker` 和 `scheduler` 共用同一个
`APP_IMAGE` 与 Dockerfile。禁止在本项目中执行不带服务名的
`docker compose build --pull`：Compose 会并行构建多个目标，多个 BuildKit
导出器同时写入同一镜像标签时会发生竞态，常见报错为
`image "docker.io/library/bbw-app:<tag>": already exists`。使用
`docker compose build --pull app` 只导出一次镜像，随后 `docker compose up -d`
会让其余服务复用该标签。

`migrate` 是一次性容器，先执行 `alembic upgrade head`；迁移成功后 App 和 Worker 才启动。Scheduler 只等待 Redis 并负责入队，即使更早启动，Worker 也会在迁移完成前保持未运行。项目规则禁止代理自行执行编译/构建，因此上述构建命令需要由服务器管理员实际执行。

查看启动日志：

```bash
docker compose logs --tail 100 postgres redis migrate app worker transcode-worker scheduler caddy
```

健康验证：

```bash
curl -fsS "https://你的域名/api/health"
docker compose exec app python /app/docker/healthcheck.py
docker compose exec postgres sh -c 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose exec redis redis-cli ping
```

预期 `migrate` 状态为成功退出，其他服务为 Running/Healthy。PostgreSQL、Redis 和 App 没有宿主机端口，不能用公网地址直接连接。

### 5.4 管理员和邀请码

数据库还没有管理员时，应用使用：

- 用户名：`.env` 中的 `ADMIN_INITIAL_USERNAME`。
- 密码：`docker/secrets/admin_initial_password`。

通过 `https://你的域名/admin` 登录。首次登录后立即修改密码。管理员密码使用 Argon2id 哈希，不能把哈希改为可逆加密。邀请码由管理端创建、禁用、设置有效期和使用次数；数据库只保存邀请码哈希，审计日志记录创建、禁用和使用结果。

初始密码修改成功后，原密码已经失效，但仍建议把 bootstrap Secret 更换为新的随机值，避免未来数据库被误清空时重新启用曾经暴露过的初始密码。这个值只用于“数据库中不存在管理员”的灾难性空库场景，不能写成当前管理员密码：

```bash
sudo sh -c 'umask 077; openssl rand -base64 32 | tr -d "\n" > docker/secrets/admin_initial_password; chmod 0444 docker/secrets/admin_initial_password'
```

管理端当前提供：

- 管理员登录、退出、密码修改和 30 分钟空闲/8 小时绝对 Session。
- TOTP 首次绑定、验证和重新绑定。
- 密码加 TOTP 解锁明文凭据 15 分钟；每次查看仍需填写理由并写审计。
- 邀请码创建、筛选和禁用；原码只在创建成功时显示一次。
- 用户启用/停用；停用会撤销该用户全部 Web Session 并暂停后台同步。
- 用户资料、会话、聊天、媒体、关系和活动查看；7 天内原始响应内容与明文凭据共用密码加 TOTP 的敏感解锁保护。
- 私有 R2 用户归档媒体最长 5 分钟临时访问地址；动态兼容视频最长 15 分钟。
- 系统概览和不可由管理端删除的审计日志。

管理员 Session 绑定登录时的可信客户端 IP；网络出口变化后需要重新登录。管理端页面不使用 `localStorage`、`sessionStorage`、IndexedDB 或 Cache Storage 保存管理数据，退出、401、页面隐藏和敏感解锁到期时会清空相关 DOM。手动锁定或页面隐藏还会尽力调用服务端锁定接口，立即撤销数据库与 Redis 中的敏感解锁；如果浏览器在卸载阶段无法送达请求，本地仍会先清空，服务端授权最迟按 15 分钟上限失效。

## 6. 资源预算

2 GB 内存是本方案的最低测试配置，Compose 已设置以下硬上限：

| 服务 | 内存上限 | CPU 上限 | 说明 |
|---|---:|---:|---|
| PostgreSQL | 448 MB | 0.70 | 40 连接，96 MB shared buffers，关闭 JIT |
| Redis | 128 MB | 0.15 | 数据上限 96 MB，`noeviction` |
| App | 384 MB | 0.80 | Uvicorn 1 worker、64 并发上限 |
| RQ Worker | 320 MB | 0.60 | 媒体使用临时文件和流式处理，禁止整段视频入内存 |
| Scheduler | 128 MB | 0.10 | 单实例 Redis 锁 |
| Caddy | 96 MB | 0.25 | 非 root，内部监听 8080/8443 |
| Migrate（一次性） | 256 MB | 0.30 | 只在升级阶段运行，不计入稳态合计 |

六个常驻服务的内存上限合计 1504 MiB、CPU 上限合计 2.60；首次迁移阶段 PostgreSQL、Redis 和 Migrate 的上限合计 832 MiB、1.15 CPU。更新时旧常驻容器可能与 Migrate 短暂重叠，最坏上限为 1760 MiB、2.90 CPU，仍给宿主系统保留有限余量。Swap 只负责缓冲瞬时压力，不代表能够提高稳定并发。持续出现 Swap、OOM 或队列堆积时，应先升级到至少 4 GB RAM，而不是继续增加 worker。

检查资源：

```bash
docker stats --no-stream
free -h
df -h
docker system df
```

30 GB 本地磁盘只保存容器层、PostgreSQL、Redis 和有限日志；聊天媒体必须及时归档 R2。Docker 日志已限制为每容器 3 个、每个 10 MB。

## 7. 数据与安全策略

### 7.1 登录和 Session

- Web Session 空闲 7 天、绝对最长 30 天。
- 管理员 Session 空闲 30 分钟、绝对最长 8 小时。
- 同一管理员最多保留 3 个有效管理 Session；新登录会撤销更旧的会话。
- 已撤销或超过绝对期限的 Session 数据库记录保留 7 天排障宽限后自动清理；Cookie 和 Redis 热状态会更早失效。
- Cookie 使用 `__Host-` 前缀、`Secure`、`HttpOnly` 和严格 SameSite。
- 登录成功、权限变化和敏感操作后轮换 Session ID。
- 登录失败按账号和可信客户端 IP 在 Redis 限流：15 分钟最多 5 次。
- Turnstile 采用异常触发模式，正常登录不固定弹出；连续失败或异常频率后才要求验证。

### 7.2 凭据

- 上游账号、密码和 Token 使用 AES-256-GCM 字段级加密。
- 主密钥从 `/run/secrets/app_master_key` 读取，密钥版本由 `BBW_CREDENTIAL_KEY_VERSION` 指定。
- 手机号索引和 Session 哈希分别使用独立 HMAC 密钥，不能复用加密主密钥。
- 日志、错误响应、审计详情和管理列表中默认不出现明文密码或 Token。
- 管理员查看明文凭据时必须已绑定 TOTP，并重新验证密码和 TOTP；解锁窗口 15 分钟。审计只记录查看对象、字段、原因、IP 和时间，不记录明文。
- TOTP 绑定信息只在 Redis 中以应用主密钥加密暂存 10 分钟；重新绑定确认前旧 TOTP 继续有效，开始或确认重绑都会撤销既有敏感解锁。管理员主动取消、退出、页面隐藏或绑定 Session 失效时会尽力立即删除暂存信息，最迟仍由 10 分钟 TTL 清理。

`credential_keyring` 通过只读 Docker Secret 同时挂载给 App 和 Worker，初始内容为 `{}`，配置加载时会拒绝非规范版本号、重复版本以及“活动主密钥与 keyring 同版本但内容不同”的危险状态。

当前暂不提供在线或自动主密钥轮换脚本：主密钥文件、keyring 和 `.env` 活动版本分属多个文件，在没有单一原子 keyset、独占锁、断电恢复和批量重加密工具前，自动修改可能让旧密文永久不可读。不要直接覆盖 `app_master_key` 或手工递增版本。后续实现必须采用单文件版本化 keyset 或等价的事务化密钥管理，并先在离线副本完成断电、回滚和旧密文解密演练；在此之前，疑似密钥泄露应视为停机维护事件，由人工同时保全数据库与全部 Secret 快照。

### 7.3 聊天同步

由于没有腾讯 IM 控制台回调权限，本地归档采用双路径：

1. 浏览器发送、接收、加载历史或收到撤回/状态变化时，非阻断上报 `/api/archive/messages`。
2. 后台对账：活跃用户每 5 分钟，非活跃用户每 60 分钟；登录和打开消息页立即同步。

消息使用上游 ID、消息键和幂等键去重，`owner_user_id` 必须从服务端 Session 推导，禁止信任浏览器提交的 owner。浏览器上报不是可信事实源，后台同步负责补漏；上游接口本身没有返回的数据无法保证恢复。

### 7.4 媒体

- 收到消息后异步下载媒体，校验实际文件头、MIME、扩展名和长度，再上传私有 R2。
- 图片去除 EXIF/GPS；拒绝 SVG、脚本和伪装文件。
- 当前 2 GB 服务器不运行 ClamAV。
- 单用户总额度 100 MB，系统归档总额度 8 GiB。
- 图片 10 MiB、音频 10 MiB、视频 50 MiB、普通附件 20 MiB。
- 闪图对接收用户保持一次查看语义，但服务器端和普通媒体一样保存 180 天。

### 7.5 日志与保留期

- Caddy 默认不启用完整访问日志，避免 URL 查询、Cookie 或敏感搜索条件进入通用日志。
- Uvicorn 访问日志关闭，错误日志输出到容器标准输出并轮转。
- PostgreSQL 只记录超过 2 秒的慢 SQL，不开启全语句日志。
- 管理敏感操作写入不可由管理端删除的审计表，保存 180 天。
- 规范化业务数据保存 180 天；脱敏并加密的原始上游响应保存 7 天。
- 清理任务必须先标记数据库记录，再删除 R2 对象，失败进入可重试队列，避免数据库与对象状态静默分叉。

## 8. 日常运维

### 8.1 更新

```bash
git pull --ff-only
docker compose config --quiet
docker compose build --pull app
docker compose up -d
docker compose ps
docker compose logs --tail 100 migrate app worker transcode-worker scheduler
```

每次发布先在 `.env` 中把 `APP_IMAGE` 改为新的不可变版本标签，例如
`bbw-app:2026.07.17-4`，再只构建 `app`。不要为共用该标签的每个服务分别构建，
也不要使用无服务名的全量构建命令；否则可能在镜像导出阶段遇到上述并发冲突。
如果误执行全量构建并在最后报错，线上旧容器通常仍未受影响；确认新标签后，
重新执行 `docker compose build app`，成功后再运行 `docker compose up -d`。

数据库迁移必须向前兼容正在运行的旧代码。涉及删除列、重写大量数据或密钥轮换时，应拆为多次发布，不能在单次启动迁移中长时间锁表。

### 8.2 进程和队列

```bash
docker compose logs --since 30m app worker transcode-worker scheduler
docker compose exec redis redis-cli INFO memory
docker compose exec redis redis-cli INFO persistence
docker compose exec redis redis-cli --scan --pattern 'rq:*' | head
```

Redis 采用 `noeviction`。达到 96 MB 后会明确拒绝写入，而不是自动驱逐 Session 或 RQ Job；此时应排查失败任务、过长结果和异常限流键，并扩容，不能改为随机淘汰。

### 8.3 数据库手工备份基线

自动备份和异地恢复方案按当前决策留到后续实现。但在保存真实用户数据前，至少应能生成并验证一份手工数据库备份：

```bash
mkdir -p backups
chmod 700 backups
docker compose exec -T postgres sh -c 'PGPASSWORD="$(cat /run/secrets/postgres_password)" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner' > "backups/bbw-$(date -u +%Y%m%dT%H%M%SZ).dump"
chmod 600 backups/*.dump
```

备份集合必须同时覆盖：

1. PostgreSQL 数据和 R2 对象元数据。
2. R2 中的实际私有对象或其独立版本副本。
3. 全部 Docker Secret，特别是当前主密钥、版本化旧密钥 keyring、两个 HMAC 密钥、协议密钥和 R2 凭据；必须单独加密保存。
4. `.env` 中的非敏感部署参数和使用中的镜像版本。

只有实际在隔离环境完成恢复并抽样读取消息、媒体、账号密文后，备份才可视为有效。R2 不是数据库备份，单独复制 R2 对象也无法恢复对象归属和聊天关系。

## 9. 上线检查表

- [ ] Cloudflare DNS 已代理，SSL/TLS 为 Full (strict)。
- [ ] 云安全组只向 Cloudflare 开放 80/443，SSH 只向管理员 IP 开放。
- [ ] R2 Bucket 为私有，Token 权限只限目标 Bucket。
- [ ] R2 已为 `compat/moments/h264-main-1280-v1/` 配置 30 天生命周期兜底规则。
- [ ] `.env` 不含任何密码或 Secret。
- [ ] `docker/secrets` 目录权限为 0700、实际 Secret 文件为只读 0444，并已有独立离线副本。
- [ ] `docker compose config --quiet` 通过。
- [ ] `migrate` 成功退出，App/PostgreSQL/Redis 健康。
- [ ] 外网只能通过正式域名访问，8000/5432/6379 无公网入口。
- [ ] 管理员已修改初始密码并完成 TOTP 绑定测试。
- [ ] `/admin` 与 `/admin/` 都能打开管理端；跨站 POST 被拒绝，管理 Cookie 不可由 JavaScript 读取。
- [ ] 管理员查看聊天、媒体、关系、活动和原始响应均产生审计记录。
- [ ] 明文凭据在未绑定 TOTP、未解锁或解锁过期时均无法读取。
- [ ] 邀请码创建、禁用、次数和过期行为已验证。
- [ ] 普通用户无法读取其他用户聊天、媒体和关系数据。
- [ ] 登录限流、异常 Turnstile、Session 超时和退出清理已验证。
- [ ] 图片 EXIF 清理、文件类型拒绝、大小限制和 R2 私有下载已验证。
- [ ] HEVC 动态触发异步兼容转换，PC/手机能播放 H.264/AAC 版本，失败态只有“重试”且没有原视频打开/下载入口。
- [ ] 浏览器实时上报和后台消息补漏都能幂等入库。
- [ ] 日志中没有密码、Token、Cookie、聊天正文和 R2 Secret。
- [ ] `bbw_protocol/sign.py` 和 RoomKit 适配器中的内置业务密钥已迁移到 Secret、在上游轮换，并关闭生产环境本地 UserSig/管理员级回退。
- [ ] 已执行一次隔离恢复演练，或明确标记自动备份为上线阻塞项。

## 10. 当前需要后续完成的事项

- 自动化 PostgreSQL PITR、R2 版本副本和异地加密备份。
- 在腾讯 IM/RoomKit 控制台轮换历史上曾进入源码的旧密钥，并净化 Git 历史；运行时代码已经只从 Docker Secret 读取。
- 定期恢复演练及备份失败告警。
- Cloudflare 网段自动核对和防火墙规则更新。
- 用户导出、删除、停用或注销的产品语义和数据处理策略。
- 超过 100 用户或持续高并发后，将 PostgreSQL 与应用拆到不同节点，并重新评估连接池、对象流量和 RQ Worker 数量。

在这些事项完成前，本方案适合当前小规模受控测试；不能把免费额度、单机磁盘或 Swap 当作生产 SLA。

## 11. 协议侧 Secret 迁移说明

生产容器不会再从源码读取腾讯 IM 或 RoomKit 密钥，启动前必须准备以下 Docker Secret：

- `docker/secrets/txim_secret_key`
- `docker/secrets/roomkit_business_token`

`docker/init-secrets.sh` 会交互式读取这两项，并分别映射为 `BBW_TXIM_SECRET_KEY_FILE`、`BBW_ROOMKIT_BUSINESS_TOKEN_FILE`。非交互执行时，先通过进程环境提供 `TXIM_SECRET_KEY`、`ROOMKIT_BUSINESS_TOKEN`；不要把值写入 `.env`。RoomKit/Rong 的公开 App Key 不是认证 Secret，仍可作为客户端集成标识保留在代码中。

旧源码、分析文档、工具文件和 Git 历史曾包含协议密钥，因此旧值应视为已经暴露。当前没有相应上游控制台权限时，可以先完成 Secret 化以避免继续进入镜像和运行配置，但“轮换旧值、清理历史、验证新值”仍是正式开放前的阻塞项。`tools/bbw_client.py`、早期静态分析文档以及本地旧 `__pycache__` 不进入镜像；对外分发源码包前仍应另行清理。

媒体归档主机由 `MEDIA_ALLOWED_HOSTS` 配置，默认只允许官方 OSS 和腾讯云后缀。上线后应根据实际抓到的媒体域名继续收紧，禁止配置为任意主机通配符，以免破坏 SSRF 防护。

## 12. R2 浏览器访问策略

Bucket 必须保持私有。用户归档对象只签发最长 5 分钟的 GET 地址；动态兼容视频因最长可播放 10 分钟，签名最长 15 分钟。若管理端或用户端需要通过 `fetch`、音视频 Range 请求访问签名地址，应在 R2 配置最小 CORS：

- Allowed Origins：只填写正式站点 `https://你的域名`。
- Allowed Methods：`GET`、`HEAD`。
- Allowed Headers：`Range`。
- Expose Headers：`ETag`、`Content-Length`、`Content-Range`、`Accept-Ranges`。
- Max Age：不超过 300 秒。

不要允许任意 Origin，不要开放 `PUT`/`DELETE` 给浏览器，也不要启用 R2.dev 公共地址。更换 Bucket 前必须迁移旧对象和数据库元数据；清理任务会安全拒绝跨 Bucket 删除，避免误删其他存储空间。

## 13. 已明确暂缓或受外部条件限制的事项

- 自动 PostgreSQL PITR、R2 版本副本、异地加密备份和定期恢复演练：按当前决定暂缓；本文只保留手工备份基线。
- 用户自助注销/删除：本阶段不实现，退出仅撤销会话，历史业务数据继续按既定保留策略保存。
- 腾讯 IM 控制台回调：当前无控制台权限，采用浏览器实时上报加 5/60 分钟主动对账，无法保证恢复上游接口从未返回的数据。
- 协议密钥轮换和 Git 历史净化：运行时已经 Secret 化，旧值仍必须在取得上游权限后轮换；对外分发源码前还要清理历史分析产物。
- 本地 UserSig 与腾讯 REST 备用通道：当前为无控制台条件下保留可用性的兼容路径，密钥只在服务端 Secret 中；待服务端签名接口和回调稳定后应通过配置关闭，并复测消息发送、撤回和降级提示。
- 当前环境没有真实 PostgreSQL、Redis、R2、Cloudflare 与上游账号，已完成静态检查和本地合同测试，但正式开放前仍必须在隔离服务器做端到端联调与恢复演练。
- 管理端列表仍使用有上限的 OFFSET 分页，当前限制每页最多 50 条、页码最多 1000；在约 100 个用户的规模内可接受，数据显著增长后应改为游标分页。
- 管理概览使用数据库精确计数，当前低频管理访问可接受；数据量增长后应增加短时缓存或预聚合，不能通过提高管理 API 限流来硬扛。
- 管理员停用用户可以撤销本站 Web Session 和停止同步，但在没有腾讯控制台服务端能力时，不能立即吊销浏览器此前获得的上游 TIM 会话；客户端会在本站 API 失效后清理本地状态。
- 管理员普通登录按已确认方案不强制 TOTP；明文凭据和原始响应内容必须完成密码加 TOTP 的 15 分钟敏感解锁。若未来风险模型提高，可再把 TOTP 提升为每次管理登录必需。
- 当前没有在线 TOTP 恢复码或“仅凭密码重置”能力，避免管理员密码单点泄露后绕过第二因素。首次绑定时必须把手动输入密钥保存到独立安全介质；如果当前验证器和备份密钥同时丢失，只能在停机维护窗口进行离线受控恢复，自动化恢复工具留待后续实现。
- 管理员密码同样没有网页找回入口；遗失后需要停机、备份数据库并执行单独审计的离线重置流程。不要把初始管理员 Secret 当作现有账号的找回密码，它只在空数据库 bootstrap 时生效。
- 凭据主密钥自动轮换和批量重加密暂未实现；当前版本化 keyring 只负责安全读取已有旧版本密文，禁止直接覆盖主密钥或复用版本号。
- Python 基础镜像、PostgreSQL/Redis/Caddy 标签和 Python 依赖目前使用受限版本范围而非已验证 digest/哈希锁。首次真实联调通过后，应记录 `docker image inspect` 得到的镜像 digest，并把依赖解析结果固化到受审查的锁文件；在此之前不能把一次成功构建视为可永久复现。
