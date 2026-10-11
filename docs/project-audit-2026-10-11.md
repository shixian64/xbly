# xbly 多方向审查与修复记录

审查日期：2026-10-11。基于提交 `8ee86d2` 以及开始审查时已有的本地热修复。

本轮覆盖认证与会话撤销、HTTP 请求边界、消息协议与异步任务、前端异步状态、CSV 导出、BYOK 网关、配置安全及测试一致性。已对确认的问题完成代码修复和回归验证；这不是生产环境无缺陷或安全认证结论。

## 认证与安全边界

### 会话撤销竞态

**优先级 P1。** 原 `UserSessionService.revoke()` 在数据库提交前删除 Redis；并发 `recover()` 可读取旧的已提交行，再把未撤销状态写回缓存。之后 `touch()` 的 300 秒快路径不检查数据库撤销标记，退出或修改密码后的旧 SID 可能继续被接受。

修复：缓存未命中的恢复、单会话撤销、批量撤销共用会话行锁；批量按会话 ID 顺序加锁。锁读使用 `populate_existing=True` 刷新 SQLAlchemy identity map，避免已加载的旧 ORM 对象绕过撤销状态。

相关代码：`bbw_prod/services.py`、`bbw_prod/repositories.py`。回归：`tests/test_user_session_revocation.py` 覆盖单/批撤销、两种先后交错、正常缓存及旧对象刷新。锁交错使用可控提交快照模拟，SQLite 只验证 ORM 刷新，仍需真实 PostgreSQL 并发验收。

### 非法 Cookie 导致服务器错误

**优先级 P2。** SID 哈希使用 ASCII 编码；非 ASCII Cookie 可使用户会话操作或管理员退出抛出异常。

修复：用户 `recover/touch/revoke` 对空、非字符串或非 ASCII SID 返回未认证/未撤销；管理员 Cookie 入口把非 ASCII SID 当作无有效会话，退出仍正常清 Cookie。合法短 ASCII SID 保持兼容。

回归：`tests/test_user_session_revocation.py`、`tests/test_admin_cookie_boundary.py`。管理员测试通过真实 FastAPI HTTP 路由覆盖无效 Cookie 返回 401、退出返回 200 且不访问数据库、合法 SID 正常撤销。

### 本地运行时不应保留上游凭据

**优先级 P2。** Web-native 无网络运行时从状态或旧快照创建时仍保留上游 Token，直接传入会话还可能保留密码，与隔离契约不符。

修复：`bbw_web/providers/web_native.py` 不再复制状态中的 Token；创建 native 会话副本时清空 Token 和密码，不修改调用方原对象或源文件。身份、设备、昵称及本地登录状态不受影响。

回归：`tests/test_web_native_runtime_contracts.py` 覆盖状态恢复、直接传入对象、旧快照加载三个入口。

### CSV 导出公式注入

**优先级 P1。** 上游 CSV 原样下载及浏览器离线 CSV fallback 均允许消息、昵称成为表格公式；仅双引号转义不能阻止公式解释。

修复：服务端解析 CSV，浏览器处理原始文本单元格，对空白/BOM 后的 `= + - @` 及开头的 Tab/CR/LF 增加文本前缀。服务端保留 BOM、逗号、引号、多行结构；损坏 CSV 返回 502，不回退为未清洗下载。导出过程中切换账号会终止旧下载。

相关代码：`bbw_web/bff_server.py`、`bbw_web/static/app.js`。回归：`tests/test_chat_export_security.py`、`tests/frontend_runtime.test.cjs`。使用无害公式文本验证，未打开或执行表格公式。

## HTTP 资源与断连处理

### JSON 请求体预算绕过

**优先级 P2。** 原中间件只识别包含 `application/json` 的头，`application/*+json` 被 FastAPI 按 JSON 解析却获得较大的上传预算；缺失类型也会先缓冲到上传预算。重复 `Content-Type` 还存在首值/末值解释不一致。

修复：统一 JSON 媒体类型及缺失类型的预算，拒绝重复 `Content-Type`（400），累计分块实际大小，超限返回 413；正常 multipart 仍使用上传预算。

可复现输入：JSON 上限 32 字节、上传上限 128 字节，58 字节 JSON 配合 `application/problem+json`，或依次提供 JSON、octet-stream 两个类型头。修复前可进入业务函数，修复后在解析前拒绝。

相关代码：`bbw_web/api.py`。回归：`tests/test_request_body_limits.py`，同时覆盖重复头的两种顺序、相同重复头和分块上传。

### 请求体重放吞掉断连

**优先级 P2。** 缓冲请求体重放完毕后，原实现不断返回空 `http.request`，不再等待真实事件；断连监听可能忙循环，也无法收到 `http.disconnect`。

修复：只重放一次完整请求体，之后委托原始 `receive()`，保留真实等待及断连语义。

回归：`tests/test_request_body_limits.py` 验证断连透传、后续读取保持等待、上传中途断开不派发业务且释放并发槽。

## 消息协议与异步可靠性

### 新的已读请求被旧内容摘要去重

**优先级 P1。** 相同回执列表，尤其空列表，被当作相同意图；同步标记有效期为 24 小时，后续新消息的已读可能被跳过。

修复：每次请求使用独立意图版本；worker 仍按最新状态合并待处理任务，同一意图重放仍可去重。

相关代码：`bbw_web/persistence.py`。回归：`tests/test_message_reliability_regressions.py` 覆盖相同秒请求、相同列表、派发中更新及 A→B→A 状态变化。

### JD 确认关联与等待边界错误

**优先级 P1。** 原 WebSocket 接收过程遇到其他消息的 ACK/NACK 就结束，并最多读取三帧；通知插入可能导致错误拒绝或虚假超时。可读文字 `content` 还可能遮蔽明确的消息 ID。

修复：按消息 ID 关联确认，跳过无关确认与通知，使用总计三秒截止；保留 APK 的 `content=原消息 UUID` 兼容格式。不确定发送状态仍禁止盲目 HTTP 补发。

相关代码：`bbw_protocol/adapters/jd_chat.py`。回归：`tests/test_message_reliability_regressions.py`，全部使用模拟 WebSocket，不连接真实 JD 服务。

### 后台任务客户端未关闭

**优先级 P2。** TIM 镜像发送与历史同步缺少自建 transport 的成功/异常清理。

修复：`bbw_web/jobs.py` 在 finally 关闭自建客户端，不关闭调用方借用对象；close 异常只记录警告，不把已经发送成功的消息变成重试。

回归：`tests/test_message_job_lifecycle.py` 覆盖成功、拒绝、异常、借用对象和清理失败。

## 前端会话及界面竞态

- **旧请求污染新账号，P1。** 用户和管理员 API 在请求、正文读取及异常分支绑定会话代次；旧成功响应、旧 401、旧网络错误不能作用于新登录。延迟注销清理也不再删除新账号存储。
- **非 JSON 的 401 不清理登录，P2。** 先根据 HTTP 状态处理认证，再解析正文。登录接口、`authOptional` 与正常 403 保持原行为。
- **后台快速筛选或翻页被旧结果覆盖，P2。** 用户、邀请码、审计列表增加独立请求序号，同时检查视图及账号代次；旧成功或失败结果均不能覆盖新列表。
- **分批已读回执跨会话执行，P1。** 绑定发起时的账号代次及 SDK 实例，每次 await 后再验证；延迟失败也不能回滚新账号未读数。正常 35 条回执仍按 30+5 分批并去重。

相关代码：`bbw_web/static/app.js`、`bbw_web/static/admin.js`。

`tests/frontend_runtime.test.cjs` 直接抽取执行真实生产函数，通过延迟 Promise、模拟网络和 DOM 边界验证；另覆盖账号隔离的 storage 事件，不是只搜索源码字符串。`tests/test_frontend_runtime.py` 已将 30 个 Node 用例接入 Python 测试发现。

## BYOK 和配置

- **无效配置被缓存，P1。** `get_settings()` 原先在校验之前发布全局缓存；首次失败后后续调用可直接得到不合法配置。现在先构造并校验，成功后再缓存。
- **敏感配置进入日志的风险，P2。** `Settings` 默认 repr 不再包含数据库/Redis URL、主密钥、密钥环、HMAC 密钥及部署令牌。实际读取字段不受影响。
- **模型压缩响应绕过内存预算，P1。** HTTPX 在大小检查前自动解压。现在显式请求 `Accept-Encoding: identity`，若服务忽略该要求返回压缩内容，在读取/解压前拒绝并关闭资源。明文流仍逐块累计限额。
- **异常用量字段导致正常模型结果失败，P2。** token 用量为无穷数时会抛 `OverflowError`；现在忽略该可选异常字段，保留正常完成结果。

相关代码：`bbw_prod/config.py`、`bbw_agent/model_gateway.py`。回归：`tests/test_agent_config_safety.py`。

**兼容性变化：** 不遵守 identity 请求头的模型供应商会被拒绝，需要其提供未压缩响应；本轮未引入复杂的自定义有界解压器。

## 测试一致性和验证结果

- 同步用户及管理员页面的 JS 资源 hash 及用户页 CSS hash；新增 `.gitattributes` 将直接引用的静态 JS/CSS/HTML 固定为 LF，避免 Windows CRLF 与 Git/Linux LF 产生不同内容 hash。
- 协议目录已是 APK v162；原测试误将当前活动 action 数量与 v154 的历史数量比较。现在按当前版本验证，同时保留 v154 与新增 action 的一致性检查，未篡改目录统计来迎合测试。
- 更新少数过时源码断言及已读回滚测试环境；新增行为测试验证安全属性，没有删除或削弱原业务断言。
- 新增 54 个 Python 测试方法，其中一个入口运行 30 个 Node 行为用例。

最终完整测试发现 **1110 项，1109 项通过，1 项跳过，0 失败，0 错误**。跳过的是依赖 `BBW_TEST_DATABASE_URL` 的 PostgreSQL 媒体集成测试；该环境未配置。Node 的 30 个用例全部通过，已包含在 Python 包装测试内，不重复累计。

同时通过 `node --check` 两个生产脚本及 `git diff --check`。Windows 的 LF/CRLF 提示不是测试失败。

### Windows 复现命令

本机 Python 3.14.5、Node v24.15.0。缺失依赖以 binary wheel、`--no-compile` 安装到仓库隔离目录；未修改全局 Python 包或项目 requirements。

```powershell
Set-Location D:\project\AI\xbly
$env:PYTHONPATH = Join-Path $PWD '.git\codex-audit-20261011\deps'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:BBW_TEST_DATABASE_URL = ''

python -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/frontend_runtime.test.cjs
node --check bbw_web/static/app.js
node --check bbw_web/static/admin.js
git diff --check
```

详细本机测试日志：`.git/codex-audit-20261011/final-tests.log`；结果摘要：`.git/codex-audit-20261011/final-summary.json`。这两个文件和临时依赖不进入版本控制。

## 仍需验证及后续风险

1. **真实基础设施联调。** PostgreSQL 行锁/死锁、Redis/RQ 多 worker、进程崩溃恢复及上游 TIM/JD 的实际协议行为需要专用测试环境。当前模拟结果不能代替这些验证，也未做部署。
2. **真实浏览器验收。** 需在浏览器验证切号、页面隐藏/恢复、SDK 断线重连、富媒体导出下载和双标签页；本轮是函数与路由级离线测试。
3. **依赖可复现性。** `requirements.txt` 主要使用版本区间，部署还应固定并验证一组依赖和运行时版本；本轮未凭空生成未经生产验证的锁文件。
4. **大模块维护成本。** 用户前端约 2.28 万行、BFF 约 7700 行，源码字面断言较多。建议后续按会话、消息、导出、管理列表逐步拆分，并持续转为行为测试；本轮不做高风险大面积重构。
5. **跨租户检查边界。** 已抽查归档/媒体查询的 owner/参与者条件及管理员敏感操作门禁，未发现有足够证据确认的新 IDOR；这不是对所有路由不存在越权的保证。

开始审查时已有的管理员手机号登录、账号绑定及其测试热修复均已核对保留。没有执行编译、构建、adb、真实账号动作，也没有提交或推送 Git。
