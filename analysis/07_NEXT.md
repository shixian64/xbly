# 07 · 下一步计划

**最后更新：** 2026-07-15

## 1. 用户侧（进行中）

### 1.1 实名认证（用户自己操作）

- [ ] 在 App 内完成实名（人脸 + 身份证）  
- [ ] 完成后通知继续协议测试  
- [ ] **不要**把真实身份证号写入公共聊天；如需记录，仅本地私密备注

实名相关接口（供对照，**勿伪造他人身份**）：

- `InitFaceVerify0.php`（初始化，假参会 400）
- `DescribeFaceVerify0.php`（查询结果；假 certifyId 也回 `T`，但当前不改库）
- `SaveRPVerifyInfo`（上报；回 `T`，当前不改库）
- `applyManualVerify`（人工审核，非即时）

绕过分析结论见 `08_RP_VERIFY_BYPASS_ANALYSIS.md`：**协议伪造服务端实名尚未打通**。

## 2. 实名完成后立即执行

1. `signin0` 刷新 token，更新 `login_session.json` + `05_ACCOUNT.md`  
2. 确认实名字段（如 `rp_verify_time`）  
3. `resetNum type=昵称`  
4. `resetNew type=昵称设置 value=Vom`  
5. 再登录确认 nickname  
6. **写入 `03_TEST_LOG.md` T05 完整记录**

脚本可直接再跑：

```powershell
python D:\project\AI\bbw\analysis\profile_edit.py
```

（按需把目标昵称写死为 Vom，已是默认）

## 3. 协议深挖优先级

| 优先级 | 项 | 说明 |
|---|---|---|
| P0 | F-001 影响面 | 是否任意手机号都可 SigninOneKeyLogin1；是否有频控/设备绑定 |
| P0 | 协议客户端骨架 | ✅ 已完成 `bbw_protocol` + 通用 call |
| P1 | 补全高频 Form 字段 | 从 jadx 自动抽参生成 typed 方法 |
| P1 | 水平越权 | 固定己 token，改 body 的 userId/authid 读他人资料 |
| P1 | 发帖/评论服务端是否验实名 | 客户端强拦，协议是否仍可写 |
| P1 | VIP 逻辑 | RefundSvipAndVip、优惠券、vip_id 枚举 |
| P1 | withdraw | 提现参数校验（已确认未实名 403） |
| P2 | OSS 签名接口 | 未授权上传 |
| P2 | testField 等调试接口 | 未登录可读（已确认） |
| P2 | IM 冒充 | 本地 UserSig 登录腾讯 IM 的业务影响 |
| P3 | native so | securitydevice / deviceid 是否额外签名 |

## 4. 动态分析（可选）

- Frida 过 `NO_PROXY` 或改包放行代理  
- 对比真机请求与脚本差异（是否缺 Header）  
- Sophix 补丁抓取

## 5. 文档义务（强制）

无论成功失败：

1. `03_TEST_LOG.md` 追加记录  
2. 结论变更 → `04_FINDINGS.md`  
3. 账号字段变更 → `05_ACCOUNT.md`  
4. 新脚本 → `06_TOOLS.md`  

---

## 6. 当前阻塞

| 阻塞 | 负责人 | 影响 |
|---|---|---|
| 账号未实名 | 用户（合法实名） | 无法改昵称等个人信息 |
| 协议伪造实名未通 | 研究中 | Describe/Save 回 T 但不写 rp 状态 |
| 余额 0 | — | 无法测通付费 VIP 正向路径 |

## 7. 实名方向可选实验（实名成功后的差分）

用户合法实名成功后建议立刻：

1. 对比实名前/后登录 JSON 的 `rp_verify_time`  
2. 若方便，记录真实 `certifyId` 形态（可打码）与 Describe 响应是否仍仅 `T`  
3. 执行 T05 改昵称 `Vom`  
4. 更新 03/04/05/08 文档
