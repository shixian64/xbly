# bbw_web — 产品化 Web 客户端

`bbw_web` 将 APK 的主要业务能力映射到浏览器，但不再把“协议可调用”描述成“所有 APK 体验都已完整复刻”。默认运行在**产品模式**；高风险协议调试能力必须显式开启 Lab。

```text
Browser SPA（五主导航 · PC 侧栏 / 手机底栏）
        │ HttpOnly + SameSite=Strict Cookie
        ▼
bbw_web BFF（语义 API、会话隔离、安全响应头）
        │
        ▼
bbw_protocol → banghua 后端
```

## 启动

```powershell
cd <repo-root>
python -m bbw_web --port 8765
# http://127.0.0.1:8765/
```

默认行为：

- Cookie-only 会话，不把 SID 写入 `localStorage`、URL 或 JSON。
- CORS 关闭；协议台、任意 action、浏览器会话枚举、弱一键登录关闭。
- Web session 仅保存在内存，不落盘保存密码或原始登录响应。
- 在线心跳跟随可见浏览器页面按需发送，不默认创建长期后台线程。

可选开发参数：

```powershell
# 仅本机授权研究时启用协议台；不要对公网开放
python -m bbw_web --enable-lab

# HTTPS 反代后使用
python -m bbw_web --secure-cookie

# 明确允许某个跨域来源（默认不需要）
python -m bbw_web --cors-origin http://127.0.0.1:3000

# 可选保存精简会话；不会保存 password/raw_user
python -m bbw_web --persist-sessions

# 可选由服务端维持心跳线程；默认由前端可见性驱动单次心跳
python -m bbw_web --auto-heartbeat
```

## 信息架构

移动端一级导航对齐当前 `xbly.apk` v154：

| 主导航 | Web 能力 |
|---|---|
| **身边** | 在线用户流、资料入口、聊天入口、匹配与话题快捷入口 |
| **消息** | 历史会话列表、未读数、会话双栏、通讯录/新朋友/访客快捷入口；受信任 TIM SDK 可接实时 C2C |
| **匹配** | 在线/同城、漂流瓶、在线列表、约会、匹配卡 |
| **动态** | 推荐、轮播、话题搜索与创建 |
| **我的** | 好友/关注/粉丝/访客统计、资料、改昵称、实名说明、礼仪分、推荐码、退出 |

好友列表与访客足迹是可直接访问的二级页面：好友按字母分组并可直接聊天；访客页包含“谁看过我 / 我看过谁”双列表。关注、粉丝、好友申请和黑名单使用各自语义正确的操作按钮。语音房间、钱包会员、成长任务继续作为二级能力，不与 APK 五个主 Tab 混成十个并列入口。

## 明确边界

| 类型 | Web 状态 |
|---|---|
| HTTP 业务能力 | 通过语义 BFF 覆盖主要流程；冷门 action 仅 Lab 可调 |
| IM 实时体验 | 产品模式仅接受服务端签发 UserSig；固定版本官方 SDK 已 vendor 在 `static/vendor/tim-js.js`（tim-js-sdk@2.27.6）；进入消息页自动连接 |
| 刷脸实名 | Web 展示状态并引导官方 App；活体仍依赖阿里云 ZIM |
| 微信/支付宝 | 可校验并生成订单参数；收银、回调和到账仍依赖官方商户配置 |
| RTC/语音房 | 可准备房间/Token 数据；实时音频仍需受控 RTC SDK |

## 验证

无需构建：

```powershell
python -m unittest discover -s tests -p "test*.py" -v
node --check bbw_web/static/app.js
```

主要文件：

- `bff_server.py`：BFF、安全边界与语义路由
- `store.py`：多用户内存会话、SID 旋转、可选精简持久化
- `normalize.py`：用户、礼物、轮播、话题、房间、歌曲、漂流瓶等 DTO
- `static/`：五主导航 SPA 与响应式 UI
