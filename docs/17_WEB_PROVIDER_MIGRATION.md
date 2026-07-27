# 已退役方案：Web 去 APK 与外部依赖渐进迁移

> 状态：历史设计，当前不启用。项目已撤销 Web 本地权威产品迁移：登录、资料、关系、在线/附近、粉丝、访客、匹配和动态继续调用原 APK 接口；私聊以 TIM SDK/TIM REST 为权威，闪图调用原 APK 接口。PostgreSQL、Redis 与 R2 仅承担 Session、缓存、归档、权限镜像、历史兼容资产和 Agent 数据。本文后续内容保留用于追溯此前设计与已存在的数据模型，不代表当前运行方式或部署目标。

## 1. 结论

当前 Web 已经不是“Banghua 或 TIM 一旦死亡，整个平台必然瘫痪”，但也还没有达到可以从部署中删除 `bbw_protocol`、覆盖 APK 全部业务的阶段。

在 PostgreSQL、Redis、Web App 正常，且账号已完成迁移的前提下：

- Banghua 不可用时，已有本地密码凭据的账号仍可使用账号密码登录 Web。
- TIM 不可用时，两个已迁移 Web 账号仍可互发、读取和标记已读文本消息。
- Banghua 与 TIM 同时不可用时，本地登录、Web-Web 私聊文本与富媒体、完整资料编辑（含头像）、文字/图片/视频动态、关系、城市发现、文本匹配以及 R2 私有媒体仍可工作。
- 未迁移账号、短信登录、原注册流程、Web 与未迁移 APK 用户通信、未完成导入的历史数据、语音匹配、房间、群聊、资产/礼物等仍依赖对应外部服务或尚未迁移。

必须区分三个概念：

| 对象 | 当前状态 |
|---|---|
| APK 客户端 | 可以按账号和领域逐步被 Web 替代；尚不能无条件立即退役 |
| `bbw_protocol` 兼容代码 | `provider-first` 和剩余 APK 兼容领域仍需要它；显式切换 `local-only` 后，本地登录、Session 恢复和 Web-Web 文本链路可由 `WebNativeProvider` 启动，不再导入协议核 |
| Banghua、TIM 等外部服务 | 已迁移账号的本地认证、Web-Web 私聊、资料/关系、动态、城市发现、文本匹配和本地媒体不要求它们在线；历史导入及 APK 兼容镜像仍会访问它们 |

因此，“外部服务死亡后主要 Web-local 能力可继续运行”已经实现；“彻底删除协议核和所有 APK 兼容代码”仍取决于历史迁移 Marker、兼容 Outbox 与 TIM mirror delivery 清空、账号生命周期方案及未迁移产品能力的下线或替代。

## 2. 已确认的迁移决策

本项目按以下方案继续，不再保留待确认分支：

1. 身份采用方案 1A：上游密码登录成功后机会式建立 Web 本地密码摘要，并允许从存量加密密码执行受控批量回填。
2. Banghua 死亡后暂时只支持账号密码登录；不实现本地短信登录、本地注册、密码找回或新账号开户。
3. 消息采用方案 3A：PostgreSQL canonical 消息为 Web-Web 权威，TIM 仅作为兼容 APK 的非必需 Outbox 镜像。本地事务成功后，即使 TIM 发送失败也不回滚 Web 消息。
4. Banghua 在线时继续使用原注册和认证，保留 APK 账号、上游 UID 与协议兼容，不创建一套与历史账号割裂的新身份。

本地密码回退只有一个安全入口：上游适配器必须明确返回 `UPSTREAM_AUTH_UNAVAILABLE`。上游返回密码错误、封禁、业务拒绝、401、403、429，或出现未分类适配器异常时，Web 都不会尝试本地密码，以防旧密码绕过上游状态。

## 3. 当前架构

```text
Browser
  |
  v
Web BFF / 领域服务
  |-- PostgreSQL + Redis
  |     |-- User / ExternalAccount / UserCredential / WebSession
  |     |-- canonical Profile / Relationship / Moments / Discovery
  |     |-- canonical ChatThread / ChatMessage / Receipt / Delivery
  |     `-- R2 私有媒体对象、附件与闪照领取
  |
  |-- RuntimeProvider
  |     `-- Legacy Banghua Provider -> Banghua API（剩余兼容领域）
  |
  `-- Compatibility Transport / Outbox
        |-- 本地事务（权威、先提交）
        |-- TIM mirror（非必需、RQ Worker 异步执行）
        `-- compatibility.*（Banghua 在线期的尽力镜像）
```

已落地的边界包括：

- Web Session、持久化服务和后台历史同步不再直接依赖具体协议对象，生产运行时通过 `RuntimeProvider` 和 Transport 组装。
- PostgreSQL、Redis 和 Web 自身正常时，Banghua/TIM 故障不会直接把 readiness 判定为失败；外部依赖状态通过管理端状态注册表单独展示。
- `auth_source=web-local` 的 Session 从持久化状态构造无网络的 `WebNativeProvider` runtime，不解密旧 Banghua token/login，也不导入 `bbw_protocol`。调用尚未迁移的领域接口会立即返回外部能力不可用。
- 默认 `provider-first` 仍装配 Legacy Provider，以保持原注册、认证和 APK 兼容；确认 Banghua 永久退役后可显式设置 `BBW_UPSTREAM_AUTH_MODE=local-only`，让进程启动和密码登录完全跳过协议核。该模式不提供短信、注册、找回密码或未迁移领域。

## 4. 已完成能力

### 4.1 本地密码认证

- 数据库迁移 `20260725_0009` 新增 `user_credentials`，与保存上游绑定和可逆凭据的 `external_accounts` 分离。
- 普通用户密码使用 Argon2id 摘要；未知账号和未迁移账号也执行 dummy verify，降低账号枚举时序差异。
- 本地认证对未知账号、密码错误、未迁移凭据、停用账号和停用凭据统一返回 `LOCAL_AUTH_REJECTED`，不向匿名调用者暴露账号是否存在、迁移或停用状态；仅内部服务保留具体失败原因。
- 验证前会先解析摘要参数并拒绝超出受控范围的 Argon2id 摘要；异常高内存或计算次数不会进入底层昂贵验证，切流检查也使用同一套参数边界。
- 只有可信的上游密码认证成功，才会机会式创建或更新本地摘要。短信登录即使请求中夹带密码，也不会创建、覆盖本地摘要或上游加密密码。
- `verified_at` 只表示上游成功认证或受控回填；本地登录只更新 `last_authenticated_at`。
- Banghua 明确不可用时，本地认证成功会签发正常 `WebSession`、恢复 Web 用户运行态并标记依赖处于降级状态。
- 数据库迁移 `20260725_0011` 将 `auth_source` 持久化到 Web Session；进程重启后仍能判定应恢复 Web-native runtime，而不是重新构造 Legacy runtime。
- `provider-first` 的单次上游密码认证默认限制为 5 秒；`local-only` 是运维显式切换，不会先探测 Banghua，也不会因上游黑洞拖慢登录。
- 本地 Argon2 认证使用每个 Web 进程共享的非排队并发闸门，`BBW_LOCAL_PASSWORD_AUTH_CONCURRENCY` 默认 2、允许 1 到 8；槽位占满时返回可重试的 `LOCAL_AUTH_BUSY`，不会继续分配 Argon2 内存。密码错误、未迁移凭据和禁用账号都会计入登录失败限流。
- 原有 `external_accounts.password_encrypted` 不会被机会式迁移或回填删除，APK 和上游恢复后的兼容路径保持不变。

当前本地登录标识仍是历史账号绑定的手机号，不是新建 Web 用户名。离线认证只能依据最后一次可信登记的密码和本地用户状态；如果用户随后只在 APK/上游修改密码，或上游在死亡前封禁账号但状态尚未同步，本地数据无法自动知道这一变化。正式切断上游前必须安排最终状态同步、密码确认窗口，并让 Web 接管后续密码和封禁权威。

### 4.2 Web 本地权威文本消息

- 数据库迁移 `20260725_0010` 新增 `chat_threads`、`chat_members`、`chat_messages`、`message_receipts` 和 `message_deliveries`。
- 双方都必须是 active `User`，具有有效的 `ExternalAccount`、上游 UID 和未禁用 `UserCredential`，才进入 Web 本地通道；否则继续尝试原 TIM 兼容路径。
- 单个 PostgreSQL 事务同时写入 canonical 消息、双方会话成员、双方 `Conversation/Message` 兼容投影、本地 delivered receipt，以及非必需 TIM delivery。
- 文本消息上限为 2000 字符。`client_message_id` 对发送人唯一，并严格绑定收件人、正文和引用内容；相同键换内容会被拒绝。
- 任一方向存在有效黑名单即拒绝发送。依赖上游黑名单快照降级时，只有双向完整快照可信才放行；缺少可信快照时 fail-closed。
- “已有会话”私聊授权只接受服务端可信来源：Banghua/TIM 尚在线时，由 Web 调用可信 `/api/im/conversations` 并把响应同步为 `provider=web-policy`、`kind=message_peer` 的服务端授权记录。浏览器上报或归档的 TIM 会话只用于兼容展示和历史归档，不得据此反推或授予私聊权限。
- 只有 TIM 服务端会话游标明确返回完整结果时，Web 才会在同一事务写入 `message-peer-snapshot` 完成标记、授权集合数量和摘要。截断分页、失败响应以及浏览器归档都不能生成该标记；永久切流检查要求每个 active 账号的标记与服务端授权集合一致。
- Web-Web 本地投递成功后立即向浏览器返回成功，不等待 TIM。TIM 失败只更新 Outbox 为 retry/failed，不改变本地 delivered 状态。
- 普通 `worker` 消费首次入队的 TIM mirror；`scheduler` 每分钟把到期记录派到 `sync-worker`，支持锁超时恢复、指数退避和最多 8 次尝试。达到上限的 failed 记录不会无限自动重试，需要受控重放。
- TIM `CloudCustomData` 携带 canonical ID、client ID 和引用信息；历史回流和归档读取据此合并 `tim` 与 `web-local` 投影，避免同一条消息重复显示。
- 本地已读先提交；TIM 已读同步失败不会令 Web 本地已读回滚。
- 定时 retention 会按 canonical 的最长参与方保留期删除过期 `chat_messages`，并级联清理 receipt 与 TIM delivery；归档双投影分页游标以本页实际返回的最早消息继续，避免去重后跳过历史。

### 4.3 Web-local 资料与社交关系

- 资料核心字段、隐私设置、关注、好友申请与确认、好友、黑名单和访问记录均以 PostgreSQL 为本地权威；Web 请求先提交本地事务。
- 昵称、个性签名、城市和性别可直接在 Web 编辑。新头像必须先通过 Web-native 上传意图写入私有 R2，再以本人 `avatar_asset_id` 绑定；浏览器不能提交 Blob、Base64、外部 URL 或任意站内 URL。资料行和当前头像引用在同一 PostgreSQL 事务内更新，替换后旧内容路径立即失效。
- 普通资料字段仍可尽力镜像 Banghua；Web 私有头像没有可安全映射的上游上传语义，API 会直接把对应兼容 Outbox 写成 `cancelled`，并记录稳定原因 `profile_avatar_upload_not_mappable`，不会向客户端虚报 `pending`。私有站内路径绝不发送给 Banghua，本地头像也不会因兼容失败回滚。因此迁移期保留的是账号和旧资料兼容，不承诺新 Web 私有头像会出现在旧 APK。
- Web-Web 账号之间的关系判断、资料读取和修改不需要 Banghua 在线。Banghua 在线期产生的 `compatibility.profile.*`、`compatibility.social.*` 只用于尽力保持 APK 可见状态，不得反向回滚已经成功的本地写入。
- 本地 tombstone 和来源优先级防止旧 Banghua 快照把 Web 已删除或已更新的关系重新“复活”。历史快照是否完整不能通过“表里已有几行数据”推断，必须由 `migration-domain:social` 完成 Marker 证明。
- 社交域使用独立主动迁移器读取本人资料，以及关注、粉丝、好友、好友申请、我拉黑的人、拉黑我的人共七个固定来源。全部网络读取和结构校验结束后才开始本地资料与关系短事务；Web 请求路径不会等待或调用该迁移器。
- 关注、粉丝和好友申请必须分页读到空页、HTTP 2xx `false` 空结果或服务端明确的完成标志；好友与双向黑名单必须是可证明完整的单次响应。重复页、页数或记录数超限、未知响应结构、缺失 UID、好友申请方向或状态不明，都会失败关闭且不会生成完成 Marker。
- 迁移只更新 `provider=beibeiwu` 的 legacy 行；`provider=web-local` 的本地关系和 tombstone 永不覆盖。完整快照中已消失的 legacy active 行会转为 inactive，重复执行按固定身份摘要幂等收敛。
- 完成前会重新校验账号绑定、本地资料、全部 legacy 关系状态和记录摘要。Marker 固定绑定内部 User UUID、ExternalAccount、上游 UID、七个来源的分页水位、四个 scope 的精确覆盖和 `unresolved_records=0`；任一验证失败都只留下不完整/失败状态。

按经审核的内部 User UUID 可单账号执行；输出只有聚合页数、记录数和写入计数，不输出账号、上游 UID、token、内容或摘要：

```bash
docker compose exec app python -m bbw_web.social_native.legacy_migration \
  --owner-user-id "内部-User-UUID"
```

该命令只能在 Banghua 仍能返回可证明完整的七类数据时运行。若某个 APK 接口已经死亡、返回未知结构或无法证明分页结束，对应账号必须保持 `social_ready=false`，不能人工补写完成 Marker。

### 4.4 Web-local 动态与发现

- 动态发布、删除、可见范围、评论策略、评论、点赞、话题、举报、浏览计数，以及推荐、最新、附近、关注、招募令和个人动态读取均使用本地 canonical 表。
- Web-native 新动态支持纯文字、最多 9 张图片或 1 个视频；图片与视频不能混用。浏览器只提交有序 `media_asset_ids`，服务端先无锁预览媒体形态，创建或解析幂等动态后，再按“目标 Post → asset UUID 顺序 → 引用 UUID 顺序”执行带锁最终校验与绑定。最终校验覆盖本人、可用状态、当前 deployment、实际 R2 Bucket、真实媒体类型和“未绑定私聊附件”，并生成固定的 `/api/media/native/{asset_uuid}/content` canonical 路径。动态写入与全部媒体引用在同一事务内完成，任一步失败都会整体回滚；相同 `client_request_id + media_asset_ids` 可安全重放。
- 动态删除会在同一事务中释放当前媒体引用；旧路径随后返回不存在。动态媒体访问继续执行动态可见性策略，不能凭保存过的站内路径绕过仅好友或仅自己可见范围。客户端提交 `media`、`pictures`、`video`、`cover` 或其他 URL 字段会被明确拒绝。
- 文字动态仍可按原适配器尽力镜像；包含 Web 私有媒体的动态会在本地事务中直接把可选兼容 Outbox 写成 `cancelled`，稳定原因为 `post_media_not_mappable`，接口明确返回 `compatibility_sync=cancelled`，不会虚报待镜像。该状态不泄露鉴权路径且不影响 Web-local 发布成功。旧 APK 因而不保证显示迁移后新发的 Web 私有媒体动态。
- 附近动态和附近用户只使用城市级资料，不信任浏览器上传的精确坐标。文本匹配、匹配状态和私聊授权写入本地模型；语音匹配、漂流瓶等未列入本地承诺。
- 历史动态有独立的 180 天主动迁移器。它使用 `someonesluntannew` 读取帖子、使用 `getMainComment` 读取评论，网络读取结束后才开启短数据库事务；失败重跑从第一页开始，依靠 legacy binding 幂等去重，本地动态请求从不等待该迁移器。
- 只有帖子页和每帖评论页都明确读到结束、时间字段可严格解析、重复分页未出现、评论父子关系全部可解析、帖子/评论/嵌入话题均已写入后，才生成 `migration-domain:moments` Marker。任一条件失败只记录稳定错误码，不会伪造完成。

按经审核的内部 User UUID 可单账号执行；输出只有聚合计数，不输出账号、上游 UID、token 或内容：

```bash
docker compose exec app python -m bbw_web.moments_native.legacy_migration \
  --owner-user-id "内部-User-UUID"
```

迁移器只覆盖执行时点之前最近 180 天。正式停服前需要冻结或明确停止上游动态写入，再执行最后一轮并重新检查 Marker；否则停服前最后发生的上游变更仍可能落在 Marker 水位之后。

### 4.5 Web-native 私有媒体

- 图片、语音、视频、普通文件、闪照和撤回均有本地上传意图、私有对象、消息附件和领取状态；R2 是对象存储，PostgreSQL 保存权威元数据和授权关系。
- 数据库迁移 `20260725_0017` 新增 owner-bound `media_asset_references`。一个 Web-native asset 同时只能作为一个当前头像或动态 slot，也不能同时作为私聊附件；头像只接受图片，动态只接受最多 9 图或单视频。当前引用会把 asset 从“未使用上传”清理范围中排除，引用释放后再按清理宽限期回收对象和配额。
- 头像和动态返回的只是稳定同源鉴权路径，不返回 R2 对象 key 或长期预签名 URL。每次写入和读取都强制匹配当前 deployment、实际 R2 Bucket、private namespace 与 owner key 前缀；读取还会核对 `User.profile.avatar` 或 `SocialPost.media[slot]` 仍与引用路径一致，并重新检查资料账号有效、动态仍处于 published 状态及本地可见性策略。全部通过后才签发短时 R2 GET 重定向。引用替换或释放会立即使同源路径失效，但已经签发的 R2 GET URL 最长仍可在 300 秒有效期内读取，不能宣称对象字节瞬时撤销。
- 当前翻倍后的边界为：图片/闪照/语音文件 20 MiB、视频 100 MiB、普通文件 40 MiB、语音最长 60 秒。服务层、数据库检查约束和浏览器校验必须保持一致。
- 单文件上限不是累计容量。默认累计配额为每用户 100 MiB、全系统 8 GiB，Web-native 上传和历史媒体归档都会计入同一用户/系统配额；生产可在完成容量、增长率和清理恢复审计后通过 `BBW_USER_MEDIA_QUOTA_BYTES`、`BBW_SYSTEM_MEDIA_QUOTA_BYTES` 调整，不能只因为单个文件未超限就认为仍有容量。
- Web-Web 媒体消息先完成本地投递；TIM 可用时再做 APK 兼容发送。视频在旧 APK 不兼容时可降级为文件，闪照在兼容发送前必须确认本地 delivery 状态，撤回可通过 canonical 标识或受控历史查找恢复 TIM `MsgKey`。
- 本地媒体能力可在 Banghua/TIM 双死亡时继续，但前提是 PostgreSQL、R2、签名密钥及 Bucket CORS 正常。旧媒体原文件是否已经进入 R2 仍必须由 `migration-domain:media` Marker 证明。
- 历史媒体迁移必须在严格有效的 `migration-domain:social`、`migration-domain:moments` 和 `message-peer-snapshot` 之后执行。它归档当前头像、保留期内 legacy 动态的图片/视频/封面，以及每个可信私聊 peer 在账号聊天保留期内的双向 TIM roaming history；任何方向只有服务端明确返回 `Complete=1` 才算完整，重复游标、未知结构、方向或时间越界、页数/消息数超限都会失败关闭。
- 原上游 URL 保留在 `User.profile`、`SocialPost.media` 和 `Message.metadata.media_report` 中，迁移器只增加包含 `media_id`、`source_hash`、`slot` 的 `_local_media` v1 sidecar。Web 仅在当前 URL 的 SHA-256 仍匹配时投影本地私有内容路由，因此后续改头像或替换媒体会自动使旧引用失效。
- 每个对象下载都受 HTTPS host allowlist、SSRF 和翻倍后单文件限制约束；配额先预留，R2 上传后必须以 HEAD 复验大小与 SHA-256。R2 删除成功后才释放失败 reservation 的用户/系统配额；若删除失败则保留 uploading 行和配额供重试收敛。历史同步产生的旧 `media.archive` 待办只有在同消息同源对象与 sidecar 全部复验后才能审计性改为 `completed`，不得直接取消。

单账号与全 active 账号分别使用以下命令；输出只含聚合页数、消息数、对象数和稳定错误码，不输出账号、UID、URL、token、对象 key 或记录摘要：

```bash
docker compose exec app python -m bbw_web.media_native.legacy_migration \
  --owner-user-id "内部-User-UUID"
docker compose exec app python -m bbw_web.media_native.legacy_migration --all-active
```

`migration-domain:media` 只有在 PostgreSQL sidecar、`MediaObject` 元数据和全部 R2 对象再次验证、`unresolved_records=0` 且对应必需 `media.archive` 待办已完成后才写入；摘要可由当前绑定和对象元数据稳定重算。正式切流前仍应在真实 staging PostgreSQL 与真实私有 R2 Bucket 上执行小批量迁移，验证下载 allowlist、配额并发、HEAD、删除失败重试、Marker 重跑和临时访问授权，不能只以单元测试或配置存在代替集成证据。

### 4.6 依赖边界和可观测性

- 双向黑名单完整快照有独立水位，空列表也能被证明为可信快照。
- 管理端展示外部依赖的可用、降级和不可用状态；公开健康接口不暴露内部拓扑细节。
- Banghua/TIM 故障不会掩盖 PostgreSQL、Redis 或 Web 自身故障，运维可以分别判断平台基础设施与外部能力状态。

## 5. Banghua / TIM 死亡后的能力矩阵

下表假设 PostgreSQL、Redis、Web App、Cookie/Session 密钥均正常，并且发送私聊所需的双向黑名单快照可信。“已迁移账号”指 active 用户具有有效 `UserCredential`、`ExternalAccount` 和上游 UID。

| 能力 | Banghua、TIM 正常 | Banghua 死亡，TIM 正常 | Banghua 正常，TIM 死亡 | Banghua、TIM 均死亡 |
|---|---|---|---|---|
| 已迁移账号密码登录 Web | 上游优先，成功后刷新本地摘要 | 本地登录 | 上游登录可用 | 本地登录 |
| 未迁移账号密码登录 | 上游成功后可机会式迁移 | 不可用 | 上游成功后可机会式迁移 | 不可用 |
| 短信登录、原注册认证 | 保持原流程 | 不可用 | 保持原流程 | 不可用 |
| Web 本地注册、找回密码 | 尚未实现 | 尚未实现 | 尚未实现 | 尚未实现 |
| 两个已迁移 Web 账号互发文本 | 本地权威，异步镜像 TIM | 本地权威，异步镜像 TIM | 本地权威；TIM delivery 积压 | 本地权威 |
| 本地会话、文本历史、搜索、未读和已读 | 可用 | 可用 | 可用 | 可用 |
| Web-local 完整资料、头像与隐私 | 本地权威；普通字段尽力镜像 Banghua | 本地权威 | 本地权威；普通字段尽力镜像 Banghua | 本地权威 |
| Web-Web 关注、好友申请/确认、好友和黑名单 | 本地权威，尽力镜像 Banghua | 本地权威 | 本地权威，尽力镜像 Banghua | 本地权威 |
| Web-local 文字/图片/视频动态、评论、点赞、话题、举报 | 本地权威；可映射操作尽力镜像 Banghua | 本地权威 | 本地权威；可映射操作尽力镜像 Banghua | 本地权威 |
| 最近 180 天历史动态、评论和话题 | 可主动导入并生成 Marker | 只能使用已完成导入 | 可主动导入并生成 Marker | 只能使用已完成导入 |
| 城市发现、附近列表、文本匹配 | 本地权威 | 本地权威 | 本地权威 | 本地权威 |
| 图片、语音、视频降级、文件、闪照、撤回 | Web-Web 本地权威，尽力兼容 TIM | Web-Web 本地权威，尽力兼容 TIM | Web-Web 本地权威；兼容 delivery 积压 | Web-Web 本地权威 |
| Web 向未迁移账号或 APK 发消息 | TIM/Banghua 兼容路径 | 条件可用，要求 TIM 凭证仍有效 | 不可用 | 不可用 |
| APK 兼容镜像 | 可用 | Banghua 写入不可用，TIM 视状态可用 | TIM 写入不可用，Banghua 视能力可用 | 不可用；Outbox 保留待处置 |
| 在线状态、输入状态、跨 APK 已读 | 依赖 TIM SDK/服务 | 依赖 TIM SDK/服务 | 不可用或仅本地状态 | 不可用或仅本地状态 |
| 语音匹配、漂流瓶、房间、群聊、礼物/资产/会员 | 仍依赖原服务 | 不可用或只读已归档数据 | 视 Banghua/其他厂商服务而定 | 不可用或只读已归档数据 |
| 私聊授权与黑名单 | 本地策略并同步上游 | 可信快照可降级；否则关闭发送 | 本地策略可用 | 可信快照可降级；否则关闭发送 |
| Web 基础健康 | 可用 | 可用并标记外部降级 | 可用并标记外部降级 | 可用并标记外部降级 |

“条件可用”不代表承诺 APK 在 Banghua 死亡后仍能启动或取得 TIM 凭证，只表示 Web 服务器已有目标 UID、TIM Secret 和可用 TIM 服务时，现有 TIM REST 兼容发送路径仍可工作。

## 6. 凭据迁移操作

### 6.1 两种迁移来源

机会式迁移无需人工命令：用户在 Banghua 正常时通过密码成功登录 Web，系统会登记或刷新本地 Argon2id 摘要。

历史 `password_encrypted` 本身不能证明它一定来自一次成功的上游密码认证：旧版本的短信登录路径可能曾保存请求中夹带的 password 字段。因此批量回填只允许处理已经通过独立日志、工单或人工核验确认来源的 ExternalAccount UUID 清单；无法证明来源的账号只能等待一次成功的上游密码登录做机会式迁移。

命令默认是全量 dry-run，只统计指定 Provider 下 active、存在 `password_encrypted`、但尚无 `UserCredential` 的候选；不会解密、运行 Argon2 或写数据库：

```bash
docker compose exec app python -m bbw_prod.credential_backfill
```

审核清单每行一个 ExternalAccount UUID，可先对该清单做定向 dry-run。清单从宿主机标准输入传入，不会写进容器镜像或命令输出：

```bash
docker compose exec -T app python -m bbw_prod.credential_backfill \
  --provider beibeiwu \
  --approved-account-ids-file - \
  --limit 50 < approved-external-account-ids.txt
```

只有同时提供 `--apply`、非空审核清单和显式 `--limit` 才会在进程内存中逐个解密并写入摘要。写入批次上限强制为 50，且每个账号使用独立短事务，完成后才处理下一个账号，不能使用无界回填：

```bash
docker compose exec -T app python -m bbw_prod.credential_backfill \
  --provider beibeiwu \
  --approved-account-ids-file - \
  --apply \
  --limit 50 < approved-external-account-ids.txt
```

确认小批次无失败后，再按不同的已审核清单受控执行；命令是幂等的，已有凭据会跳过。输出只包含以下计数和 Provider，不包含审核清单、ExternalAccount UUID、手机号、上游 UID、密文或明文：

- `dry_run`
- `scanned`
- `eligible`
- `created`
- `skipped_existing`
- `failed`
- `upstream_password_ciphertext_retained`

当 `failed` 非零时命令退出码为 1，应先检查密钥版本、历史密文完整性和应用日志，不应盲目扩大批次。`--apply` 不会删除或改写原 `password_encrypted`。

### 6.2 回填前置检查

执行任何 `--apply` 前必须完成：

1. PostgreSQL 已备份并实际验证可读取；`app_master_key`、`credential_keyring`、`phone_hmac_key` 和 `session_hmac_key` 已按同一时间点安全备份。
2. 已依次应用 `20260725_0009`、`20260725_0010` 和 `20260725_0011`，并确认 App、PostgreSQL、Redis、`worker`、`sync-worker`、`scheduler` 处于预期版本和健康状态。
3. 先运行不带 `--apply` 的全量 dry-run，记录候选数量；再对审核清单做定向 dry-run。正式写入必须使用 `--limit 50` 或更小批次评估 Argon2 CPU 和数据库锁等待。
4. 确认 `ExternalAccount.provider`、上游 UID 和内部 User 绑定没有冲突；计划保留的用户状态为 active。
5. 不能仅凭 `password_encrypted` 非空判断来源。审核清单中的每个 ExternalAccount 都必须有独立证据确认该密文来自成功的上游密码认证；短信请求、临时口令、来源不明或未经上游验证的数据一律不得进入清单。
6. 确认目标用户知道当前密码。Banghua 死亡后尚未实现本地找回、重置或注册，遗漏账号将无法补救。
7. 若要保证 Web-Web 消息可离线发送，通信双方都必须完成凭据迁移，并提前同步可信的双向黑名单快照。
8. Banghua/TIM 停服前，必须让计划保留的账号通过服务端可信 `/api/im/conversations` 同步已有会话，并确认对应 `web-policy/message_peer` 授权记录及 `message-peer-snapshot` 完成标记已生成。不得使用浏览器 TIM 归档、浏览器上报的会话或消息记录补造授权或完成标记。

可用以下只读查询核对覆盖率和 UID 冲突：

```sql
SELECT
  count(*) AS upstream_accounts,
  count(*) FILTER (
    WHERE u.status = 'active'
      AND uc.id IS NOT NULL
      AND uc.disabled_at IS NULL
      AND nullif(ea.upstream_uid, '') IS NOT NULL
  ) AS local_ready_accounts,
  count(*) FILTER (
    WHERE ea.password_encrypted IS NOT NULL AND uc.id IS NULL
  ) AS backfill_candidates
FROM external_accounts ea
JOIN users u ON u.id = ea.user_id
LEFT JOIN user_credentials uc ON uc.user_id = u.id
WHERE ea.provider = 'beibeiwu';

SELECT upstream_uid, count(*)
FROM external_accounts
WHERE provider = 'beibeiwu' AND nullif(upstream_uid, '') IS NOT NULL
GROUP BY upstream_uid
HAVING count(*) > 1;
```

第二条查询必须返回空结果。迁移后还应按产品目标核对 `local_ready_accounts / upstream_accounts`，不能只看命令是否退出成功。

### 6.3 统一迁移就绪检查

完成凭据回填、消息授权预热和各领域历史迁移后，运行聚合检查。命令不会解密凭据、写数据库或输出用户 ID、手机号、上游 UID、R2 凭据、Endpoint、Bucket、对象 key 或预签名 URL。它会在数据库事务之外使用随机隔离 key 向私有 R2 写入一个小型探针对象，通过 HEAD 和 GET 校验大小、SHA-256 与实际内容，然后删除并再次 HEAD 确认对象不存在：

```bash
docker compose exec app python -m bbw_prod.migration_readiness
```

用于 APK/外部 Provider 退役闸门时增加 `--require-ready`。它要求全部 active 账号同时满足本地认证与消息核心、四个领域完成 Marker，R2 当前真实具备写、读、删能力，同时要求所有 `compatibility.*` OperationOutbox 已完成，以及所有 `channel='tim'` MessageDelivery 已进入 `delivered` 或受控 `cancelled` 终态。R2 任一能力失败，或两类队列中存在 pending、retry、processing、failed、未知状态，都会使退出码为 1：

```bash
docker compose exec app python -m bbw_prod.migration_readiness --require-ready
```

检查分成六层，不应再把“认证和文本可用”误报为“整个平台可退役”：

| 层级 | 主要字段 | 含义 |
|---|---|---|
| 账号与消息核心 | `local_login_ready_accounts`、`local_message_identity_ready_accounts`、`block_snapshot_ready_accounts`、`message_peer_snapshot_ready_accounts`、`local_core_ready_accounts`、`local_core_ready` | 已迁移账号在双死亡时能本地登录并按可信策略进行 Web-Web 私聊 |
| 领域历史 | `social_ready_accounts`、`moments_ready_accounts`、`discovery_ready_accounts`、`media_ready_accounts`、`all_domains_ready_accounts`、`domain_data_ready` | 每个领域均有账号绑定、范围、计数、摘要和水位有效的完整 Marker |
| APK 兼容退役 | `compatibility_outbox_pending`、`compatibility_outbox_retry`、`compatibility_outbox_processing`、`compatibility_outbox_failed`、`compatibility_outbox_unfinished`、`compatibility_outbox_ready` | 尚有兼容写未完成时，Web-local 可继续服务，但不能宣称外部 Provider 已可永久删除 |
| 历史媒体归档退役 | `media_archive_outbox_pending`、`media_archive_outbox_retry`、`media_archive_outbox_processing`、`media_archive_outbox_failed`、`media_archive_outbox_unfinished`、`media_archive_outbox_ready` | `media.archive` 待办只认复验后的 `completed` 为终态（§4.5 不得直接取消，`cancelled` 一并计入未完成）；媒体 Marker 只在写入时点校验，Marker 之后新产生的待办由本层独立把关 |
| TIM 镜像退役 | `tim_delivery_pending`、`tim_delivery_retry`、`tim_delivery_processing`、`tim_delivery_failed`、`tim_delivery_other_unfinished`、`tim_delivery_unfinished`、`tim_delivery_ready` | 尚有 TIM 镜像或补偿动作未结束时不影响 Web-local 成功结果，但不能关闭兼容链路 |
| R2 当前能力 | `r2_write_ready`、`r2_read_ready`、`r2_delete_ready`、`r2_storage_ready`、`r2_capability_error_code` | 不是检查配置是否存在，而是以一次完整 PUT、HEAD/GET、DELETE 和删除后不存在校验确认生产私有媒体仍可读写和清理 |
| 最终切流 | `cutover_ready_accounts`、`cutover_ready`、`ready` | 全部 active 账号完成核心和四领域迁移，R2 当前能力通过，兼容 Outbox、`media.archive` 归档与 TIM delivery 均无未完成项 |

`r2_capability_error_code` 成功时为 `null`；失败时只输出稳定代码：`r2_initialization_failed`、`r2_write_failed`、`r2_write_verification_failed`、`r2_head_failed`、`r2_head_verification_failed`、`r2_get_failed`、`r2_get_verification_failed`、`r2_delete_failed`、`r2_delete_confirmation_failed` 或 `r2_delete_verification_failed`。报告不会包含 Provider 异常原文。`r2_probe_not_checked` 只用于未执行真实探针的内部评估调用；正式 CLI 每次都会执行探针，因此该值同样令最终 readiness 失败。

`fully_ready_accounts` 为兼容旧报表保留，但现在与 `cutover_ready_accounts` 同义，不再表示仅认证/文本就绪。Marker 使用以下独立 stream：`migration-domain:social`、`migration-domain:moments`、`migration-domain:discovery`、`migration-domain:media`。Marker 必须包含 `complete=true`、`source_complete=true`、账号和上游 UID 绑定、明确 scopes、非负 counts、覆盖起止时间、来源路径及 64 位记录摘要；社交 Marker 还必须包含七个固定来源的完整水位、精确 scope 覆盖和本人资料记录。动态 Marker 还必须是 `phase=complete`、声明 `history_days=180`，且来源精确为 `someonesluntannew`、`getMainComment`。发现 Marker 必须固定声明资料、匹配偏好和匹配结果三个来源、`counts.profile=1`，并以 `migration-domain:social` 作为资料来源 Marker。媒体 Marker 必须是当前账号的 `phase=complete`，固定声明资料、动态、TIM roaming history 和私有 R2 四个来源；其 history 必须包含完整状态、受控页数/消息数、与可信 peer 数相符的双向方向数及摘要，且 social、moments、message-peer 三个 prerequisite 摘要必须与当前严格 Marker 一致。缺失 Marker 与格式错误 Marker 分别进入 `missing_*_marker_accounts` 和 `invalid_*_marker_accounts`。

没有对应导入器或最终对账工具的领域应保持 Marker 缺失并令 `ready=false`，不能因为本地表已创建或存在少量数据而人工推断“历史已完整迁移”。这不会影响 `local_core_ready=true` 的 Web-local 账号继续运行，只会阻止 APK/外部 Provider 正式退役。

## 7. 上线与迁移顺序

1. 发布 Provider/Transport 边界、依赖状态和当前版本全部 additive 数据库迁移，不切断任何上游路径；先验证 PostgreSQL、Redis、R2、Worker、Scheduler 和密钥恢复能力。
2. 开启机会式密码迁移，观察一段时间后按审核清单执行小批量 dry-run/apply，提高存量账号覆盖率；在最终切流前完成密码、封禁、注销状态的最后同步。
3. 让所有 Web 文本发送统一经过 `/api/im/rest/send`；已迁移双方走 canonical 本地事务，未迁移目标继续 TIM 兼容路径。保持 TIM mirror、canonical ID 回流去重和历史归档对账，验证并行期不会双显、丢失或错误累计未读。
4. 在 Banghua/TIM 停服前，让计划保留的账号通过服务端可信 `/api/im/conversations` 完成已有会话同步，确认游标完整，生成 `web-policy/message_peer` 授权记录与 `message-peer-snapshot` 完成标记；浏览器 TIM 归档不能补造该门槛。
5. 启用 Web-local 资料、关系、动态、发现、文本匹配和私有媒体写路径，并验证每次本地事务成功都不依赖兼容镜像成功。R2 正式 Bucket 必须先完成本章 CORS 配置和浏览器直传验证。
6. 逐账号执行历史导入与最终对账。顺序固定为七来源社交迁移、180 天动态迁移、发现初始化，再在可信 `message-peer-snapshot` 已完成后运行历史媒体迁移；媒体迁移会实际读取双向 TIM roaming history 并把头像、动态和私聊原文件归档到 R2。其余领域只有在相应导入/对账工具能证明来源完整时才能写 Marker。没有工具或无法证明完整的领域必须保持 Marker 缺失，不能人工改成完成。
7. 演练 Banghua 不可达、TIM 不可达以及两者同时不可达，覆盖本地登录、Web-Web 文本与媒体发送、资料和关系修改、动态互动、发现、读取、重启恢复、并发幂等及黑名单 fail-closed。
8. 检查 `compatibility.*` OperationOutbox 和 `channel='tim'` MessageDelivery；先处置 failed、未知状态和长期 processing，再等待可以完成的 pending/retry 清零。迁移期保持 `BBW_COMPATIBILITY_MODE=enabled`；演练暂停时可设为 `paused`，此时 Web 本地写继续成功、待办继续保留，但 Scheduler 和已排队 Worker 不会再发起 Banghua/TIM 外呼。
9. 外部服务已永久死亡且剩余可选镜像不可能补发时，先把所有 app、worker、sync-worker、media-worker 和 scheduler 实例统一滚动到 `BBW_COMPATIBILITY_MODE=retired`，并确认旧的 `enabled` Worker 已退出。随后先执行只读预览，再以精确确认短语封存普通 `compatibility.*` 与非必需 TIM 待办：

   ```bash
   docker compose exec app python -m bbw_prod.compatibility_retirement
   docker compose exec app python -m bbw_prod.compatibility_retirement \
     --confirm-retire RETIRE-LEGACY-COMPATIBILITY
   ```

   封存只把可选待办改为可审计的 `cancelled`，不删行，也不取消 `media.archive`、`compatibility.media.archive*` 或 required TIM 记录。`retired` 下新产生的普通兼容镜像会直接写为终态 `cancelled`，不会形成永远无法清空的新积压。
10. 只有在本地凭据覆盖率、最终账号状态同步、双向黑名单快照、可信已有会话授权及保留领域 Marker 达到目标后，才把 `BBW_UPSTREAM_AUTH_MODE` 从 `provider-first` 改为 `local-only`。该切换会立即停止 Banghua 密码探测并拒绝短信登录，应先在演练环境验证再滚动发布。
11. 房间、群聊本轮明确不迁移；对应入口应在 APK 退役前下线或保持清晰的“不可用”状态，不能让 Web 主链路继续隐式调用旧服务。其他未保留能力也按同样原则显式下线。
12. `python -m bbw_prod.migration_readiness --require-ready` 返回 0，并确认本次随机 R2 探针的 PUT、HEAD/GET、DELETE 全部通过，以及生产请求、后台任务、恢复流程和运维脚本不再需要 Legacy Provider 后，才停止 APK 支持并从镜像中移除 `bbw_protocol`。Readiness 将经上述流程形成的 `cancelled` 视为终态，但仍会阻止 R2 能力失败以及任何未封存的 failed、processing、pending、retry 或未知状态。

## 8. APK 退役前仍不能保证的领域

当前离线承诺已经覆盖已迁移账号的密码登录、Web-Web 文本私聊、资料与关系、动态互动、城市发现、文本匹配以及 Web-Web 私有媒体。历史完整性仍以每账号领域 Marker 为准，而不能仅凭本地表存在来推断。以下能力尚未得到 Banghua/TIM 死亡后的完整保证：

- 新用户注册、短信登录、密码找回、密码重置、手机号换绑和账号申诉。
- 房间和群聊本轮明确不迁移；语音匹配、漂流瓶、任务、礼物、资产、会员等未建立本地权威模型的能力也不在离线承诺内。
- 在线状态、输入状态、APK 跨设备已读，以及未迁移的表情包或其他 TIM/SDK 专有展示能力。
- 刷脸、RoomKit、融云及其他厂商原生能力。
- 缺失或未通过校验的领域 Marker 所代表的历史资料、关系、动态、发现数据、媒体元数据和媒体原文件，以及其他只存在于外部服务的数据。
- Banghua UID 之外的新 Web-native 身份和外部服务死亡后的新用户开户。
- 只在上游发生、尚未同步到 Web 的最后一次密码修改、封禁、注销或关系变更。
- TIM 请求已被服务端接受但客户端超时的歧义结果。Outbox 是至少一次尝试，Web 会依靠 canonical ID 去重，但未理解该标识的旧 APK 不保证绝对不双显。

解析 APK API 只能提供兼容客户端，不会自动获得 Banghua/TIM 的服务端数据库、密钥、队列和业务状态。每个需要离线存活的领域都必须建立本地权威模型、幂等命令、审计、迁移和冲突规则。

## 9. 监控

基础检查：

```bash
curl -fsS "https://你的域名/api/health"
curl -fsS "https://你的域名/livez"
curl -fsS "https://你的域名/readyz"
docker compose ps
docker compose logs --since 30m app worker sync-worker scheduler
```

管理端应分别观察 Banghua auth/api、TIM message 等依赖的可用、降级、不可用状态。建议至少告警以下信号：

- `UPSTREAM_AUTH_UNAVAILABLE` 突增，以及随后统一的 `LOCAL_AUTH_REJECTED`、`LOCAL_AUTH_BUSY`、`LOCAL_AUTH_UNAVAILABLE` 比例；账号是否缺失凭据或已停用只应在内部聚合监控中区分，不能回显给匿名登录调用者。
- `LOCAL_MESSAGE_SERVICE_UNAVAILABLE`、幂等冲突或黑名单快照不可信导致的 fail-closed。
- TIM Outbox 的 pending/retry 最老时间、`attempt_count` 增长、failed 数量和 sync 队列积压。
- `compatibility.*` OperationOutbox 的 pending/retry/processing/failed/未知状态及最老未完成时间；该队列不清零时不得退役 APK 兼容链路。
- `sync-worker`、`scheduler` 重启或停止，Redis noeviction 写失败，PostgreSQL 连接和事务失败。
- 本地凭据覆盖率下降、active 用户缺失上游 UID，或密钥文件版本与数据库密文不匹配。

TIM Outbox 可用只读查询检查：

```sql
SELECT
  status,
  count(*) AS deliveries,
  min(available_at) AS oldest_available_at,
  max(attempt_count) AS max_attempt_count
FROM message_deliveries
WHERE channel = 'tim'
GROUP BY status
ORDER BY status;
```

Banghua 兼容写可用以下只读查询检查：

```sql
SELECT
  operation_type,
  status,
  count(*) AS operations,
  min(created_at) AS oldest_created_at,
  min(available_at) AS oldest_available_at,
  max(attempt_count) AS max_attempt_count
FROM operation_outbox
WHERE operation_type LIKE 'compatibility.%'
GROUP BY operation_type, status
ORDER BY operation_type, status;

SELECT
  count(*) FILTER (WHERE status <> 'completed') AS compatibility_outbox_unfinished
FROM operation_outbox
WHERE operation_type LIKE 'compatibility.%';

SELECT
  count(*) FILTER (WHERE status <> 'completed') AS media_archive_outbox_unfinished
FROM operation_outbox
WHERE operation_type = 'media.archive';
```

TIM 最终退役检查应单独确认未完成数量；`cancelled` 是本地撤回产生的受控终态，其他未识别状态一律阻止切流：

```sql
SELECT
  count(*) FILTER (
    WHERE status NOT IN ('delivered', 'cancelled')
  ) AS tim_delivery_unfinished
FROM message_deliveries
WHERE channel = 'tim';
```

TIM 长期死亡时，pending/retry/failed 增长是预期降级，不应把它误判为 Web 本地消息失败；但必须监控表增长和保留策略。正式退役时，`compatibility_outbox_unfinished`、`media_archive_outbox_unfinished` 和 `tim_delivery_unfinished` 都必须为 0，并以统一 readiness 命令结果为最终判断。

### 9.1 Cloudflare R2 Bucket CORS

浏览器通过预签名 URL 直接 PUT 到 R2，因此 CORS 是 Bucket 层运维配置。`BBW_R2_*` 应用配置只能决定签名和对象位置，不能替代 Bucket CORS；如果 staging 与 private 使用不同 Bucket，两边都必须配置。正式环境必须精确列出 Web Origin（协议、域名和端口均需一致），禁止使用 `*`。

Cloudflare R2 Bucket 的 CORS 规则应至少等价于以下配置，将示例域名替换为真实正式域名；不要把末尾路径或斜杠写进 Origin：

```json
[
  {
    "AllowedOrigins": [
      "https://web.example.com"
    ],
    "AllowedMethods": [
      "PUT",
      "GET",
      "HEAD"
    ],
    "AllowedHeaders": [
      "cache-control",
      "content-type",
      "x-amz-meta-deployment",
      "x-amz-meta-intent-id",
      "x-amz-meta-kind",
      "x-amz-meta-owner-user-id",
      "x-amz-meta-sha256",
      "x-amz-meta-size-bytes"
    ],
    "ExposeHeaders": [
      "ETag"
    ],
    "MaxAgeSeconds": 600
  }
]
```

预检可用真实预签名 PUT URL 验证。URL 必须整体加引号，避免查询参数被 Shell 拆开：

```bash
curl -i -X OPTIONS "$SIGNED_PUT_URL" \
  -H "Origin: https://web.example.com" \
  -H "Access-Control-Request-Method: PUT" \
  -H "Access-Control-Request-Headers: cache-control,content-type,x-amz-meta-deployment,x-amz-meta-intent-id,x-amz-meta-kind,x-amz-meta-owner-user-id,x-amz-meta-sha256,x-amz-meta-size-bytes"
```

响应必须包含与请求完全一致的 `Access-Control-Allow-Origin`，并允许 PUT 和上述请求头。随后在浏览器开发者工具中用应用返回的 `upload_url`、`required_headers` 和原始文件执行真实 PUT；成功响应应为 2xx，并能通过 `response.headers.get('etag')` 读取 ETag。再用私有读取 URL 分别验证 GET；若前端会主动 HEAD，也应验证 HEAD。

CORS 更新可能存在边缘传播时间，浏览器还会按 `MaxAgeSeconds` 缓存预检结果。修改规则后应等待传播，并用禁用缓存的开发者工具或全新浏览器会话复测，不要为了绕过旧缓存把正式 Origin 改成 `*`。

故障判断应先看 OPTIONS：OPTIONS 返回 403、缺少 `Access-Control-Allow-Origin`，或未允许某个 Header，通常是 Bucket CORS、Origin 或 Header 白名单问题。OPTIONS 正常但真实 PUT 返回 403，且响应体含 `SignatureDoesNotMatch`、`AccessDenied` 或过期信息，通常是预签名 URL 过期、时钟偏差，或 `content-type`、`cache-control`、`x-amz-meta-*` 的实际值与签名值不一致。浏览器有时会把真实请求的 403 也显示成 CORS 错误，应使用同一 URL 和完全相同 Header 通过 curl 复现后再归因。

## 10. 回滚与故障处置

- 本轮 `20260725_0009` 至 `20260725_0017` 以新增表、索引、约束和新增列为主；生产必须应用发布镜像对应的完整 migration head。应用异常时优先回滚应用镜像并保留这些结构，不要在仍有新版本实例写入时执行 downgrade。
- `20260725_0010` 的 downgrade 会删除 canonical 消息表。canonical 已承载用户消息后，禁止用数据库 downgrade 作为普通回滚手段。
- `20260725_0017` 的 downgrade 会删除头像/动态当前媒体引用，并使 `/api/media/native/.../content` 全部失效；已有 Web-native 头像或动态媒体后同样禁止把 downgrade 当作普通应用回滚。
- 凭据 `--apply` 保留上游加密密码，因此不会破坏恢复后的 Banghua/APK 登录。出现问题时停止后续批次、保留审计和计数，修复原因后重跑；不要批量删除 `user_credentials`。
- TIM mirror 首次任务由普通 `worker` 消费，到期重试由 `sync-worker` 消费；当前没有独立的 TIM mirror 总开关。若必须完全暂停镜像，需要同时考虑两个 Worker，且会连带暂停它们承载的其他任务。不要删除 `message_deliveries`；修复并重启 Worker 后，Scheduler 会重新派发 pending/retry，已经达到 8 次上限的 failed 记录需审核后受控重放。
- 本地消息已经返回成功后，canonical 记录就是权威数据。不能因为 TIM 镜像失败而删除、改写或向用户返回失败；应保留 Outbox 并异步恢复兼容同步。
- 若需数据库恢复，必须使用相匹配的 PostgreSQL 备份和密钥备份。只恢复数据库、不恢复 HMAC/加密密钥，会造成手机号查找、Session 或历史可逆凭据不可用。
- 真正切断 Banghua/TIM 前应保留可回滚窗口、旧镜像和只读对账报告；回滚只改变流量和实现，不回退或丢弃已接受的本地身份、消息和回执。

## 11. APK 退役门槛

至少满足以下条件后，才可以把 APK 标记为正式退役：

1. 计划保留的存量账号全部具有可用本地密码凭据，并完成密码遗忘和遗漏账号处置方案。
2. Banghua/TIM 双断网演练中，本地登录、Web-Web 文本发送、历史、未读/已读、重启和多实例恢复均通过。
3. Web 与 APK 并行期的 TIM mirror、历史回流、canonical 去重和未读对账稳定，失败 Outbox 可恢复。
4. Banghua/TIM 停服前已通过服务端可信 `/api/im/conversations` 同步计划保留账号的已有会话，并核验 `web-policy/message_peer` 授权与 `message-peer-snapshot` 完成标记覆盖率；任何浏览器 TIM 归档或浏览器上报数据均未被用于反推、补造私聊授权或伪造完成标记。
5. 所有 active 账号的 `migration-domain:social`、`migration-domain:moments`、`migration-domain:discovery`、`migration-domain:media` Marker 均存在且通过账号绑定、范围、计数、摘要和水位校验；不能以人工插入 Marker 代替完整导入证据。
6. `compatibility_outbox_unfinished=0`、`media_archive_outbox_unfinished=0` 且 `tim_delivery_unfinished=0`；pending、retry、processing、failed 和未知状态均已处置；`r2_write_ready=true`、`r2_read_ready=true`、`r2_delete_ready=true` 且 `r2_storage_ready=true`，最终 `python -m bbw_prod.migration_readiness --require-ready` 返回 0。
7. 资料、关系、黑名单、动态、文本匹配、媒体及其他保留产品能力已经建立本地权威；房间、群聊本轮明确下线或不再从 Web 路由，其他未迁移能力也已显式处置，而不是静默失效。
8. 注册、找回和新用户策略已单独实现或明确产品停止新增用户。本阶段确认的“死亡后仅密码登录”不能长期替代完整账号生命周期。
9. 默认 Provider 已替换为 Web-native 实现，生产 Web、Worker、Scheduler、恢复脚本和部署镜像不再需要 `bbw_protocol`。
10. 已完成数据库、R2 对象与 Bucket CORS、密钥、队列和灾难恢复演练，并建立持续监控、容量和保留策略。

在这些门槛之前，正确做法是渐进迁移和降级运行，而不是一次性删除 APK 兼容链路。
