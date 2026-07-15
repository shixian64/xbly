# bbw_web — 像使用 App 一样的 Web 客户端

多用户 Web 壳：**登录 → 首页 / 匹配 / 社交 / 消息 / 我的**，走与 APK 相同的 `bbw_protocol` HTTP 业务。

```
浏览器 SPA
   │ Cookie bbw_sid
   ▼
bff_server.py          ← 本包
   │ 每用户独立 BeibeiwuApp + 心跳
   ▼
bbw_protocol（协议核）
   ▼
banghua 后端
```

## 启动

```powershell
cd <repo-root>
python -m bbw_web --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

## 界面能力（对齐 App 主路径）

| Tab | 功能 |
|---|---|
| **首页** | 冷启动推荐 / 礼物列表 / 在线心跳状态 |
| **匹配** | 匹配次数与卡、在线/同城匹配、漂流瓶、乐园币买卡 |
| **社交** | 关注 / 粉丝 / 好友申请、查资料、关注用户 |
| **消息** | 拉取 TIM UserSig，尝试 CDN 登录发消息（完整体验需 TIM SDK） |
| **我的** | 资料、改昵称、钱包/VIP 下单、任务领取、语音房、实名说明、任意 `do=` |

登录方式：密码 · 一键（弱接口）· 短信。

## 与「调试台」的区别

- 产品化底部导航与移动端布局，而不是 JSON 调试面板为主。  
- BFF 提供 `/api/app/home`、`/api/match/*`、`/api/social/*` 等语义 API。  
- 仍保留 `/api/call` 逃生舱给高级用户。

## 能力边界（与 APK）

| 能力 | Web 现状 |
|---|---|
| 业务 HTTP（登录、匹配、关注、任务…） | ✅ 同协议 |
| IM 实时 | ⚠️ 有凭证；收发依赖 TIM Web SDK |
| 刷脸实名 | ⚠️ 建议官方 App 完成；Web 仅 HTTP 编排 |
| 微信支付到账 | ⚠️ 可下单；收银受官方包名/商户限制 |

## 多用户

每个浏览器 Cookie `bbw_sid` 对应独立协议会话；无痕/多浏览器可同时登录不同账号。

## 文件

| 文件 | 说明 |
|---|---|
| `bff_server.py` | App BFF |
| `store.py` | 多用户 SessionStore |
| `static/index.html` | App 壳 |
| `static/app.css` | 移动端样式 |
| `static/app.js` | 前端逻辑 |
