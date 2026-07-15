# 06 · 工具脚本

**最后更新：** 2026-07-15

## 1. 工具一览

**日常主入口：** `python -m bbw_protocol.cli` · `python -m bbw_web`

| 脚本 | 用途 | 备注 |
|---|---|---|
| **`bbw_protocol/`** | 协议客户端 + CLI | **主入口** |
| **`bbw_web/`** | 多用户 BFF + UI | 与核隔离 |
| `bbw_client.py` | 早期签名/UserSig 模板 | 已被 protocol 覆盖 |
| `auth_flow.py` | 登录/短信/改密探测 | 可选 |
| `profile_edit.py` | 改昵称/角色探测 | 可选 |
| `rp_verify_probe.py` | 实名接口探测 | 可选 |
| `guest_gates_scan.py` | 门禁文案静态扫 | 需 `jadx_out` |
| `guest_capability_probe.py` | L0/L1 批测 | 可选 |
| `enum_all_apis.py` / `enum_short.py` / `enum_apis.py` | 生成 catalog | 需源码树 |
| `coverage_report.py` | 覆盖率摘要 | 可选 |

本地不入库：`session.json`、`sessions/`、`jadx_out/`、`*.apk`（见根目录 `.gitignore`）。

## 2. bbw_client.py

```powershell
cd D:\project\AI\bbw\analysis

python bbw_client.py demo
python bbw_client.py list
python bbw_client.py sign --uid 726285 --token "LOGIN_ID|HEX"
python bbw_client.py usersig --uid 726285
python bbw_client.py url --action SvipTry --uid 726285 --token "LOGIN_ID|HEX"
```

实现算法：

- `get_sign_token` / `get_expire_token` / `get_unique_login_token`
- `get_author_signature`
- `gen_tls_user_sig`

## 3. auth_flow.py

```powershell
python auth_flow.py probe          # 批量探测
python auth_flow.py login          # signin0
python auth_flow.py sms            # 触发短信
python auth_flow.py verify <code>  # 校验验证码并尝试登录/改密
python auth_flow.py setpass        # findpassword + signin0
python auth_flow.py onekey         # SigninOneKeyLogin1
```

内置手机号/密码常量在文件顶部（测试号）。

## 4. profile_edit.py

```powershell
python profile_edit.py
```

流程：signin0 → resetNum → resetNew(Vom) → VIP/角色探测 → 再登录打印前后对比。

## 5. rp_verify_probe.py

```powershell
python rp_verify_probe.py
```

探测 `SaveRPVerifyInfo` / `DescribeFaceVerify0` / `InitFaceVerify0` / `checkAge`，并复查 `rp_verify_time` 与 `resetNew`。

## 6. guest 能力工具

```powershell
python guest_gates_scan.py
python guest_capability_probe.py
# 输出 guest_capability_results.json
```

## 7. bbw_protocol（主协议客户端）

```powershell
cd D:\project\AI\bbw\analysis
python -m bbw_protocol.cli --help
python -m bbw_protocol.cli login --phone 19122614669 --password "***"
python -m bbw_protocol.cli repl
python -m bbw_protocol.smoke_test
```

详见 `bbw_protocol/README.md` 与 `10_PROTOCOL_CLIENT.md`。

## 8. jadx

```powershell
$env:JAVA_HOME = 'D:\tools\jadx\win\jadx-gui-1.5.5-with-jre-win\jre'
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
& 'D:\tools\jadx\jadx-1.5.3\bin\jadx.bat' `
  -d 'D:\project\AI\bbw\jadx_out' `
  --show-bad-code --deobf `
  'D:\project\AI\bbw\beibeiwu.apk'
```

GUI：桌面快捷方式 → `jadx-gui-1.5.5.exe`。

## 9. 会话文件

| 文件 | 说明 |
|---|---|
| `session.json` | **bbw_protocol 主会话** |
| `login_session.json` | 早期登录快照 |
| `guest_capability_results.json` | 游客能力批测原始结果 |
| `api_catalog.json` | 全量 action 目录 |

## 10. adapters / bbw_web（原生 IM·刷脸·支付）

```powershell
cd D:\project\AI\bbw\analysis
python -m bbw_protocol.cli native-status
python -m bbw_protocol.cli im-tim --prefer local
python -m bbw_protocol.cli im-rong
python -m bbw_protocol.cli pay-coin --channel wechat --coin-id 1
python -m bbw_protocol.cli face-status
python -m bbw_web --port 8765
# 浏览器 http://127.0.0.1:8765/
```

说明见 [12_NATIVE_INTEGRATION.md](./12_NATIVE_INTEGRATION.md)、[bbw_web/README.md](./bbw_web/README.md)。

## 11. 新增工具登记模板

新增脚本时在本表追加一行，并在 `README.md` 产物表同步。
