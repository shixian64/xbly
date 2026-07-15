# 10 · 全功能协议客户端落地

**最后更新：** 2026-07-15  
**包路径：** `bbw_protocol/`  
**Action 目录：** `docs/api_catalog.json`（v154 活跃 **398**；历史并集 **405**）

---

## 1. 目标与完成度

| 目标 | 状态 |
|---|---|
| 枚举客户端 HTTP 功能面 | ✅ v154 活跃 398 actions / 328 full URLs；catalog 另保留 7 个 v148 下线 action |
| 统一签名与会话 | ✅ `sign.py` + `session.json` |
| 语义化业务 API | ✅ auth/social/profile/content/economy/room/match/im/misc |
| **action 名称面与调用器** | ✅ 默认 `app.call`；另有 `call_i888` / Redis / URL / multipart |
| CLI + REPL | ✅ `python -m bbw_protocol.cli` |
| 冒烟测试 | ✅ 登录/礼物/推荐/关注/门禁行为符合预期 |
| 原生 SDK 1:1（刷脸/支付 UI/IM 长连接） | ⚠️ `adapters` + `bbw_web` 集成面已加；活体/收银/长连接仍靠官方 SDK |

**结论：**  
在「HTTP 业务 = App 主路径」前提下，**已经可以像用 APK 一样通过协议完成绝大多数功能**；剩余缺口集中在活体 SDK、支付收银台 UI、IM 实时通道。

---

## 2. 使用方式（像正常用 App）

### 2.1 快速开始

```powershell
cd <repo-root>

# 登录（保存 session.json）
python -m bbw_protocol.cli login --phone 19122614669 --password "YOUR_PASSWORD"

# 查看身份
python -m bbw_protocol.cli whoami

# 冷启动一批（广告/礼物/推荐/敏感词/心跳/资料）
python -m bbw_protocol.cli bootstrap

# 常用操作
python -m bbw_protocol.cli gifts
python -m bbw_protocol.cli follow 1
python -m bbw_protocol.cli me
python -m bbw_protocol.cli nick Vom          # 未实名会 403
python -m bbw_protocol.cli online

# 任意接口（全量覆盖）
python -m bbw_protocol.cli call getRoomTop
python -m bbw_protocol.cli call getFollowList id=726285
python -m bbw_protocol.cli call-redis getUserRoomInfo uid=726285
python -m bbw_protocol.cli actions --cat social

# 交互
python -m bbw_protocol.cli repl
```

### 2.2 Python

```python
from bbw_protocol import BeibeiwuApp

app = BeibeiwuApp.load()
app.auth.login_password("19122614669", "YOUR_PASSWORD")
app.save()

app.content.gift_list()
app.social.follow("123")
app.profile.get_me()
app.economy.svip_try()
app.room.create()
app.im.local_user_sig()          # 腾讯 IM
app.call("AnyDoAction", foo="bar")  # 逃生舱
```

---

## 3. 架构

```
BeibeiwuApp
├── session (uid/token/user fields → session.json)
├── client  (URL + SIGN/EXPIRE + form/multipart + ApiResult)
├── auth / content / social / profile
├── economy / room / match / im / misc
└── call()  → 任意 short action（≈ startHttp）
```

与 APK 对应：

| APK | 协议层 |
|---|---|
| `OkHttpInstance.startHttp(map, action)` | `client.call(action, map)` |
| `getUrl(action)` | `client.url(action)` |
| Header 三件套 | `sign.auth_headers` |
| SharedPreferences token | `session.json` |
| 各 Activity 业务 | modules/* 语义方法 |
| 未封装按钮 | `call("ExactName")` |

---

## 4. 功能覆盖矩阵（协议）

### 4.1 已封装模块

| 模块 | 代表能力 |
|---|---|
| auth | 密码/一键/短信/改密/登出/唯一登录 |
| content | 礼物、推荐、广告、敏感词、版本、话题、testField |
| social | 关注/取关/粉丝/好友申请/黑名单/点赞/举报 |
| profile | 用户资料、改昵称、reset 系列、隐私、礼仪分 |
| economy | 礼物收发、VIP 试用/兑换/赠送、充值下单、提现 |
| room | 建房、房间设置、声网 token、点歌、redis 房间信息 |
| match | 匹配移除、在线匹配、漂流瓶、约会 |
| im | 腾讯 sign、本地 UserSig、融云注册、闪照/表情 HTTP |
| misc | 心跳、前后台、实名相关 HTTP、通用 raw |

### 4.2 全量 action（未逐个写方法）

`api_catalog.json` 分类：

| 分类 | 目录条目 |
|---|---:|
| social | 71 |
| profile | 62 |
| content | 34 |
| match | 28 |
| economy | 28 |
| room | 26 |
| auth | 24 |
| im | 8 |
| other | 121 |
| 未分类历史下线项 | 3 |
| **catalog 历史并集** | **405** |

其中 v154 当前活跃 **398**；7 个小说 action 仅为 v148 历史留档。`signin0` 与
`SigninOneKeyLogin1` 由登录流程运行时拼接 `do=`，旧的明文 URL/startHttp 扫描会漏掉，
现已作为 `dynamic_do_actions` 补录。

默认 `i=999999&m=socialchat`、表单编码的 action 可通过：

```text
python -m bbw_protocol.cli call <ActionName> k=v k2=v2
```

需要 `i=888`、Redis、absolute URL 或 multipart/file 的接口，应分别使用
`call_i888`、`call_redis`、`call_url`、`call_multipart`；action 名称已枚举不等于
参数、租户和编码方式已自动推断。

### 4.3 无法单靠本库完成的部分

| 能力 | 原因 | 补齐方式 |
|---|---|---|
| 人脸核身通过 | 阿里云 ZIM 原生 | 真机 App / 官方 SDK |
| 微信支付/支付宝确认 | 需手机支付 App | 协议只拿到 orderString/prepay |
| 腾讯/融云实时消息流 | 长连接 SDK | 用 userSign/token 接官方 SDK 或 REST |
| 一键本机号码登录 | 运营商 SDK | 短信/密码替代 |
| 热修复补丁逻辑 | Sophix | 非业务必须 |

---

## 5. 冒烟结果（2026-07-15）

账号 `19122614669` / uid `726285`：

| 步骤 | 结果 |
|---|---|
| login_password | ✅ 200 登录成功，session 持久化 |
| gifts / recommend / ads | ✅ |
| online 心跳 | ✅ 有回包 |
| follow | 400 已关注（业务正常） |
| nick Vom | 403 未实名（门禁正常） |
| withdraw | 403 未实名（门禁正常） |
| room create | no（条件不足） |
| local UserSig | ✅ |
| catalog size | 405（其中 v154 活跃 398） |

---

## 6. 与「正常使用 APK」的操作对照

| 你在 App 里点… | 协议命令 |
|---|---|
| 打开 App | `bootstrap` |
| 密码登录 | `login --phone --password` |
| 刷推荐/礼物墙 | `recommend` / `gifts` |
| 关注某人 | `follow <uid>` |
| 看自己主页 | `me` |
| 改昵称 | `nick <name>` |
| 送礼物 | `call sendGift2 ...`（参数按 jadx） |
| 开语音房 | `room-create` |
| 进 IM | `usersig` / `txim-sign` + IM SDK |
| 任意冷门功能 | `call <DoName> k=v` + 查 `actions --cat` |

---

## 7. 后续增强（可选）

1. 从 jadx 自动抽取每个方法的 FormBody 字段 → 生成 typed stubs  
2. 接腾讯 IM REST 做收发消息  
3. 支付回调模拟（仅测试环境）  
4. 异步/重试/限速中间件  
5. 把 guest 矩阵接入 `bbw_protocol` 自检  

---

## 8. 文档同步

- 包说明：`bbw_protocol/README.md`  
- 本文：`10_PROTOCOL_CLIENT.md`  
- 目录：`api_catalog.json`  
- 会话：`session.json`（登录后生成）  
- 测试日志 / Findings / 总索引已更新  
