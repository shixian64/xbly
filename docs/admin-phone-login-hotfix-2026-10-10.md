# 管理员手机号登录补修与部署（2026-10-10）

## 结果与范围

已部署至 `https://xbly.shixinn.com`。管理员用户管理中的手机号登录，不再要求预先存在本地用户、外部账号记录或上游 UID，也不要求邀请码；仍须完成上游认证，成功后建立本地账号/绑定。

普通用户的密码/短信入口不变。管理员鉴权、同源校验、限流、停用账号检查、已确认上游身份一致性、账号归属冲突检查和审计均保留。既有空值/空字符串/纯空白 UID 视为尚未绑定，不允许覆盖已有非空身份，也不会通过管理员登录创建普通用户密码。

修改涉及 `bbw_web/admin_api.py`、`bbw_prod/services.py`，以及三个相关测试文件和 README。仅对已认证管理员请求复制预检查上下文并关闭邀请码要求，不改变普通登录的默认策略。

## 验证

- 在运行镜像原始代码上复现旧限制：未绑定账号在调用上游前返回 409。
- 使用实际生产镜像、无网络、只读容器、无生产环境变量/密钥/数据库挂载，运行 5 个相关测试模块：72 项通过，0 跳过。
- 覆盖首次登录、缺失绑定、空 UID、邀请码隔离、身份冲突、停用账号、限流、上游失败、审计失败和会话轮换。
- 新旧 Compose 配置解析通过；本地 Python AST 检查与 `git diff --check` 通过，未执行构建。
- 切流后核对容器内两个文件的 SHA-256 与验收候选一致；容器 healthy，重启次数 0，未 OOM。
- 内网与公网 `/healthz`、`/readyz` 均返回 200；无管理员认证和伪造普通 Cookie 返回 401；跨源请求返回 403。
- 新容器检查时无 Traceback / ERROR / CRITICAL 日志；其他工作容器未重建，数据库及 Redis 健康。

测试使用模拟上游，不代表实际用户的上游凭据必然可用；未替用户执行真实手机号登录，请在管理端使用原手机号重试。

## 实际发布方式

服务器仓库基线为 `8ee86d2`，本地基线为 `da02827`，因此补丁施加到当前运行文件，而非用本地旧文件覆盖服务器。

镜像仍为 `bbw-app:2026.09.28-8ee86d2-full`，未构建或发布新镜像。已验证的新代码保存在：

`/home/shixian/project/xbly/.migration/admin-phone-login-20261010/release/`

服务器 `compose.backend.yaml` 为 app 增加两个只读文件挂载，分别覆盖 `/app/bbw_web/admin_api.py` 和 `/app/bbw_prod/services.py`。因此服务实际执行的是修复代码，但镜像本身尚未包含本补丁。不要删除此 release 目录，也不要在下一次部署中无意丢掉挂载。

仅用 `docker compose up -d --no-deps --no-build --pull never app` 重建应用容器，未运行数据库迁移；`.env` 哈希切流前后一致，未改密钥、生产数据或其他服务。

验收、配置备份、发布元数据和日志位于同一审计目录；`app-inspect.private.json` 可能包含敏感运行配置，不得直接分享。

## 回滚

原始配置保存在：

`/home/shixian/project/xbly/.migration/admin-phone-login-20261010/compose.backend.original.yaml`

回滚应用需要恢复此配置，并在 `/home/shixian/project/xbly` 执行（只重建 app，不构建、不拉取）：

```bash
docker exec bbw-app-1 python /app/docker/healthcheck.py drain
cp .migration/admin-phone-login-20261010/compose.backend.original.yaml compose.backend.yaml
docker compose --project-name bbw --project-directory /home/shixian/project/xbly --env-file .env -f compose.yaml -f compose.backend.yaml up -d --no-deps --no-build --pull never app
docker exec bbw-app-1 python /app/docker/healthcheck.py resume
```

恢复后确认 `/readyz` 返回 200 且容器 healthy。原运行源码另保存在审计目录 `baseline/`；服务器源码同步不影响本次镜像回滚，但若要继续以源码发布，应同时撤回两个运行文件的补丁。

本轮未提交、未推送；后续正常发布应将补丁纳入镜像，验收后再移除本次只读热修复挂载。
