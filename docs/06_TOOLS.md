# 06 · 工具与命令

**最后更新：** 2026-07-15

## 1. 主入口

在**仓库根目录**执行：

```powershell
# 协议核
python -m bbw_protocol.cli login --phone PHONE --password PASS
python -m bbw_protocol.cli whoami
python -m bbw_protocol.cli bootstrap
python -m bbw_protocol.cli native-status
python -m bbw_protocol.cli im-tim
python -m bbw_protocol.cli call getGiftList
python -m bbw_protocol.cli repl

# 多用户 Web
python -m bbw_web --port 8765
```

| 模块 | 说明 |
|---|---|
| `bbw_protocol/` | 协议客户端 + adapters + CLI |
| `bbw_web/` | SessionStore + BFF + static UI |

## 2. 可选 tools/ 脚本

历史探测脚本，功能多数已被 `bbw_protocol` 覆盖：

| 脚本 | 用途 |
|---|---|
| `tools/bbw_client.py` | 早期签名 / UserSig 模板 |
| `tools/auth_flow.py` | 登录/短信/改密探测 |
| `tools/profile_edit.py` | 改昵称/角色探测 |
| `tools/rp_verify_probe.py` | 实名接口探测 |
| `tools/guest_gates_scan.py` | 门禁文案扫描（需 jadx_out） |
| `tools/guest_capability_probe.py` | L0/L1 批测 |
| `tools/enum_*.py` | 生成 catalog（需源码树） |
| `tools/coverage_report.py` | 覆盖率摘要 |

```powershell
python tools/bbw_client.py demo
python tools/auth_flow.py probe
```

## 3. 本地文件（不入库）

| 路径 | 说明 |
|---|---|
| `session.json` | CLI 会话 |
| `sessions/` | Web 多用户协议会话 |
| `*.apk` | 原始安装包 |
| `jadx_out/` | 反编译输出（可删可再生） |

## 4. 文档与 catalog

- 文档：`docs/*.md`
- Action 目录：`docs/api_catalog.json`
