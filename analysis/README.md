# beibeiwu.apk CTF 逆向文档索引

> 工作目录：`D:\project\AI\bbw`  
> 目标 APK：`beibeiwu.apk`  
> 包名：`xin.banghua.beiyuan0`  
> 版本：`154`（对照包 `xbly.apk`；旧包 `beibeiwu.apk` 为 148）
> 文档维护原则：**凡分析、测试、结论，一律写入本文档体系；禁止只口头结论不落盘。**

---

## 文档目录

| 文档 | 内容 |
|---|---|
| [00_OVERVIEW.md](./00_OVERVIEW.md) | 项目总览、目录结构、进度与待办 |
| [01_STATIC_ANALYSIS.md](./01_STATIC_ANALYSIS.md) | 静态逆向：结构、密钥、鉴权、接口、安全缺陷 |
| [02_PROTOCOL.md](./02_PROTOCOL.md) | 协议说明：Header/签名/登录注册/资料修改 |
| [03_TEST_LOG.md](./03_TEST_LOG.md) | **测试流水账**（按时间顺序，含请求响应结论） |
| [04_FINDINGS.md](./04_FINDINGS.md) | 漏洞/风险结论汇总（P0–P3） |
| [05_ACCOUNT.md](./05_ACCOUNT.md) | 测试账号状态、会话、字段可改性 |
| [06_TOOLS.md](./06_TOOLS.md) | 工具脚本用法 |
| [07_NEXT.md](./07_NEXT.md) | 下一步计划（含用户自行实名后动作） |
| [08_RP_VERIFY_BYPASS_ANALYSIS.md](./08_RP_VERIFY_BYPASS_ANALYSIS.md) | 实名链路与绕过面分析（含实测） |
| [09_GUEST_CAPABILITY_MATRIX.md](./09_GUEST_CAPABILITY_MATRIX.md) | 游客/未登录能力矩阵与协议可落地性 |
| [10_PROTOCOL_CLIENT.md](./10_PROTOCOL_CLIENT.md) | **全功能协议客户端**落地说明 |
| [11_FEATURE_REALNAME_AND_COVERAGE.md](./11_FEATURE_REALNAME_AND_COVERAGE.md) | 实名门槛功能 / 其他功能 / 协议覆盖评估 |
| [12_NATIVE_INTEGRATION.md](./12_NATIVE_INTEGRATION.md) | **IM / 刷脸 / 支付** 原生能力集成 |
| [13_APK_V154_DIFF.md](./13_APK_V154_DIFF.md) | **xbly.apk v154** 与旧包差异 / 协议跟版 |
| [bbw_protocol/README.md](./bbw_protocol/README.md) | 协议库使用手册 |
| [bbw_web/README.md](./bbw_web/README.md) | BFF + Web 脚手架 |
| [DEEP_DIVE.md](./DEEP_DIVE.md) | 早期深挖报告（保留，内容已并入 01/02/04） |

---

## 产物路径（精简后）

| 路径 | 说明 |
|---|---|
| `../xbly.apk` / `../beibeiwu.apk` | 原始 APK（本地保留，**gitignore**，不入库） |
| `./bbw_protocol/` | **协议核**（CLI / 单会话） |
| `./bbw_web/` | **Web 层**（多用户 BFF + UI） |
| `./api_catalog.json` | action 目录（v154） |
| `./bbw_client.py` 等 | 早期探测脚本（可选；主入口用 `bbw_protocol`） |
| `session.json` / `sessions/` | 登录会话（本地，**gitignore**） |

**已清理、勿再提交：** `jadx_out/`、`apk_extracted/`、`analysis/assets/`、`asset_408037528/`、`__pycache__/`、各类 `*_results.json`。需要时从 APK 用 jadx 重新反编译即可。

---

## 记录规范（后续必须遵守）

1. **每次协议测试**：在 `03_TEST_LOG.md` 追加一节（时间、目的、请求、响应、结论）。  
2. **每个新结论**：同步更新 `04_FINDINGS.md` 与相关专题文档。  
3. **账号状态变化**（昵称/VIP/实名等）：更新 `05_ACCOUNT.md` + `login_session.json`。  
4. **新工具脚本**：在 `06_TOOLS.md` 登记。  
5. **敏感凭据**：可写在 `05_ACCOUNT.md`（CTF 本地用途），勿提交到公共仓库。

---

## 当前状态一句话

- 静态鉴权已完整还原；客户端签名可本地伪造。  
- 测试号 `19122614669` 已能协议登录；密码已设置。  
- **关键洞：`SigninOneKeyLogin1` 仅需手机号即可登录。**  
- 改昵称接口已定位（`resetNew`），当前被 **未实名** 拦截；用户将自行实名。  
- `user_role` / `vip` / `svip` 目前无法通过客户端直接改成目标值。  
- 实名绕过：`Describe/Save` 回显恒 `T` 但**未改服务端实名状态**；协议级伪造实名**尚未打通**（见 08 / F-015）。  
- 游客能力：见 **09**；L1 未实名可 follow；改资料/提现 403；大量读接口 L0 也开放。  
- **协议客户端已落地**：`bbw_protocol` + CLI，`call` 覆盖 402 actions；见 **10**。  
- **原生能力集成面已落地**：`adapters` + `bbw_web` BFF（IM 凭证 / 支付下单 / 刷脸编排）；见 **12**。  
- **协议核已跟版 v154**（`xbly.apk`）：`VERSION_CODE=154`，新增 `Id2MetaVerifyRequest`；见 **13** / T13。
