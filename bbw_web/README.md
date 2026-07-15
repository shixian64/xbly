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
- CORS 关闭；协议台、任意 action、会话列表、弱一键登录关闭。
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
| **身边** | 推荐内容、轮播、人气礼物、常用服务 |
| **消息** | TIM/融云凭证状态；宿主提供受信任 SDK 时支持基础 C2C 文本 |
| **匹配** | 在线/同城、漂流瓶、在线列表、约会、匹配卡 |
| **动态** | 推荐、轮播、话题搜索与创建 |
| **我的** | 资料、改昵称、实名说明、礼仪分、推荐码、退出 |

社交关系、语音房间、钱包会员、成长任务作为二级能力，不再与 APK 五个主 Tab 混成十个并列入口。

## 明确边界

| 类型 | Web 状态 |
|---|---|
| HTTP 业务能力 | 通过语义 BFF 覆盖主要流程；冷门 action 仅 Lab 可调 |
| IM 实时体验 | 产品模式仅接受服务端签发 UserSig、不回退本地签名；不动态加载 `latest` CDN；正式接入需固定版本官方 SDK |
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
