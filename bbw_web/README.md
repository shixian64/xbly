# bbw_web — 多用户 Web 层（与协议核隔离）

```
┌──────────────────────────────────────────┐
│  bbw_web（本目录）                         │
│  · store.py      多用户 SessionStore       │
│  · bff_server.py HTTP BFF + Cookie        │
│  · static/       浏览器 UI                 │
└──────────────────┬───────────────────────┘
                   │ 仅 import 调用
                   ▼
┌──────────────────────────────────────────┐
│  bbw_protocol（协议核，无 Web 概念）        │
│  · BeibeiwuApp / Session / adapters      │
│  · device / heartbeat（单实例工具）        │
│  · CLI 仍可单独用，不依赖 bbw_web          │
└──────────────────────────────────────────┘
```

**原则**

- 协议核 **不知道** Cookie、web_sid、多租户。
- Web 层为每个浏览器会话持有 **独立** `BeibeiwuApp` + 可选 `Heartbeat`。
- TIM SECRETKEY / 登录 token 只在 BFF 进程；浏览器只有 `web_sid` + UserSig。

## 启动

```powershell
cd <repo-root>
python -m bbw_web --port 8765
# http://127.0.0.1:8765/
```

## 多用户怎么用

1. 浏览器 A 登录手机号 1 → Cookie `bbw_sid=S1`，后端 `BeibeiwuApp` 实例 1。  
2. 浏览器 B（或无痕）登录手机号 2 → `bbw_sid=S2`，实例 2。  
3. `GET /api/sessions` 可看当前内存中全部 web 会话。  
4. 协议会话落盘：`analysis/sessions/{uid}.json`（勿提交 git）。

认证方式（任选）：

- Cookie `bbw_sid`（页面自动）
- Header `X-BBW-SID`
- Query `?sid=`

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | `{phone,password,mode?,label?}` → 设 Cookie |
| POST | `/api/auth/logout` | 销毁 web 会话 |
| POST | `/api/auth/guest` | 空 web 会话 |
| GET | `/api/me` | 当前用户 |
| GET | `/api/sessions` | 全部 web 会话（本机） |
| GET | `/api/bootstrap` | IM/pay 启动包 |
| GET | `/api/im/tim` | TIM 凭证 |
| POST | `/api/call` | `{action, params}` 任意协议 action |
| POST | `/api/heartbeat/*` | start/stop/once |
| POST | `/api/pay/*` `/api/face/*` | 下单 / 刷脸编排 |

## 与 CLI 的关系

```powershell
# 协议核 CLI（单 session.json）—— 不经过 Web
python -m bbw_protocol.cli login --phone ... --password ...
python -m bbw_protocol.cli whoami

# Web 多用户 —— 只起 BFF
python -m bbw_web
```

两套会话文件：

| 文件 | 归属 |
|---|---|
| `session.json` | 协议 CLI 默认 |
| `analysis/sessions/{uid}.json` | Web 多用户落盘 |

## 安全

- 默认绑定 `127.0.0.1`；勿对公网裸奔。  
- `/api/sessions` 暴露本机所有登录态，仅适合本地 CTF。  
- 密码可写入 `sessions/*.json`（与 CLI 相同本地便利）；生产应改加密存储。
