# 09 · 游客能力矩阵与协议可落地性

**最后更新：** 2026-07-15  
**样本账号：** uid=`726285`，nickname=`游客`，user_role=`普通用户`，`rp_verify_time=0`，phone 已绑定（脱敏），vip/svip=0，money=0  
**原始探测结果：** `guest_capability_results.json`  
**脚本：** `guest_capability_probe.py`、`guest_gates_scan.py`

---

## 0. 术语：什么叫「游客」

本项目里「游客」要拆成 **三层身份**，否则门禁会混在一起：

| 层级 | 判定条件（客户端/数据） | 本账号是否 |
|---|---|---|
| **L0 未登录 anon** | `uid=="0"` 或 SP userId 空/`0`；无 `AUTHOR-TOKEN` | 否（探测用 token=0 模拟） |
| **L1 已登录游客像账号 guest** | 有 uid+token；nickname 常为默认「游客」；资料极简；**未实名** `rp_verify_time=="0"` | **是（当前测试号）** |
| **L2 正常用户** | 已登录 + 资料完善 + 通常已实名 | 否 |
| **L3 会员** | `vip`/`svip` 为未来时间戳（`isVip/isSVip`：值 > 当前秒） | 否 |

> 注意：服务端返回的 `user_role=普通用户` 与 UI 昵称「游客」不是同一字段。  
> 「游客」主要是 **默认昵称 + 弱资料态**；业务门禁更多看 **登录 / 实名 / 手机 / 会员 / 资产**。

---

## 1. 门禁维度（静态）

### 1.1 客户端条件命中统计（业务包扫描）

| 条件 | 约涉及文件数 | 典型用途 |
|---|---:|---|
| `rp_verify_time=="0"` | 30 | 发帖、加好友、匹配、评论、开播/群主申请、聊天部分入口… |
| 余额/money | 26 | 送礼、装扮、兑换 VIP |
| `uid=="0"` | 23 | 未登录拦截（聊天、匹配、部分内容） |
| VIP/SVIP 判断 | 21 | 特权、装扮、次数上限 |
| 手机号 empty | 11 | 与实名并列的二次门禁（加好友/匹配/发帖/评论等） |
| 匹配卡/改名卡 | 10+10 | 道具消耗 |
| `isSignIn()` | 5 | 进部分页强制跳登录 |

### 1.2 高频文案门禁

| 文案 | 含义 | 主要触发 |
|---|---|---|
| 登录后可以发起聊天！ | 需 L1 | uid==0 |
| 登录后可以发起匹配！ | 需 L1 | uid==0 |
| 登录以查看更多内容！ | 需 L1 | 动态/个人页等 |
| 还未实名认证,请前往实名认证 | 需实名 | 发帖/好友/匹配/评论/部分聊天 |
| 还绑定手机号,请前往绑定 | 需绑定手机 | 同上链路常紧跟实名检查 |
| 匹配卡/CP卡/改名卡不足 | 需道具 | 匹配、CP、改名 |
| 余额不足 | 需 money | 送礼/兑 VIP |
| 非会员每日可发起5次添加好友… | 需 VIP 或等次日 | 加好友频控 |
| 礼仪分不足80，不能使用聊天功能 | 需礼仪分 | 会话页 |
| 请先提升等级 | 等级 | 主播/群主入驻 |
| 请完成实名认证后再试 | 提现等资金 | Wallet |

### 1.3 服务端统一错误码（协议层）

| code / 形态 | 含义 | 客户端处理 |
|---|---|---|
| `700` + extra 提示 | 登录失效 | 清登录态跳登录 |
| `403` | 业务拒绝（未实名改资料/提现等） | Toast |
| `400` | 参数/业务错误 | Toast |
| `300` | 需重新校验/拉起 launch | 跳 launch |
| 纯文本 `余额不足`/`false`/`no` | 弱协议业务结果 | 视接口 |
| 空 body | 未登录或无数据/未实现 | 需个案判断 |

### 1.4 VIP 判定（协议字段）

```java
// CommonUtil.isVip / isSVip
vip或svip 解析为 int，若 > 当前 unix 秒 → 会员有效
// 故 "0" = 非会员；正大整数 = 到期时间戳
```

---

## 2. 功能矩阵（产品功能 × 身份）

图例：

- ✅ 可用  
- ⚠️ 部分可用 / 仅客户端拦或结果降级  
- ❌ 不可用  
- 🧪 协议已探测  
- 📦 协议可封装  

### 2.1 账号与会话

| 功能 | L0 未登录 | L1 游客登录(本号) | 协议 | 可落地 |
|---|---|---|---|---|
| 密码登录 signin0 | ✅ 入口 | ✅ | 🧪 | 📦 高 |
| 无验证码 SigninOneKeyLogin1 | ✅（洞） | ✅ | 🧪 | 📦 高（滥用面） |
| 短信登录 | ✅ | ✅ | 🧪 | 📦 高 |
| 改密 findpassword | ❌ 700 | ✅ | 🧪 | 📦 高 |
| 保持会话 AUTHOR-TOKEN | ❌ | ✅ | 🧪 | 📦 高 |
| 退出/700 踢下线 | — | ✅ 可触发 | 🧪 | 📦 中 |

### 2.2 浏览 / 公开读

| 功能 | L0 | L1 | 协议实测 | 可落地 |
|---|---|---|---|---|
| 礼物列表 getGiftList | ✅ JSON 列表 | ✅ | 🧪 两边都有数据 | 📦 高 |
| 推荐/幻灯 Tuijiannew | ✅ | ✅ | 🧪 | 📦 高 |
| 轮播 getSlide | ✅ 空数组 | ✅ | 🧪 | 📦 高 |
| 广告开关 isShowAD1 | ⚠️ 空 | ✅ `message=10` | 🧪 | 📦 中 |
| 聊天敏感词 getChatCensorship | ✅ 词表 | ✅ | 🧪 | 📦 高 |
| 推荐码 getReferral | ✅ 返回数字 | ✅ | 🧪 | 📦 中 |
| testField | ✅ 返回调试文案 | ✅ | 🧪 | 📦 中（信息泄露） |
| 用户资料 getUserAttributes* | ⚠️ 异常/空 | ⚠️ 响应异常形态 | 🧪 需再修解析 | 📦 中（接口在，解析要修） |
| 关注列表 | ⚠️ 脏数据 userId=0 | ⚠️ false | 🧪 | 📦 低-中 |

### 2.3 社交写操作

| 功能 | L0 | L1 客户端 | L1 协议 | 可落地 |
|---|---|---|---|---|
| 关注 follow | ❌/空 | UI 可；协议 **成功** | 🧪 `ok:成功` | 📦 高 |
| 发帖 publish | ❌登录 + 实名+手机 | UI 拦实名 | 未直接打 publish 写接口 | 📦 中（客户端强拦，服务端待测） |
| 评论 | 同发帖 | UI 实名+手机 | 待测 | 📦 中 |
| 加好友 | 登录+实名+手机+频控/VIP | UI 多重门禁 | 待测 | 📦 中 |
| 匹配 | 登录+实名+手机+匹配卡 | UI 多重 | 🧪 remove 返回 NULL | 📦 中 |
| 聊天/会话 | 登录；礼仪分；部分实名 | UI 有礼仪分/手机/实名 | IM 另栈 | 📦 中（融云/腾讯 IM） |
| 拉黑列表 | false | false | 🧪 | 📦 低 |

### 2.4 资料修改

| 功能 | L0 | L1 | 协议 | 可落地 |
|---|---|---|---|---|
| 改昵称 resetNew | ❌ 700 | ❌ **403 未实名** | 🧪 | 📦 高（门禁清晰） |
| 改名次数 resetNum | false | false（可改次数态） | 🧪 | 📦 高 |
| 性别等 reset 类 | — | 已实名不可改性别等 | 静态 | 📦 中 |
| 实名 RP 流程 | — | 可走 Init（真资料） | 🧪 假参失败 | 📦 中（合法实名流程） |
| 人工实名申请 | — | 可提交等待审核 | 🧪 | 📦 中 |

### 2.5 资产 / VIP / 房间

| 功能 | L0 | L1 | 协议 | 可落地 |
|---|---|---|---|---|
| 余额兑 VIP | ❌ 700 | ❌ 余额不足 | 🧪 | 📦 高 |
| 提现 withdraw | ❌ 700 | ❌ **403 未实名不可提现** | 🧪 | 📦 高 |
| 送礼 | ❌ 700 | false（无钱/参数） | 🧪 | 📦 中 |
| SvipTry | 空 | 空（副作用见历史 svip_try） | 🧪 | 📦 中 |
| 创建语音房 createRoom0 | no | no | 🧪 | 📦 中（条件未满足） |
| 房间权限 getRoomAuth | 空 | 空 | 🧪 | 📦 低 |

### 2.6 客户端专属（难纯协议）

| 功能 | 说明 | 协议化 |
|---|---|---|
| 腾讯 IM 收发 | 需 userSign + SDK | 📦 中（有 userSign/密钥） |
| 融云 IM | rong token + SDK | 📦 中 |
| 一键登录运营商 | AUTH_SECRET + 运营商 SDK | 📦 低 |
| 阿里云刷脸 ZIM | 原生 SDK | 📦 低（仅能协议化 Init/Describe） |
| Sophix 热修 | 原生 | 📦 低 |
| 本地 isSignIn 跳转 | 纯客户端 | 不需要协议 |

---

## 3. 协议实测对照表（L0 vs L1）

来源：`guest_capability_probe.py` 一次跑批。

| 接口/能力 | L0 anon | L1 guest_login |
|---|---|---|
| getGiftList | 有列表 JSON | 同 |
| Tuijiannew | 有推荐 JSON | 同 |
| getChatCensorship | 敏感词长文本 | 同 |
| getReferral | `20978` | 同 |
| testField | `测试腾讯im账号导入` | 同 |
| UpdateOnline0 | 返回一段 token 样字符串 | 另一种字符串 |
| isShowAD1 | 空 | `200/10` |
| follow | 空 | **`成功。`** |
| resetNew | **700 登录失效** | **403 未实名** |
| moneyExchangeVip | 700 | **余额不足** |
| withdraw | 700 | **403 未实名不可提现** |
| sendGift1 | 700 | false |
| createRoom0 | no | no |
| getTopic | 700 | `[]` |
| 礼仪分接口 | 700 | 异常/空 code 形态 |

### 3.1 关键推断

1. **大量读接口不校验登录**（礼物、推荐、敏感词、testField、推荐码）→ 未登录可信息收集。  
2. **写/资金接口先校验登录（700），再校验业务（403/余额）**。  
3. **L1 游客号已能 follow 成功** → 「游客昵称」≠ 社交写死。  
4. **实名门禁至少覆盖：改个人信息、提现**（服务端 403 文案明确）。  
5. **创建房间返回 `no`**：另有条件（等级/权限/实名/业务开关），非单纯登录。

---

## 4. 当前测试号（L1 游客像）能力总结

### 4.1 已确认可用（协议或逻辑）

- 登录 / 维持 token / 改密  
- 拉公开配置与列表（礼物、推荐、敏感词、testField…）  
- 关注他人（follow 成功）  
- 在线心跳 UpdateOnline0（有回包）  
- 查询改名次数（false=客户端认为可改）  
- 触发人工实名提交（不等同通过）  
- 无验证码登录洞（F-001）

### 4.2 已确认不可用 / 被拒

| 能力 | 拒绝形态 |
|---|---|
| 改昵称等 resetNew | 403 未实名 |
| 提现 | 403 未实名 |
| 兑 VIP | 余额不足 |
| 送礼（本参数） | false |
| 创建房间 | no |
| 客户端发帖/匹配/加好友入口 | 实名+手机等 UI 门禁（手机本号已绑，主要卡实名） |
| VIP 特权 | vip/svip=0 |
| 改名卡/匹配卡消耗玩法 | 卡数 0 |

### 4.3 灰区（需补测）

- 动态列表/帖子详情完整读写  
- 私信发送（礼仪分阈值）  
- 语音房进房旁观 vs 开播  
- getUserAttributes 响应解析（探测显示异常，可能 Content-Type/编码/嵌套 JSON）  
- 未登录 follow 是否服务端真写库  
- 发帖 multipart 是否仅客户端拦

---

## 5. 是否能落为协议实现？

### 5.1 结论

**能。** 本 App 业务主路径高度 HTTP 化（微擎 `do=` + 固定 Header），适合做成 Python「协议客户端」。  
不能 100% 覆盖的是：原生 SDK（刷脸、部分一键登录、IM 长连接）与强 UI 流程。

### 5.2 建议模块拆分

```
bbw_protocol/
  auth.py          # signin0 / onekey / sms / findpassword / token 刷新
  sign.py          # SIGN/EXPIRE/UNIQUE/UserSig（已有 bbw_client.py）
  client.py        # startHttp 封装、错误码统一
  social.py        # follow/unfollow/black/list
  profile.py       # resetNew/resetNum/getUser*
  economy.py       # gift/vip/withdraw/coin
  room.py          # createRoom/roomSet/agora token URL
  content.py       # 公开读：gift/slide/ads/censor
  im_tx.py         # 可选：UserSig + REST/IM
  models.py        # UserInfo 字段
  gates.py         # 本地预检：是否登录/实名/余额（镜像客户端）
```

### 5.3 落地优先级

| 优先级 | 模块 | 理由 | 依赖 |
|---|---|---|---|
| P0 | auth + sign + client | 一切基础；F-001 可做登录器 | 无 |
| P0 | content 公开读 | 未登录即可，易测 | 无 |
| P1 | social follow 等 | L1 已证实可用 | token |
| P1 | profile reset* | 门禁明确，实名后立刻有用 | token + 实名 |
| P1 | economy | 403/余额语义清晰 | token |
| P2 | room / match | 返回 no/NULL，需补条件 | 业务条件 |
| P2 | IM | 有密钥/userSign | 网络与 SDK 或 REST |
| P3 | 刷脸 | 仅协议壳，核心在阿里云 SDK | 真机 |

### 5.4 统一客户端伪代码

```python
class BBWClient:
    def __init__(self, uid="0", token="0"):
        self.uid, self.token = uid, token

    def start_http(self, action, body=None):
        # headers: AUTHOR-TOKEN / EXPIRE-TOKEN / SIGN-TOKEN
        # url: getUrl(action)
        ...

    def login_password(self, phone, password): ...
    def login_onekey_phone_only(self, phone): ...  # F-001
    def follow(self, target_uid): ...
    def reset_nickname(self, name): ...  # may 403
```

已有基础：`bbw_client.py`、`auth_flow.py`、`profile_edit.py`、`guest_capability_probe.py`。

### 5.5 协议实现时要注意的坑

1. **错误码不统一**：JSON code / 纯文本 / 空 body 混用 → 统一 `classify()`。  
2. **resetNew 客户端用 multipart**，form 也能触发同样 403（已验证）。  
3. **phone 字段**登录 JSON 为 `***********`，`TextUtils.isEmpty(phone)` 在客户端可能为 false（已绑定）；协议侧不要假设空。  
4. **VIP 是时间戳不是 0/1**。  
5. **部分接口未登录也返回「像成功」的数据**（脏关注列表），写库与否要二次验证。  
6. **NO_PROXY / 弱 SSL**：抓包需 Frida 或改包；协议直连不受影响。

---

## 6. 与「游客」相关的攻击/审计价值

| 点 | 说明 |
|---|---|
| L0 信息暴露 | 礼物、推荐、敏感词、testField、推荐码未登录可读 |
| L1 社交已写 | 未实名仍可 follow |
| 实名门禁不一致 | 改资料/提现服务端强校验；部分写操作可能仅客户端拦 |
| F-001 | 任意手机号可升级到 L1，再打 L1 能力面 |
| testField | 调试文案未登录可达 |

---

## 7. 建议的下一轮补测清单

1. 发帖/评论真实 `do=` 在 L1 是否仅客户端拦  
2. 加好友/匹配完整参数与卡消耗  
3. 修复 getUserAttributes 响应解析并确认越权  
4. 未登录 follow 是否落库  
5. 进房旁观类 redis 接口  
6. 礼仪分字段与聊天发送接口  
7. 将 `guest_capability_probe` 扩到 50+ 高频 action 自动分级  

---

## 8. 文档与产物

| 文件 | 作用 |
|---|---|
| 本文 | 能力矩阵 + 协议落地结论 |
| `guest_capability_results.json` | 原始探测 |
| `guest_capability_probe.py` | 可重复跑批 |
| `guest_gates_scan.py` | 静态门禁文案/条件 |
| `05_ACCOUNT.md` | 账号字段 |
| `08_*.md` | 实名专项 |
