# 文档索引

**项目根：** 仓库根目录（`bbw_protocol` / `bbw_web` / `docs` / `tools`）  
**客户端版本：** 154（`xbly.apk`）  
**原则：** 分析结论落盘；真实密钥与本地 session 不提交 Git。生产 Session 按部署文档写入 PostgreSQL/Redis。

---

## 阅读顺序（建议）

1. [00_OVERVIEW](./00_OVERVIEW.md) — 总览  
2. [02_PROTOCOL](./02_PROTOCOL.md) — 协议  
3. [10_PROTOCOL_CLIENT](./10_PROTOCOL_CLIENT.md) — 怎么用客户端  
4. [04_FINDINGS](./04_FINDINGS.md) — 风险清单  
5. 生产部署：[14](./14_PRODUCTION_DEPLOYMENT.md)
6. 性能优化：[16](./16_PERFORMANCE_OPTIMIZATION.md)
7. 已退役的 Web 本地权威迁移方案（历史记录）：[17](./17_WEB_PROVIDER_MIGRATION.md)
8. 内置 BYOK 模型运行器、账号动作与受控无人值守 Agent：[18](./18_BYOK_MODEL_RUNNER.md)
9. 专题：实名 [08](./08_RP_VERIFY_BYPASS_ANALYSIS.md) / 游客 [09](./09_GUEST_CAPABILITY_MATRIX.md) / 覆盖 [11](./11_FEATURE_REALNAME_AND_COVERAGE.md) / 原生 [12](./12_NATIVE_INTEGRATION.md) / v154 [13](./13_APK_V154_DIFF.md)

---

## 文档表

| 文档 | 内容 |
|---|---|
| [00_OVERVIEW.md](./00_OVERVIEW.md) | 项目总览、结构、进度 |
| [01_STATIC_ANALYSIS.md](./01_STATIC_ANALYSIS.md) | 静态逆向：结构、密钥、鉴权 |
| [02_PROTOCOL.md](./02_PROTOCOL.md) | Header / 签名 / 登录注册 / 资料 |
| [03_TEST_LOG.md](./03_TEST_LOG.md) | 测试流水账 T01–T13 |
| [04_FINDINGS.md](./04_FINDINGS.md) | 漏洞/风险 P0–P3 |
| [05_ACCOUNT.md](./05_ACCOUNT.md) | 测试账号与会话状态（无明文密码） |
| [06_TOOLS.md](./06_TOOLS.md) | 工具与命令 |
| [07_NEXT.md](./07_NEXT.md) | 下一步 |
| [08_RP_VERIFY_BYPASS_ANALYSIS.md](./08_RP_VERIFY_BYPASS_ANALYSIS.md) | 实名链路与绕过面 |
| [09_GUEST_CAPABILITY_MATRIX.md](./09_GUEST_CAPABILITY_MATRIX.md) | 游客能力矩阵 |
| [10_PROTOCOL_CLIENT.md](./10_PROTOCOL_CLIENT.md) | 协议客户端落地 |
| [11_FEATURE_REALNAME_AND_COVERAGE.md](./11_FEATURE_REALNAME_AND_COVERAGE.md) | 实名门槛与覆盖 |
| [12_NATIVE_INTEGRATION.md](./12_NATIVE_INTEGRATION.md) | IM / 刷脸 / RoomKit 与只读会员权益 |
| [13_APK_V154_DIFF.md](./13_APK_V154_DIFF.md) | 148→154 差异 |
| [14_PRODUCTION_DEPLOYMENT.md](./14_PRODUCTION_DEPLOYMENT.md) | Docker、PostgreSQL、Redis、R2、管理端、安全与运维 |
| [15_APK_SECURITY_AUDIT.md](./15_APK_SECURITY_AUDIT.md) | APK 安全审计补充记录 |
| [16_PERFORMANCE_OPTIMIZATION.md](./16_PERFORMANCE_OPTIMIZATION.md) | Web 首屏、数据链路、后台任务与流畅度优化 |
| [17_WEB_PROVIDER_MIGRATION.md](./17_WEB_PROVIDER_MIGRATION.md) | 已退役的 Web 本地权威迁移设计，仅供历史追溯；当前产品接口以原 APK/TIM 为权威 |
| [18_BYOK_MODEL_RUNNER.md](./18_BYOK_MODEL_RUNNER.md) | 内置 BYOK 模型运行器、三层管理与用户门禁、固定账号动作、专用 Agent Worker、受控无人值守范围与安全边界 |
| [DEEP_DIVE.md](./DEEP_DIVE.md) | 早期深挖（已并入 01/02/04） |
| [api_catalog.json](./api_catalog.json) | action 目录 |

---

## 代码入口

| 路径 | 说明 |
|---|---|
| `.../bbw_protocol/` | 协议核 + CLI |
| `.../bbw_web/` | 多用户 Web |
| `.../bbw_prod/` | 生产持久化、加密、Session 和服务层 |
| `../compose.yaml` | 生产 Docker Compose 拓扑 |
| `../tools/` | 可选探测脚本 |

```powershell
python -m bbw_protocol.cli whoami
python -m bbw_web --port 8765
```

---

## 记录规范

1. 协议测试 → 追加 [03_TEST_LOG.md](./03_TEST_LOG.md)  
2. 新结论 → [04_FINDINGS.md](./04_FINDINGS.md) + 对应专题  
3. 账号变化 → [05_ACCOUNT.md](./05_ACCOUNT.md)  
4. 新工具 → [06_TOOLS.md](./06_TOOLS.md)  
5. **勿**提交 `session.json` / 密码 / APK  
