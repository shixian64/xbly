# bbw_protocol — 贝贝屋协议客户端

把 APK 的 HTTP 业务面封装成 Python，使大部分功能可在无 UI 情况下调用。

## 能力范围

| 可协议化 | 说明 |
|---|---|
| ✅ 登录/改密/短信/一键登录 | `auth` |
| ✅ 公开内容读取 | 礼物/推荐/广告/敏感词… |
| ✅ 社交 | follow/粉丝/黑名单/举报… |
| ✅ 资料 | 用户信息/改昵称/隐私… |
| ✅ 经济 | VIP/金币/送礼/提现下单参数 |
| ✅ 房间/匹配/IM 辅助 HTTP | token 获取、列表、redis 缓存接口 |
| ✅ **action 名称面 + 调用器** | 默认 action 用 `app.call`；特殊租户/URL/Redis/multipart 用对应 `call_*` |
| ⚠️ 腾讯/融云 **实时长连接** | `adapters.im` 出凭证；收发需 TIM/融云 SDK（见 `bbw_web`） |
| ⚠️ 阿里云刷脸 | `adapters.face` 编排 Init/Describe；活体 metaInfo 仍靠 ZIM |
| ⚠️ 支付收银台 | `adapters.pay` 规范 order_params；收银靠微信/支付宝官方 |

完整 action 目录：`../docs/api_catalog.json`（v154 活跃 **398**；历史并集 **405**，含 7 个已下线小说 action）。

原生边车：`adapters/` + Web BFF：`../bbw_web/`。

## 安装 / 运行

无需第三方依赖（stdlib only）。

```powershell
cd <repo-root>

# CLI
python -m bbw_protocol.cli whoami
python -m bbw_protocol.cli login --phone 19122614669 --password "YOUR_PASS"
python -m bbw_protocol.cli bootstrap
python -m bbw_protocol.cli gifts
python -m bbw_protocol.cli follow 1
python -m bbw_protocol.cli me
python -m bbw_protocol.cli nick Vom
python -m bbw_protocol.cli call getGiftList
python -m bbw_protocol.cli call follow me=726285 you=1 quietly_follow=1
python -m bbw_protocol.cli actions --cat social
python -m bbw_protocol.cli repl
```

会话默认保存在 `session.json`。

## Python API

```python
from bbw_protocol import BeibeiwuApp

app = BeibeiwuApp.load()
app.auth.login_password("19122614669", "password")
app.save()

print(app.whoami())
print(app.content.gift_list().raw[:200])
print(app.social.follow("12345").message)
print(app.profile.reset_nickname("Vom").message)

# 任意接口（协议逃生舱）
app.call("getRoomTop")
app.call_redis("getUserRoomInfo", uid=app.session.uid)
app.im.local_user_sig()  # 本地腾讯 IM UserSig

# 原生能力适配（凭证 / 下单 / 刷脸编排）
print(app.native.im.tim_login_payload())
print(app.native.pay.prepare_coin_wechat("1").to_dict())
print(app.native.face.status_hint())
```

## 原生能力 CLI（协议核内）

```powershell
python -m bbw_protocol.cli native-status
python -m bbw_protocol.cli im-tim --prefer local
python -m bbw_protocol.cli im-rong
python -m bbw_protocol.cli pay-coin --channel wechat --coin-id 1
python -m bbw_protocol.cli pay-vip --channel wechat --level vip --vipid 5
python -m bbw_protocol.cli face-status
```

**Web 多用户不在本包**：见独立模块 `../bbw_web/`（`python -m bbw_web`）。  
协议核不依赖 Cookie / web_sid；CLI 与 Web 可同时存在、互不耦合。

## 模块结构

```
bbw_protocol/          # 协议核（无 Web）
  sign.py session.py client.py app.py cli.py
  device.py heartbeat.py   # 单实例设备字段 / 心跳
  modules/  adapters/
bbw_web/               # Web 层（多用户）—— 独立包
  store.py bff_server.py static/
```

## 与正常 App 的对应关系

| App 行为 | 协议 |
|---|---|
| 打开 App 拉配置 | `bootstrap()` / content.* |
| 账号密码登录 | `auth.login_password` |
| 短信登录 | `auth.sms_send` + `sms_login` |
| 关注 | `social.follow` |
| 改昵称 | `profile.reset_nickname`（需实名） |
| 送礼物 | `economy.send_gift*`（需余额） |
| 开房间 | `room.create`（服务端条件） |
| 进 IM | `im.tencent_sign` / `local_user_sig` + 外部 IM SDK |
| 未封装的按钮 | `call("ExactDoName", **formFields)` |

## 设计说明

1. **Header** 与 APK `OkHttpInstance.startHttp` 一致。  
2. **通用 `call`** 保证目录内 action 都可达，不要求每个都先写死方法。  
3. 常用路径提供语义化 API，便于脚本化「像人一样用」。  
4. 错误码统一到 `ApiResult`（json/text/empty/700/403…）。

## 限制（诚实说明）

- 不能 1:1 复刻全部原生 SDK 体验（人脸、支付 UI、IM 实时）。  
- 部分接口参数需对照 jadx 或抓包补全；未知参数用 `call` 试验。  
- 服务端门禁（实名/余额/礼仪分）协议层同样生效。  
- 仅供 CTF/授权安全研究。
