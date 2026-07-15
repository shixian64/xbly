# 12 · 原生能力集成（IM / 刷脸 / 支付）

**最后更新：** 2026-07-15  
**代码：** `bbw_protocol/adapters/`（核） + `bbw_web/`（多用户 Web，独立模块）  
**前提：** HTTP 业务核 `bbw_protocol` 的 catalog 覆盖 v154 活跃 398 actions（历史并集 405）；本页只补 **SDK 边车**。

**隔离：** 协议核无 Cookie/web_sid；多用户只在 `bbw_web.store`。

---

## 1. 问题与结论

| 缺口 | 能否「只靠协议」补全 | 能否集成 | 本仓库交付 |
|---|---|---|---|
| IM 实时长连接 | 否 | **能**（官方 TIM/融云 SDK） | 凭证适配 + BFF + 演示页 |
| 刷脸实名 | 否（活体） | **能**（阿里云 ZIM + 真身份） | Init/Describe 编排 |
| 支付确认 | 否（收银/商户） | **能**（微信/支付宝 SDK） | order_params 规范化 |

**原则：** 协议出凭证/订单/会话；SDK 做原生动作；**不伪造** UserSig 校验绕过、不伪造刷脸通过、不伪造支付回调。

---

## 2. 架构

```
Web UI (bbw_web/static)
        │
        ▼
BFF (bbw_web/bff_server.py)     ← TXIM_SECRETKEY 仅在此
        │
        ▼
NativeBundle (adapters)
  ├── im.py    TimCredentials / RongCredentials
  ├── face.py  FaceSession pipeline
  └── pay.py   PayPrepareResult
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
# 或: from bbw_protocol.adapters import NativeBundle; n = NativeBundle(app)

print(app.native.status())
print(app.native.im.tim_login_payload(prefer="local"))
print(app.native.im.rong_register().to_dict())
print(app.native.pay.prepare_coin_wechat("1").to_dict())
print(app.native.face.status_hint())
```

### 3.2 CLI

```powershell
cd <repo-root>
python -m bbw_protocol.cli native-status
python -m bbw_protocol.cli im-tim --prefer local
python -m bbw_protocol.cli im-rong
python -m bbw_protocol.cli pay-coin --channel wechat --coin-id 1
python -m bbw_protocol.cli pay-vip --channel wechat --level vip --vipid 5
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
| `GET /api/im/tim` | `{SDKAppID, userID, userSig}` |
| `GET /api/im/rong` | 融云 token |
| `POST /api/pay/coin` | 充币下单参数 |
| `POST /api/face/init` | 需 SDK `meta_info` |

---

## 4. 各通道边界

### IM

- 本地 UserSig：`sign.gen_user_sig`（APK 硬编码 SECRETKEY）— **仅 CLI / 显式 Lab 研究使用**；产品 Web 不隐式回退到本地签名。
- 服务端：`tximsign.php`；登录响应 `userSign`；产品 Web 默认且强制使用该路径。
- 融云：`APP_KEY=m7ua80gbmo0km` + `userregister.php`。
- 实时收发：由受信任宿主提供固定版本 `@tencentcloud/chat` 或融云 Web SDK，再用 BFF 凭证 `login`。

### 刷脸

```
ZIM.metaInfo → face.start → certifyId → ZIM 活体 → face.describe → 重登看 rp_verify_time
```

假 certifyId 历史上 `message=T` 但 **`rp_verify_time` 不变**（见 08）。

### 支付

| 类型 | 协议 | 收银 |
|---|---|---|
| 充币微信/支付宝 | `buyCoin*XBXX` | 官方 SDK；商户绑官方包名 |
| VIP/SVIP | `Payunifiedorder2*` / `Alipayaddorder2*` | 同上 |
| 匹配卡 | `buyCard` | **无外部收银**，扣乐园币 |

Web 自建支付通常 **不能**直接复用官方 App 的微信商户配置。

---

## 5. 文件清单

| 路径 | 说明 |
|---|---|
| `bbw_protocol/adapters/im.py` | TIM / 融云凭证 |
| `bbw_protocol/adapters/face.py` | 刷脸会话编排 |
| `bbw_protocol/adapters/pay.py` | 下单结果规范化 |
| `bbw_protocol/adapters/bundle.py` | `NativeBundle` / `app.native` |
| `bbw_web/bff_server.py` | stdlib BFF |
| `bbw_web/static/` | 演示 UI |

---

## 6. 后续可选

1. 正式前端用 npm `@tencentcloud/chat` 替代 CDN 实验按钮。  
2. 阿里云 H5 实人页或引导「回 App 实名」。  
3. 支付仅调试下单；到账以协议查余额为准。  
4. 勿将 BFF 对公网暴露。
