# 04 · 漏洞与风险结论

**最后更新：** 2026-07-15  
**范围：** 静态分析 + 已授权测试账号上的协议验证

---

## P0 · 严重

### F-001 · `SigninOneKeyLogin1` 可无验证码登录

| 项 | 内容 |
|---|---|
| 接口 | `do=SigninOneKeyLogin1` |
| 复现 | 仅 POST `userAccount={手机号}`（+ 可本地算的 uniquelogintoken） |
| 结果 | 返回完整用户资料 + `AUTHOR-TOKEN` + `userSign` |
| 影响 | 知道/枚举手机号即可登录对应用户（未校验短信/密码） |
| 证据 | `03_TEST_LOG.md` T02 / T03 |
| 状态 | **已验证** |

### F-002 · 腾讯 IM SECRETKEY 硬编码

| 项 | 内容 |
|---|---|
| 位置 | `GenerateTestUserSig.java` |
| SDKAppID | `1600039823` |
| 影响 | 任意 identifier 可本地生成 7 天 UserSig |
| 工具 | `bbw_client.py usersig` |
| 状态 | **已验证（静态 + 算法实现）** |

### F-003 · 客户端签名体系可完全复现

| 项 | 内容 |
|---|---|
| 涉及 | SIGN-TOKEN / EXPIRE-TOKEN / UNIQUE-LOGIN / AUTHOR-SIGNATURE |
| 问题 | 固定盐、无服务端密钥 |
| 影响 | 拿到 AUTHOR-TOKEN 后可全量脚本化；未登录接口若只验 SIGN/EXPIRE 则可直接访问 |
| 状态 | **已验证** |

---

## P1 · 高

### F-004 · 发短信接口路径暴露

| 项 | 内容 |
|---|---|
| URL | `https://applet.banghua.xin/sms_beibeiwu.php` |
| 问题 | 独立 PHP；客户端仅附弱 Header |
| 实测 | 200 空 body，但用户能收到码（说明可触发） |
| 风险 | 短信轰炸 / 枚举（取决于服务端频控，未充分测） |
| 状态 | **部分验证** |

### F-005 · 改密接口依赖登录态但签名可伪造

| 项 | 内容 |
|---|---|
| 接口 | `do=findpassword` |
| 实测 | 无 token → 700；有 token → 改密成功 |
| 含义 | 改密本身需登录；结合 F-001 可先无码登录再改密 |
| 状态 | **已验证** |

### F-006 · SSL 校验薄弱 + 禁代理

| 项 | 内容 |
|---|---|
| 问题 | 非 banghua 域名 hostnameVerifier 恒 true；banghua 仅查 CN 含 RapidSSL |
| 另 | `Proxy.NO_PROXY` 阻碍系统代理抓包 |
| 状态 | **静态确认** |

### F-007 · VIP 相关接口存在业务滥用面（待深测）

| 项 | 内容 |
|---|---|
| 接口 | `sendVip` / `SvipTry` / `moneyExchangeVip` / `RefundSvipAndVip` |
| 已测 | 余额 0 时 send/exchange 返回「余额不足」；SvipTry 未直接给会员 |
| 待测 | 参数篡改、跨 uid、优惠券、退款逻辑 |
| 状态 | **部分验证** |

### F-008 · 登录响应泄露 password 哈希

| 项 | 内容 |
|---|---|
| 现象 | `UserInfoList.password` 出现在登录 JSON（如 `a8162874...`） |
| 风险 | 哈希泄露；算法未逆向确认 |
| 状态 | **已观察** |

---

## P2 · 中

### F-009 · Sophix 调试模式开启（v148 历史版本）

| 项 | 内容 |
|---|---|
| 代码 | `setEnableDebug(true)` + tags `test` + secret metadata null |
| 风险 | 热修复通道/调试信息面扩大 |
| 状态 | **v148 静态确认；v154 已移除 Sophix 组件** |

### F-010 · 资料修改强制实名

| 项 | 内容 |
|---|---|
| 接口 | `resetNew` |
| 实测 | 未实名 → `403 未实名账号不可修改个人信息` |
| 评价 | 安全控制有效，但与 F-001 形成反差（登录松、改资料紧） |
| 状态 | **已验证** |

### F-011 · OSS 上传签名由服务端代签

| 项 | 内容 |
|---|---|
| 接口 | `getAliyunSignature&content=` |
| bucket | `newecs` |
| 待测 | 是否鉴权不严导致任意上传 |
| 状态 | **静态待测** |

---

## P3 · 低 / 信息

### F-012 · 大量第三方 Key 硬编码

推送 / 广告 / HA / 微信 AppId 等，见 `01_STATIC_ANALYSIS.md`。

### F-013 · assets 嵌套广告包

`408037528` 为穿山甲嵌套包，非业务隐藏逻辑。

### F-014 · 注册页密码字段与真实协议不一致

`SignupActivity` UI 收集密码，但成功路径走短信 + `SigninOneKeyLogin1`，易造成审计误解。

### F-015 · 实名查询/上报接口对客户端回显「T」过于宽松

| 项 | 内容 |
|---|---|
| 接口 | `DescribeFaceVerify0.php`、`SaveRPVerifyInfo` |
| 现象 | 任意/空 certifyId 或随意 result，均返回 `{"code":"200","message":"T"}` |
| 客户端 | `message=="T"` 即视为刷脸成功并**本地**写入 `rp_verify_time` |
| 服务端 | 重登后 `rp_verify_time` 仍为 `0`；`resetNew` 仍 403 |
| 影响 | 可造成「客户端已实名、服务端未实名」状态分裂；若未来服务端误信该回显写库则升级为 P0 |
| 状态 | **已验证（回显宽松 + 未写库）** |
| 文档 | `08_RP_VERIFY_BYPASS_ANALYSIS.md`、T06 |

### F-016 · 改资料实名门禁在服务端（客户端绕过无效）

| 项 | 内容 |
|---|---|
| 接口 | `resetNew` 等 |
| 现象 | 未实名 → `403 未实名账号不可修改个人信息` |
| 含义 | 仅 hook/改 SP 不能改昵称；需服务端实名状态 |
| 状态 | **已验证** |

### F-017 · 未登录可读大量业务配置/列表

| 项 | 内容 |
|---|---|
| 接口 | `getGiftList`、`Tuijiannew`、`getChatCensorship`、`getReferral`、`testField` 等 |
| 现象 | uid=0/token=0 仍返回业务数据或调试文案 |
| 影响 | 信息收集、词表/运营配置暴露、调试接口暴露 |
| 状态 | **已验证** |
| 文档 | `09_GUEST_CAPABILITY_MATRIX.md` T07 |

### F-018 · 未实名登录用户仍可关注

| 项 | 内容 |
|---|---|
| 接口 | `follow` |
| 现象 | L1 游客未实名返回成功；客户端对发帖/加好友等卡实名 |
| 影响 | 实名策略不一致；社交图可被未实名账号污染 |
| 状态 | **已验证** |

### F-019 · 提现与改资料同属服务端实名门禁

| 项 | 内容 |
|---|---|
| 接口 | `withdraw` |
| 现象 | `403 未实名账号不可提现，请实名后再试` |
| 状态 | **已验证** |

---

## 已排除 / 负面结果

| 项 | 结果 |
|---|---|
| 客户端直接改 `user_role` | 失败（无有效接口 / 403 / 空响应） |
| 客户端直接写 `vip/svip` | 失败 |
| 无余额开通 VIP | 失败（余额不足） |
| 无实名改昵称 | 失败（403） |
| 假参数 SaveRPVerifyInfo / DescribeFaceVerify 变实名 | 失败（回 T 但不改 rp_verify_time） |
| 仅客户端 setRp_verify_time | 对 resetNew 无效 |
| flag{...} 字符串 | 静态业务包未发现典型 CTF flag 字面量 |

---

## 修复建议（给开发视角，CTF 记录用）

1. **`SigninOneKeyLogin1` 必须绑定已验证的短信 session / 运营商 token**，禁止仅手机号登录。  
2. IM 密钥严禁下发客户端；UserSig 只服务端签发。  
3. 客户端签名改为 HMAC(服务端密钥) 或直接依赖 HTTPS + 短时 token。  
4. 登录响应去掉 password 哈希。  
5. 短信接口强制图形验证 + 频控 + 业务 token。  
6. 历史 v148 若仍在分发，应关闭 Sophix debug 并配置正确 secret；v154 已移除该组件。
