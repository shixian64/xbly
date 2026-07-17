# 11 · 功能全景：实名门槛 / 其他功能 / 协议覆盖

**最后更新：** 2026-07-15  
**依据：** jadx 静态门禁 + 协议实测 + `api_catalog.json`（v154 活跃 398；历史并集 405）+ `bbw_protocol`

---

## 0. 先回答三个核心问题

### Q1. 需要实名认证前置的功能有哪些？

分两层：

| 层级 | 含义 | 代表 |
|---|---|---|
| **S 服务端硬门禁** | 协议直接打也会拒（已实测） | 改个人信息 `resetNew`、提现 `withdraw` |
| **C 客户端硬门禁** | App UI 先拦；服务端是否同样拦部分未逐条测 | 发帖、评论、加好友、匹配、部分进群/游戏、主播/群主/督导申请等 |
| **B 权益型（已实名才可用的装扮）** | 不是「入口禁止」，而是商品描述/装备条件 | 「实名认证专属头像框/坐骑」 |
| **R 反向限制** | 实名后反而不能改 | **性别**（已实名不可改性别） |

### Q2. 其他功能有什么？

不依赖实名（或仅需登录/手机/会员/余额）的功能面很大：登录会话、公开浏览、礼物列表、推荐、关注、心跳、房间相关、匹配浏览、IM 辅助、充值下单参数、VIP 试用接口等。详见第 2 节。

### Q3. 是否实现了 APK 所有功能接口？

| 维度 | 结论 |
|---|---|
| **HTTP `do=` / startHttp 业务接口** | **名称可达**：目录含 v154 活跃 **398** 个 action，另保留 7 个 v148 历史下线项；特殊 `i/m`、absolute URL、multipart 仍应使用对应调用器 |
| **语义化封装方法** | modules/adapters 当前有 **105** 个静态 action 名，另有动态支付 action；其余用通用 `call` |
| **原生 SDK 能力** | **否 / 部分**：人脸活体、支付收银台 UI、IM 实时长连接需外挂 SDK |
| **因未实名测不通 ≠ 未实现** | **正确**：协议方法已实现；服务端 403 是业务门禁，不是缺接口 |

---

## 1. 需要实名的功能清单

### 1.1 服务端已证实（协议打也拦）

| 功能 | 接口 / 路径 | 实测拒绝 |
|---|---|---|
| 修改个人信息（昵称等） | `do=resetNew`（multipart，`type=昵称设置` 等） | `403 未实名账号不可修改个人信息` |
| 提现 | `do=withdraw` | `403 未实名账号不可提现` |

> 说明：改名 UI 在 `NicknameEditActivity`，最终仍走 `resetNew`。

### 1.2 客户端强制实名（代码门禁明确）

下列位置在 `rp_verify_time == "0"` 时弹窗/Toast 并跳转 `/app/rp_verify`（或拦截提交）：

| 功能 | 代码位置 | 典型文案 | 常同时要求 |
|---|---|---|---|
| **发帖** | `publish/PublishPostActivity` | 还未实名认证 | 绑定手机 |
| **评论** | `comment/CommentDialog` | 还未实名认证 | 绑定手机 |
| **添加好友** | `Main2Branch/AddFriend` | 还未实名认证 | 绑定手机 |
| **申请好友/找搭子** | `discipline/friend/ApplyFriendDialog`、`CreateFindFriendDialog` | 还未实名认证 | 绑定手机 |
| **匹配** | `match/MatchActivity` | 还未实名认证 | 绑定手机、匹配卡 |
| **群会话相关操作** | `ConversationActivity`（`targetId` 以 `group` 开头） | 还未实名认证 | — |
| **五子棋会话** | `ConversationActivity`（`fivechess`） | 还未实名认证 | — |
| **主播入驻申请** | `chatroom/AnchorApplyActivity`、`cn.leyuan...AnchorApplyActivity` | 请先完成实名认证 | 等级、协议勾选 |
| **群主入驻申请** | `GroupOwnerApplyActivity` | 请先完成实名认证 | 等级、协议 |
| **督导申请** | `discipline/SupervisorApplyActivity` | 请先完成实名认证 | 绑定手机 |
| **督导相关添加** | `supervision/AddZBDialog` | 还未实名认证 | 绑定手机 |
| **首页部分入口** | `MainActivity` | 还未实名认证 | 绑定手机 |
| **钱包提现入口** | `Main5Branch/WalletNewActivity` | 请完成实名认证后再试 | 收益余额 |

### 1.3 实名权益（有实名才能用的商品）

| 功能 | 说明 |
|---|---|
| 实名认证专属头像框 / 坐骑 / 头衔框 | `AvatarFrameAdapter`、`EnterAnimAdapter`、`GoodsFragment`、`EnterAnim` 列表描述含「实名认证专属」 |

未实名时通常不能装备这些装扮（客户端判断 `rp_verify_time != "0"`）。

### 1.4 实名后的反向限制

| 功能 | 行为 |
|---|---|
| **修改性别** | `ResetPersonalInfoFragment`：已实名 → Toast「已实名账号不可修改性别，请联系客服」 |

### 1.5 实名本身相关能力（做实名用的）

| 功能 | 协议 |
|---|---|
| 人脸初始化 | `InitFaceVerify0.php` |
| 人脸结果查询 | `DescribeFaceVerify0.php` |
| 实名结果上报 | `SaveRPVerifyInfo` |
| 人工实名 | `applyManualVerify` + 图片 OSS |
| 年龄校验 | `checkAge` |
| 二次身份证确认 | `verify_certNo` |
| 二要素/元信息（v154） | `Id2MetaVerifyRequest`（`misc.id2_meta_verify`；缺参常见 401） |

这些在 `bbw_protocol.modules.misc` / 文档 08 已覆盖。**活体通过依赖阿里云原生 SDK，协议只能走到 HTTP 壳。**

### 1.6 实名门禁模式图

```
                    ┌─────────────┐
                    │  未实名用户  │
                    └──────┬──────┘
           ┌───────────────┼───────────────┐
           v               v               v
    【服务端 403】   【客户端拦截】    【仍可使用】
    resetNew 改资料   发帖/评论        登录/浏览
    withdraw 提现     加好友/匹配      关注 follow
                      主播/群主申请    礼物列表
                      部分群/游戏会话  推荐/心跳
                      钱包提现入口     IM token 获取…
```

---

## 2. 其他功能（不依赖实名，或主要依赖别的条件）

按产品域分类。标注：**登录** / **手机** / **会员** / **余额** / **道具卡** / **等级** 等额外门槛。

### 2.1 账号与会话（不需实名）

| 功能 | 额外条件 | 协议 |
|---|---|---|
| 密码登录 | — | `auth.login_password` / `signin0` |
| 短信登录 | 短信码 | `sms_send` + `smsVerify0` + `SigninOneKeyLogin1` |
| 一键登录（弱） | 手机号 | `SigninOneKeyLogin1`（F-001） |
| 改密 | 登录 | `findpassword` |
| 登出 / 700 踢下线 | — | `logout` / 错误码处理 |
| 唯一设备 token | 登录 | `uniquelogin` |
| 心跳在线 | 登录更佳 | `UpdateOnline0` |

### 2.2 浏览与公开内容（多数未登录也可）

| 功能 | 额外条件 | 协议 |
|---|---|---|
| 礼物列表 | 无 | `getGiftList` |
| 推荐/幻灯 | 无 | `Tuijiannew` / `getSlide` |
| 广告开关 | 登录后更完整 | `isShowAD1` |
| 敏感词/审查配置 | 无 | `getChatCensorship` 等 |
| 推荐码 | 无 | `getReferral` |
| 版本/关于 | 无 | `getVersion1` / About H5 |
| testField | 无 | `testField`（调试） |

### 2.3 社交（部分要实名，部分不要）

| 功能 | 实名？ | 其他条件 | 协议 |
|---|---|---|---|
| **关注 / 取关** | **否**（已实测未实名可成功） | 登录 | `follow` / `unfollow` |
| 粉丝/关注列表 | 否 | 登录 | `getFansUser` / `getFollow*` |
| 加好友 | **是（客户端）** | 手机、会员频控 | 对应 friend apply actions |
| 发帖/删帖/点赞 | 发帖**是**；点赞可能否 | 登录 | `luntanlike` 等 |
| 评论 | **是（客户端）** | 手机 | comment 系列 |
| 黑名单 | 否（列表可读） | 登录 | `getMyBlackList` 等 |
| 举报 | 视场景 | 登录 | `ReportViolation` |

### 2.4 资料与设置

| 功能 | 实名？ | 说明 | 协议 |
|---|---|---|---|
| 查看自己/他人资料 | 否 | — | `getUserAttributes*` |
| 改昵称/头像/签名等 resetNew | **是（服务端）** | 另有改名卡/180 天 | `resetNew` |
| 改性别 | 未实名可进；**已实名不可改** | — | reset 系列 |
| 隐私设置 | 否 | — | `setprivatesetting` |
| 礼仪分查看 | 否 | 聊天可能卡礼仪分 | `getEtiquetteScoreAndDescription` |

### 2.5 匹配 / 漂流瓶 / 约会

| 功能 | 实名？ | 其他 | 协议 |
|---|---|---|---|
| 发起匹配 | **是（客户端）** | 手机、匹配卡 | match 系列 |
| 漂流瓶扔/捡 | 视客户端分支 | 登录 | `ThrowADriftBottle` 等 |
| 约会发布/申请 | 视分支 | 登录 | `PublishDating` 等 |

### 2.6 语音房 / 游戏

| 功能 | 实名？ | 其他 | 协议 |
|---|---|---|---|
| 建房 | 未证实名硬拦；实测 `no` | 权限/业务条件 | `createRoom0` |
| 房间设置/点歌/礼物 | 视场景 | 登录、余额 | room / gift |
| 声网 RTC/RTM token | 否（HTTP） | 登录 | Agora PHP |
| 五子棋/部分群玩法入口 | **是（客户端）** | — | 会话内 H5/接口 |

### 2.7 经济 / VIP

| 功能 | 实名？ | 其他 | 协议 |
|---|---|---|---|
| 礼物列表 | 否 | — | `getGiftList` |
| 送礼 | 否（客户端） | **余额** | `sendGift*` |
| VIP 试用 SvipTry | 否 | 业务规则 | `SvipTry` |
| 余额兑 VIP | 否 | **余额** | `moneyExchangeVip` |
| 充值下单（微信/支付宝参数） | 否 | 登录 | `buyCoin*` / 统一下单 |
| **提现** | **是（服务端）** | 收益 | `withdraw` |
| 会员特权次数 | 否 | **VIP 时间戳** | 客户端 isVip |

### 2.8 IM

| 功能 | 实名？ | 其他 | 协议 |
|---|---|---|---|
| 获取腾讯 UserSig（服务端） | 否 | 登录 | `tximsign.php` |
| 本地生成 UserSig | 否 | 密钥硬编码 | `im.local_user_sig` |
| 融云注册 token | 否 | 登录 | `userregister.php` |
| 实时收发消息 | — | **需 IM SDK** | 协议库不内置长连接 |
| 闪照/表情 HTTP | 否 | 登录 | flash/sticker actions |

### 2.9 其他门槛（非实名）汇总

| 门槛 | 影响功能 |
|---|---|
| **未登录** | 聊天、匹配、改密、多数写操作 → 700 或 UI 跳登录 |
| **未绑手机** | 与实名并列出现在发帖/好友/匹配/评论 |
| **VIP/SVIP** | 加好友次数、装扮、特权文案 |
| **余额/金币** | 送礼、兑 VIP |
| **改名卡/匹配卡/CP 卡** | 改名、匹配、CP |
| **礼仪分 &lt; 80** | 聊天功能（会话页文案） |
| **等级** | 主播/群主入驻 |
| **相册未上传** | 「上传后可查看更多」 |

---

## 3. 协议实现覆盖评估

### 3.1 数字结论

| 项 | 数量 |
|---|---:|
| catalog 历史 action 并集 | **405**（含 7 个 v148 下线小说 action） |
| v154 当前活跃 actions | **398** |
| 分类（auth/profile/social/…） | 见 `api_catalog.json` |
| 具名静态 action（modules/adapters） | **105**（另有动态支付 action） |
| 通用 `app.call("AnyAction")` 名称可达 | **405**；非默认租户/模块、absolute URL、multipart 需专用调用器 |
| v154 完整/重建 URL | 328（含 H5/支付 PHP；已补运行时拼接的两个登录 URL） |

### 3.2 「是否实现了所有 APK 功能接口？」

分清楚 **接口实现** vs **业务跑通**：

```
APK 功能
├── A. HTTP 业务接口（微擎 do= / otherinterface PHP）
│     └── bbw_protocol：✅ 已具备各类调用器；catalog 完成名称枚举，冷门接口仍需准确参数/租户/编码
├── B. 服务端业务门禁（实名/余额/权限）
│     └── 协议已实现调用；未满足条件会 403/文本失败（与 App 一致）
├── C. 客户端-only UI 逻辑
│     └── 不需要协议（跳转、本地校验）；可用协议绕过部分 UI 门禁去打服务端
└── D. 原生 SDK（2026-07-15 已加 adapters + bbw_web BFF）
      ├── 人脸 ZIM          → adapters.face 编排 HTTP；活体仍需阿里云 SDK
      ├── 微信/支付宝支付 UI → adapters.pay 规范 order_params；收银官方
      ├── 腾讯/融云实时 IM  → adapters.im 出凭证；TIM Web 接 BFF
      └── 运营商一键登录    → 可用短信/密码替代
```

**简答：**

- **传输层具备调用能力**：但仅有 action 名还不等于已还原参数、`i/m`、multipart/file 等语义。
- **不是：不等于每个功能在当前游客未实名账号上都能业务成功。**  
- **因实名失败测不通的**（如改昵称、提现），**协议代码路径已写好**，实名后同一命令即可再测。

### 3.3 模块 vs 功能映射

| App 域 | 协议入口 | 覆盖方式 |
|---|---|---|
| 登录注册 | `app.auth.*` | 具名 |
| 首页内容 | `app.content.*` | 具名 + call |
| 社交 | `app.social.*` | 具名 + call |
| 资料 | `app.profile.*` | 具名 + call |
| 钱包 VIP | `app.economy.*` | 具名 + call |
| 语音房 | `app.room.*` | 具名 + call |
| 匹配漂流瓶 | `app.match.*` | 具名 + call |
| IM 辅助 | `app.im.*` | 具名 + UserSig |
| IM 实时凭证 | `app.native.im.*` / BFF `/api/im/*` | adapters + Web |
| 实名 HTTP | `app.misc.face_*` / `app.native.face.*` | 具名 + 编排 |
| 支付下单 | `app.native.pay.*` / BFF `/api/pay/*` | order_params |
| **任意冷门按钮** | `app.call*` / `call_url` / multipart | 名称全量；按真实传输元数据选择调用器 |

### 3.4 未实名账号上：协议「已实现且可成功」vs「已实现但被拒」

| 状态 | 例子 |
|---|---|
| ✅ 已实现且当前可成功 | 登录、礼物列表、推荐、关注、心跳、UserSig、testField… |
| ⚠️ 已实现，业务拒绝 | `nick`→403 实名；`withdraw`→403 实名；`exchange-vip`→余额不足；`room-create`→no |
| ⚠️ 已实现，客户端会拦但协议可尝试 | 发帖/加好友等（需补参数；服务端是否 403 待实名前后对比） |
| ❌ 协议无法单独完成 | 刷脸**活体通过**、支付**资金确认**、IM **长连接收发**（现已提供 SDK 集成面，非伪造） |

---

## 4. 使用指引（实名前后）

### 未实名现在就能当「半个 App」用

```powershell
cd <repo-root>
python -m bbw_protocol.cli login --phone ... --password ...
python -m bbw_protocol.cli bootstrap
python -m bbw_protocol.cli gifts
python -m bbw_protocol.cli follow <uid>
python -m bbw_protocol.cli me
python -m bbw_protocol.cli call <任意Action>
```

### 实名后应立刻验证的「曾 403」接口

```powershell
python -m bbw_protocol.cli nick Vom
python -m bbw_protocol.cli withdraw --alipay ... --name ... --amount ...
# 以及发帖/好友等 call 补参测试
```

---

## 5. 结论表（给决策用）

| 问题 | 结论 |
|---|---|
| 哪些必须实名？ | **改资料、提现（服务端）**；发帖/评论/好友/匹配/主播群主督导申请/部分会话玩法/提现入口（客户端，多数还加绑手机） |
| 哪些不必须实名？ | 登录、浏览、礼物/推荐配置、关注、心跳、多数读接口、IM token、充值下单参数等 |
| 协议是否覆盖全部 HTTP 接口？ | catalog 名称面为 **405**（v154 活跃 398）；默认 `call` 并不替代 `call_i888` / `call_url` / multipart 等传输差异 |
| 是否等于完整 APK？ | **HTTP 业务层 ≈ 是**；**原生 SDK 层 ≠ 是** |
| 实名导致测失败算不算没实现？ | **不算**；接口已实现，门禁与 App 一致 |

---

## 6. 匹配次数 / 匹配卡（补充，2026-07-15 协议实测）

### 6.1 卡从哪来

| 来源 | 接口 | 实测 |
|---|---|---|
| 商城购买 | `buyCard`（`card_id=1` 等为匹配卡套餐，扣乐园币） | 币不足 → `400 乐园币不足` |
| 每日/每周任务 | `createHotActivityList` 列表 + `receiveHotActivityList` 领取 | 奖励文案含「匹配卡一张」；进度未满时领取无有效回包 |
| 任务示例（本号） | 聊天30句 / 观看动态100条 / 匹配3次 → 各奖匹配卡1张；周任务话题 → 改名卡 | 进度均为 0 时不能靠空领取加卡 |

### 6.2 免费次数 vs 扣卡（服务器裁决）

| 类型 | 免费字段 `getMatchNum` | 免费用完后扣卡（客户端显示逻辑） | 服务端失败形态（本号） |
|---|---|---|---|
| 在线 | `online` | −1 张 | 未实名时优先 **`340 还未实名`**（有 free 也先卡实名） |
| 同城 | `local` | **−2 张** | **`430 匹配卡不足`**（有 1 张不够 2 张） |
| 语音 | `voice` | **−3 张** | `"false"` |
| 视频 | `video` | 同类高消耗 | `"false"` |

本号快照（协议）：`match_card=1`，`online=1,local=0,voice=0,video=0`。

### 6.3 绕过次数限制？（实名暂不讨论）

| 手法 | 结果 |
|---|---|
| 本地改 `match_card` / 免费次数 | 无效，匹配接口服务端校验 |
| `resetMatch` 乱参 | 空响应，次数/卡不变 |
| 未完成任务直接 `receiveHotActivityList` | 不能白嫖加卡 |
| `buyCard` 无效 `card_id`（0/match/999） | 多回显当前库存，**不再涨卡**；合法 id 要乐园币 |
| 协议直打匹配 | 同城/语音仍受卡/次数限制；**不能免费无限匹配** |

**结论：次数与匹配卡以服务端为准；任务是正规涨卡途径（需真实进度）；未发现稳定的「零成本绕过次数/卡」协议洞。同城一次要 2 张卡，语音约 3 张。**

### 6.4 任务进度校验：本地还是服务器？能否伪造完成？

#### 客户端只有 3 个任务相关 HTTP

| 接口 | 参数 | 作用 |
|---|---|---|
| `haveHotActivityList` | `uid` | 红点/文案（有无可领） |
| `createHotActivityList` | `uid` | **拉取任务列表**（含服务端算好的 `progress` / `num` / `available`） |
| `receiveHotActivityList` | **仅 `id`** | 领取奖励 |

客户端 **没有**「上报进度」接口。`HotActivity` UI 逻辑：

```text
if progress == num → 按钮可点「领取」→ receiveHotActivityList(id)
else → 按钮不可点
```

这是 **本地 UI 门禁**；真正发领取请求时 **只带任务 id**，不带 progress。

#### 进度从哪来

- `progress` 由 **`createHotActivityList` 服务端返回**。
- 客户端不做聊天句数/刷动态次数的本地累加再提交。
- 推断：进度在服务端由业务行为旁路累加（发消息、看动态、匹配等），**不是客户端自报**。

#### 伪造探测（本号 YOUR_UID）

| 手法 | 结果 |
|---|---|
| `receive` 只传 id（进度仍 0） | 空 body，**卡不增加** |
| `receive` 附带 `progress=num` / `complete=1` 等 | 空 body，**无效** |
| 猜测接口 `updateHotActivityList` / `taskProgress` / `completeHotActivity`… | empty/false，**不存在或无效** |
| `playOnce` 等试图刷「观看动态」 | 列表里 progress 仍 0 |
| 重拉 `createHotActivityList` | progress 仍服务端原值 |

本号列表曾出现 **`available=已领取` 但 `progress=0`**（状态不一致展示），重复领取仍无回包、**不能再刷卡**。

#### 结论

| 问题 | 答案 |
|---|---|
| 任务完成校验在哪？ | **服务器**（进度与是否可领以服务端状态为准） |
| 客户端校验？ | 仅 **UI**（progress==num 才可点领取） |
| 能否直接伪造完成？ | **目前不能**；无进度上报接口，领取不接受客户端伪造 progress |
| 正规完成方式 | 真实触发对应行为 → 服务端加 progress → `receiveHotActivityList` |

```
真实行为(聊天/刷动态/匹配…)
        │
        v
  服务端累加 progress（客户端无上报 API）
        │
        v
createHotActivityList 读出 progress/available
        │
        v
receiveHotActivityList(id) → 服务端再验是否达标 → 发匹配卡
```

---

## 7. 相关文档

| 文档 | 内容 |
|---|---|
| `08_RP_VERIFY_BYPASS_ANALYSIS.md` | 实名链路与伪造探测 |
| `09_GUEST_CAPABILITY_MATRIX.md` | 游客 L0/L1 能力 |
| `10_PROTOCOL_CLIENT.md` | 协议客户端落地 |
| `api_catalog.json` | 全量 action 列表 |
| `bbw_protocol/README.md` | 使用手册 |
