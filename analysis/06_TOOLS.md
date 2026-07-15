# 06 · 工具脚本

**最后更新：** 2026-07-15

## 1. 工具一览

| 脚本 | 用途 |
|---|---|
| `bbw_client.py` | 客户端签名算法、UserSig、curl 模板（默认不发网） |
| `auth_flow.py` | 登录/注册/短信/改密协议探测 |
| `profile_edit.py` | 登录后改昵称/角色/VIP 探测 |
| `rp_verify_probe.py` | 实名相关接口探测（Init/Describe/Save/人工） |
| `guest_gates_scan.py` | 静态扫描登录/实名/VIP 门禁文案与条件 |
| `guest_capability_probe.py` | L0/L1 协议能力批测 → JSON 矩阵 |
| `enum_all_apis.py` | 生成 api_catalog.json（402 actions） |
| **`bbw_protocol/`** | **完整协议客户端 + CLI（主入口）** |
| `enum_short.py` | 从源码枚举 do= / startHttp action |
| `enum_apis.py` | 全量枚举（可能较慢） |

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

## 10. 新增工具登记模板

新增脚本时在本表追加一行，并在 `README.md` 产物表同步。
