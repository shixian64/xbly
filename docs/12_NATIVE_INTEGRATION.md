# 12 · 原生能力集成（IM / 刷脸 / RoomKit）

**最后更新：** 2026-07-17
**代码：** `bbw_protocol/adapters/`（核） + `bbw_web/`（多用户 Web，独立模块）  
**前提：** HTTP 业务核 `bbw_protocol` 保留非商业协议能力；购买、充值、会员开通及相关订单 action 已在请求前统一停用。

**隔离：** 协议核无 Cookie/web_sid；多用户只在 `bbw_web.store`。

---

## 1. 问题与结论

| 缺口 | 能否「只靠协议」补全 | 能否集成 | 本仓库交付 |
|---|---|---|---|
| IM 实时长连接 | 否 | **能**（官方 TIM/融云 SDK） | 凭证适配 + BFF + Web 接入 |
| 刷脸实名 | 否（活体） | **能**（阿里云 ZIM + 真身份） | Init/Describe 编排 |
| RoomKit 房间列表 | 否（独立会话） | **能** | 独立登录、授权状态与列表适配 |
| 服务端会员权益 | **能读取** | 无需客户端 SDK | `vip` / `svip` 会话持久化和只读展示 |

**原则：** 协议出凭证或会话，SDK 做原生动作；不伪造 UserSig 校验绕过，不伪造刷脸通过。会员有效期完全以服务端下发为准。

---

## 2. 架构

```
Web UI (bbw_web/static)
        │
        ▼
BFF (bbw_web/bff_server.py)     ← 敏感凭证仅在服务端
        │
        ▼
NativeBundle (adapters)
  ├── im.py       TimCredentials / RongCredentials
  ├── face.py     FaceSession pipeline
  └── roomkit.py  RoomKit 独立会话
        │
        ▼
BeibeiwuApp HTTP core → banghua 后端
```

---

## 3. 使用

### 3.1 Python

```python
from bbw_protocol import BeibeiwuApp

app = BeibeiwuApp.load()

print(app.native.status())
print(app.native.im.tim_login_payload(prefer="local"))
print(app.native.im.rong_register().to_dict())
print(app.native.face.status_hint())
print(app.native.roomkit.public_status())
print({"vip": app.session.vip, "svip": app.session.svip})
```

### 3.2 CLI

```powershell
cd <repo-root>
python -m bbw_protocol.cli native-status
python -m bbw_protocol.cli im-tim --prefer local
python -m bbw_protocol.cli im-rong
python -m bbw_protocol.cli face-status
```

### 3.3 BFF + 浏览器

```powershell
python -m bbw_protocol.cli login --phone ... --password ...
python -m bbw_web --port 8765
# 打开 http://127.0.0.1:8765/
```

| API | 作用 |
|---|---|
| `GET /api/im/tim` | 产品模式下返回受控能力说明，不向浏览器暴露宽泛凭证 |
| `GET /api/im/rong` | 产品模式下继续禁止通用 IM 凭证下发 |
| `POST /api/match/voice/bootstrap` | 仅为一对一语音匹配下发当前用户的融云连接凭证 |
| `POST /api/match/voice/start` | 调用 APK 同款 `xiaobeiMatchNew(type=语音)` |
| `POST /api/match/voice/cancel` | 尝试移出语音匹配等待队列 |
| `POST /api/face/init` | 需 SDK `meta_info` |
| `GET /api/wallet` | 返回余额、礼物背包和服务端会员状态；会员字段只读 |

---

## 4. 各通道边界

### IM

- 本地 UserSig：`sign.gen_user_sig`，仅 CLI / 显式 Lab 研究使用；产品 Web 不隐式回退到本地签名。
- 服务端：`tximsign.php` 或登录响应提供 `userSign`；产品 Web 会把服务端 UserSig 下发给已登录浏览器。
- 实时收发：浏览器使用固定版本 TIM SDK 和上传插件发送文字及富媒体；该直连通道不经过 BFF 的逐对象私信鉴权。SDK 不可用时仅回退到经过 BFF 权限检查的 REST 文本发送。

### 刷脸

```
ZIM.metaInfo → face.start → certifyId → ZIM 活体 → face.describe → 重登看 rp_verify_time
```

假 certifyId 历史上 `message=T` 但 **`rp_verify_time` 不变**（见 08）。

### RoomKit

- RoomKit 使用独立授权，不复用主协议 `AUTHOR-TOKEN`。
- 浏览器只接收房间 DTO 与非敏感状态，不接收独立授权值。
- 房间列表可读不代表浏览器已经具备实时语音能力。

### 会员权益与商业能力

- 登录、资料刷新、会话持久化继续保留 `vip` / `svip` 字段。
- Web 的资产与权益页只展示服务端返回的会员状态，不修改有效期。
- 商业 action 由 `ProtocolClient` 在发起网络请求前返回 `COMMERCE_DISABLED`。
- 项目不包含支付适配器、收银参数生成、充币、会员开通、会员试用、余额兑换、礼物购买或匹配卡购买入口。

---

## 5. 文件清单

| 路径 | 说明 |
|---|---|
| `bbw_protocol/adapters/im.py` | TIM / 融云凭证 |
| `bbw_protocol/adapters/face.py` | 刷脸会话编排 |
| `bbw_protocol/adapters/roomkit.py` | RoomKit 独立会话与列表 |
| `bbw_protocol/adapters/bundle.py` | `NativeBundle` / `app.native` |
| `bbw_protocol/session.py` | 服务端会员字段持久化 |
| `bbw_web/bff_server.py` | stdlib BFF 与只读权益响应 |
| `bbw_web/static/` | 产品 UI |

---

## 6. 后续可选

1. 正式前端继续收敛 IM 实时能力和权限校验。
2. 阿里云 H5 实人页或引导「回 App 实名」。  
3. 为服务端会员权益补充明确的功能门禁说明。
4. 勿将 BFF 对公网直接暴露。
