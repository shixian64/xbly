# bbw_web — 原生能力脚手架（IM / 刷脸 / 支付）

把 `bbw_protocol` 的 **HTTP 业务核** 接到 **BFF + 浏览器**，补齐原先缺的三类原生能力的**集成面**（不是伪造）：

| 能力 | 协议核 | 本脚手架 | 仍须官方 SDK / 合规 |
|---|---|---|---|
| IM 实时 | UserSig / 融云 register | BFF 签发凭证 + 前端接 TIM | 腾讯 IM / 融云 Web SDK 收发 |
| 刷脸实名 | Init / Describe | BFF 编排 HTTP | 阿里云 ZIM 出 metaInfo + 活体 |
| 支付确认 | buyCoin* / 统一下单 / buyCard | BFF 返回 order_params | 微信/支付宝收银台 + 商户绑定 |

## 架构

```
Browser (static/index.html)
    │  fetch /api/*
    ▼
bff_server.py  (stdlib HTTP)
    │  UserSig 在此签发，SECRETKEY 不进前端
    ▼
bbw_protocol + adapters/
    │
    ▼
applet.banghua.xin / 腾讯 IM / 阿里云 / 微信支付宝
```

## 启动

```powershell
cd D:\project\AI\bbw\analysis

# 1. 先登录写出 session.json
python -m bbw_protocol.cli login --phone YOUR_PHONE --password "YOUR_PASSWORD"

# 2. 起 BFF（默认 127.0.0.1:8765）
python -m bbw_web --port 8765

# 3. 浏览器打开
# http://127.0.0.1:8765/
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 探活 |
| GET | `/api/me` | 会话 + 能力状态 |
| GET | `/api/bootstrap?prefer=local` | whoami + IM + pay meta |
| GET | `/api/im/tim?prefer=local\|server` | TIM 登录载荷 |
| GET | `/api/im/rong` | 融云 token |
| GET | `/api/im/bootstrap` | TIM + 融云 |
| POST | `/api/pay/coin` | `{"channel":"wechat","coin_id":"1"}` |
| POST | `/api/pay/vip` | `{"channel":"alipay","level":"vip"}` |
| POST | `/api/pay/card` | `{"card_id":"1"}` 乐园币买卡 |
| POST | `/api/face/init` | cert_name / cert_no / meta_info |
| POST | `/api/face/describe` | certify_id + 证件 |
| GET | `/api/face/status` | 管道说明 + session |

## Python 直接用 adapters

```python
from bbw_protocol import BeibeiwuApp
from bbw_protocol.adapters import NativeBundle

app = BeibeiwuApp.load()
n = NativeBundle(app)

print(n.im.tim_login_payload())     # 给 TIM Web login
print(n.im.rong_register().token)
print(n.pay.prepare_coin_wechat("1").to_dict())
# face: meta_info 来自阿里云 SDK 后再 n.face.start(...)
```

## 安全注意

- **不要**把 `TXIM_SECRETKEY` 打进前端仓库或静态资源。
- 支付 **不要**伪造异步 notify；订单失败多半是商户/包名绑定，不是缺 `do=`。
- 刷脸 **不要**指望假 certifyId；以重登后 `rp_verify_time` 为准。
- BFF 默认只绑 `127.0.0.1`；勿对公网裸奔。

## 与 CLI 配合

```powershell
python -m bbw_protocol.cli native-status
python -m bbw_protocol.cli im-tim
python -m bbw_protocol.cli im-rong
python -m bbw_protocol.cli pay-coin --channel wechat --coin-id 1
python -m bbw_protocol.cli face-status
```
