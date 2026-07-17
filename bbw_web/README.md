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

### 手机访问与录音

- 默认地址 `http://127.0.0.1:8765/` 仅供运行 BFF 的电脑本机访问；手机自己的 `127.0.0.1` 并不是电脑。
- 手机浏览器使用麦克风必须处于浏览器认可的 HTTPS 安全上下文，并使用手机信任的证书；普通局域网 HTTP 页面只能发送已有文件，不能录音。
- 推荐由同机 HTTPS 反向代理转发到仍绑定 `127.0.0.1` 的 BFF，并同时启用 `python -m bbw_web --secure-cookie`。
- `--secure-cookie` 只为 Cookie 添加 `Secure` 属性，不会让 Python BFF 自己提供 TLS。
- 仅在确需可信局域网访问时使用 `python -m bbw_web --host 0.0.0.0`，并通过防火墙限制来源，禁止直接暴露到公网。

## 信息架构

移动端一级导航对齐当前 `xbly.apk` v154：

| 主导航 | Web 能力 |
|---|---|
| **身边** | 在线用户流、资料入口、聊天入口、匹配与话题快捷入口 |
| **消息** | 聚焦历史会话列表、未读数与会话双栏；通讯录可从关系中心或消息空态进入；受信任 TIM SDK 可接实时 C2C，并支持文字、图片/GIF、语音、视频、文件、表情包和 5 秒闪图 |
| **匹配** | 在线/同城、APK 同款性别与属性筛选、漂流瓶、在线列表、约会、匹配卡 |
| **动态** | 推荐/附近/最新/招募令/关注动态流，资料弹窗查看指定用户公开动态，点赞评论，以及本人动态的可见范围、删除和个人主页置顶 |
| **我的** | 好友/关注/粉丝/访客统计、资料、改昵称、实名说明、礼仪分、推荐码、退出 |

PC 侧栏不再使用“更多服务”兜底分组，而是按所属模块展示入口：语音房不再占用侧栏二级入口，改为在匹配页顶部通过“匹配 / 语音房”标签切换下方功能区；关系中心、钱包与会员、任务与奖励仍归入我的。关系中心统一承载通讯录、好友申请、关注、粉丝、访客和黑名单，访客内部保留“谁看过我 / 我看过谁”双列表；旧的 `#/room`、`#/friends`、`#/visitors` 地址会兼容跳转到对应标签。手机端仍保持五项底栏。

## 明确边界

| 类型 | Web 状态 |
|---|---|
| HTTP 业务能力 | 通过语义 BFF 覆盖主要流程；冷门 action 仅 Lab 可调 |
| IM 实时体验 | 产品模式仅接受服务端签发 UserSig；固定版本官方 SDK 已 vendor 在 `static/vendor/tim-js.js`（tim-js-sdk@2.27.6）；进入消息页自动连接；SDK 不可用时的 Web REST 降级仅支持文本消息；视频上传按该版本能力限制为 MP4/MOV，浏览器无法读取部分 MOV 元数据时仍会尝试交给 TIM 上传；图片支持 JPEG/PNG/GIF/BMP/WebP；图片、音频、视频加载失败和本地媒体发送失败均提供重试 |
| 5 秒闪图 | 支持按住查看、松手关闭并最长展示 5 秒；受浏览器权限模型限制，Web 无法阻止操作系统截图或录屏 |
| 刷脸实名 | Web 展示状态并引导官方 App；活体仍依赖阿里云 ZIM |
| 微信/支付宝 | 可校验并生成订单参数；收银、回调和到账仍依赖官方商户配置 |
| RTC/语音房 | 有限支持：旧版推荐榜单、RoomKit 独立登录与 APK 原生房间列表读取；Web 尚未接入实时音频 |

### 语音房分层

- 当前 `/api/room/top` 对接 APK 的旧版 `getRoomTop` 推荐入口；上游返回 `false` 时按“暂无推荐房间”处理，不再展示为操作失败。
- 旧版 `createRoom0` 返回 `no/false` 时，会明确展示为账号资格或服务端业务开关导致的建房不可用，不把具体原因误判为实名限制。
- APK v154 的完整房间列表属于独立 RoomKit 链路：BFF 通过房间服务登录取得独立 `Authorization`，再请求 `GET /mic/room/list?page=1&size=10&type=1`。它不复用现有微擎 `AUTHOR-TOKEN`。
- 页面上的“读取 APK 房间列表”会显式触发该链路；凭证仅缓存在当前 BFF 用户的内存会话中，响应只返回房间 DTO 和非敏感连接状态。
- 即使房间列表可读，浏览器进入房间收听、上麦和通话仍需要可用的融云 Web 实时音频能力。

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
