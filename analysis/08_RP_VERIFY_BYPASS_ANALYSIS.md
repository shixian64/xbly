# 08 · 实名认证绕过面分析

**最后更新：** 2026-07-15  
**性质：** CTF/安全研究 — 协议与信任边界分析  
**约束：** 用户将自行完成合法实名；本文不提供伪造他人身份/人脸材料的操作手册。

---

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| 改资料（如昵称）卡在哪？ | **服务端**校验实名，非纯客户端 |
| 本地改 `rp_verify_time` 有用吗？ | **没用**（重登/服务端接口仍按库里的状态拦） |
| 直接调 `SaveRPVerifyInfo` 能变实名吗？ | **实测不能**（接口常回 `message=T`，但登录后 `rp_verify_time` 仍为 `0`） |
| 直接调 `DescribeFaceVerify0` 假 certifyId？ | **同上**：恒回 `T`，**库状态未变**，`resetNew` 仍 403 |
| 是否存在「协议级一键变实名」？ | **当前测试未打通** |
| 更现实的路径 | ① 用户本人合法人脸实名 ② 人工审核 `applyManualVerify`（非即时） ③ 继续挖服务端是否在某些条件下写库 |

**一句话：**  
客户端实名成功标志可以「看起来很好骗」，但 **改昵称依赖的服务端实名状态**，目前用假参数**没有被改写**。

---

## 2. 实名体系架构

### 2.1 状态字段

| 字段 | 含义（推断） |
|---|---|
| `UserInfoList.rp_verify_time` | `"0"` = 未实名；非 0 多为时间戳或状态码（见下） |
| 本地 SP | `SharedHelper.saveCurrentUserInfo` 会缓存整份 UserInfo |
| UI 展示 | 各 Adapter 用 `rp_verify_time != "0"` 显示已实名图标 |

代码中出现的特殊值：

| 值 | 出现位置 | 含义推断 |
|---|---|---|
| `"0"` | 默认 / 失败 | 未实名 |
| 当前秒级时间戳 | `RPVerifyActivity` 成功回调本地写入 | 人脸通过后的客户端缓存 |
| `"9"` | `MainActivity` 校验身份证号成功后本地写入 | 「已实名需再输身份证确认」类流程 |
| `"3"` | `MainActivity` 判断条件 | 某种需二次确认的状态 |

> 注意：`"9"`/`时间戳` 的**本地写入**不等于服务端已实名。

### 2.2 正式人脸实名流程（客户端）

入口：`RPVerifyActivity`（路由 `/app/rp_verify`）

```
用户输入 certName + certNo
        │
        v
[1] getRPVerifyToken
    POST otherinterface/aliyun/InitFaceVerify0.php
    body: metaInfo(ZIM SDK), certNo, certName
    ← message = certifyId / 认证 token
        │
        v
[2] 阿里云金融级刷脸 ZIMFacade.verify(certifyId)
    成功 code: 1000 或 2006
        │
        v
[3] saveRPVerifyToken  (命名有误导)
    实际 POST DescribeFaceVerify0.php
    body: id, cert_name, cert_no, certifyId
    ← 客户端期望 message == "T"
        │
        v
[4] 仅本地:
    setRp_verify_time(now)
    saveCurrentUserInfo()
    Toast 认证成功
```

失败路径：`saveRPVerifyFail` → 实际打 **`SaveRPVerifyInfo`**，带 `result=错误码+reason`。

### 2.3 人工实名流程

入口：`RPManualActivity`

```
姓名 + 身份证 + ≥3 张图
  → checkAge(certNo)
  → OSS 上传图片
  → applyManualVerify { cert_name, cert_no, result=图片路径 }
  → 「提交成功，请耐心等待，审核结果会以系统消息告知」
```

**非即时生效**；需运营/后台审核。

### 2.4 客户端拦截 vs 服务端拦截

| 场景 | 校验位置 |
|---|---|
| 发帖/匹配/开播申请等 UI | 客户端读 `rp_verify_time=="0"` 弹窗引导实名 |
| **改昵称 `resetNew`** | **服务端返回 403**「未实名账号不可修改个人信息」 |

因此：

- **只 hook 客户端 / 只改内存 / 只改 SP** → 过得了部分 UI，**过不了 resetNew**。  
- 要改昵称，必须让 **服务端认为已实名**。

---

## 3. 攻击面拆解（按信任边界）

### 面 A · 纯客户端绕过（对 resetNew：无效）

| 手法 | 原理 | 对改资料 |
|---|---|---|
| Frida/Xposed 改 `getRp_verify_time()` 恒返回非 0 | 骗 UI | ❌ 服务端仍 403 |
| 改 SP 里缓存的 UserInfo JSON | 同上 | ❌ |
| 在 `RPVerifyActivity` 成功回调处手动 `setRp_verify_time` | 客户端已有此逻辑 | ❌ 不写服务端 |

**结论：** 客户端实名标记 **不可信**；业务关键写操作以服务端为准（至少 `resetNew` 如此）。

### 面 B · 落库接口被客户端「自证成功」（高价值，需验证）

涉及接口：

| 接口 | 客户端用途 | 参数 |
|---|---|---|
| `InitFaceVerify0.php` | 向阿里云申请 certifyId | metaInfo, certNo, certName |
| `DescribeFaceVerify0.php` | 查询刷脸结果 | id, cert_name, cert_no, certifyId |
| `SaveRPVerifyInfo` (i=888) | 失败上报 / 结果落库？ | id, cert_name, cert_no, result |

客户端成功判定极其脆弱：

```java
// RPVerifyActivity: 只要 Describe 返回 message 等于 "T" 就当成功
if ("T".equals(infoList.getMessage())) {
    setRp_verify_time(...); // 仅本地
}
```

### 面 C · 人工审核链路

`applyManualVerify`：提交材料等审核。  
可能的问题（待测，非结论）：审核后台是否校验图与证件一致性、是否可重复提交、消息通知是否可伪造等。  
**不是协议即时绕过。**

### 面 D · 二次身份证确认 `verify_certNo`

`MainActivity`：已处于某实名状态时，要求再输入身份证号，调 `verify_certNo`。  
仅影响本地把 `rp_verify_time` 设为 `"9"` 或清 `"0"`，**不是从未实名变实名的主路径**。

### 面 E · 业务逻辑旁路（不「变成实名」，而是「不需要实名」）

例如：

- 是否所有敏感接口都服务端校验实名？还是只有 `resetNew`？  
- 是否存在旧接口 `resetName` / 其它 profile API 不校验？  
- 未实名是否仍可通过 F-001 登录做其它事？

这是 **绕过「实名门禁的业务需求」**，不是伪造实名状态。

---

## 4. 实测记录（2026-07-15）

脚本：`analysis/rp_verify_probe.py`  
账号：uid `726285`，登录后 `rp_verify_time=0`。

| # | 请求 | 响应摘要 | 重登后 rp_verify_time | resetNew |
|---|---|---|---|---|
| 1 | `SaveRPVerifyInfo` result=`T/F/true/1/PASS/...` | 均 `{"code":"200","message":"T"}` | 仍 `0` | 仍 403 |
| 2 | `DescribeFaceVerify0` certifyId=空/fake/0/T/1000/... | 均 `{"code":"200","message":"T"}` | 仍 `0` | 仍 403 |
| 3 | `InitFaceVerify0` metaInfo=`{}` 假证件 | `400 实名调用失败，请联系客服` | — | — |
| 4 | `checkAge` 假号 | `200 message=726285`（含义待解） | — | — |
| 5 | `applyManualVerify` 假材料 | `200 提交成功，请耐心等待...` | 仍 `0`（即时） | 仍 403 |

### 4.1 关键解读

1. **`DescribeFaceVerify0` / `SaveRPVerifyInfo` 对客户端「成功字面量 `T`」几乎无条件返回**  
   - 客户端会把这当成刷脸成功。  
   - **但服务端用户实名标志未变**（重登可证）。  
   - 推断：这些 PHP 可能  
     - 只回显固定成功结构，**未写库**；或  
     - 写库条件依赖阿里云侧真实核验，假 certifyId 不满足；或  
     - `message=T` 仅表示「接口通了」，真正状态在别字段/别表。

2. **InitFaceVerify 有一定校验**  
   假 meta / 假身份 → 400，说明初始化链路不是完全空壳。

3. **人工通道可提交**  
   假参数也能「提交成功」——审核质量未知；**即时不能用于改昵称**。

4. **resetNew 门禁稳定**  
   多次探测后仍 403，说明当前没有用「假 T」打穿服务端实名。

---

## 5. 可能的绕过路径排序（研究向）

> 下列为 **分析假设**，标注验证状态。不提供伪造真实身份/人脸的步骤。

### P0 假设 · Describe/Save 在某种合法 certifyId 下写库，假 id 不写

- **状态：** 与「假 id 回 T 但不写库」相容  
- **研究点：** 抓一次真实刷脸的 `certifyId`，对比 Describe 请求差异与 DB 效果  
- **CTF 意义：** 判断后端是否「信阿里云查询结果」还是「信客户端」

### P1 假设 · 服务端只校验 token+uid，不校验阿里云（当前证据偏否）

- 若只信客户端上报，假 Describe 应已改变 `rp_verify_time`  
- **实测未改变 → 该假设目前不成立**（至少对当前账号/当前接口版本）

### P1 假设 · 存在其它写实名状态的 do=

- 搜索关键词：`SaveRP` / `setRp` / `rp_verify` / `verify_certNo`  
- 已见：`SaveRPVerifyInfo`、`verify_certNo`、`applyManualVerify`、`checkAge`  
- **待：** 全量 `do=` 中与 verify/auth/realname 相关的未测接口

### P2 假设 · 人工审核弱校验 / 社工后台

- `applyManualVerify` 已可提交  
- 依赖审核人员与后台，**不可协议稳定复现**，且涉及虚假材料时有法律风险 → **不做**

### P2 假设 · 业务接口遗漏实名校验

- 不修改 `rp_verify_time`，但找到 **不检查实名的改资料接口**  
- 已测 `resetName` 空响应且昵称未变；可继续扫 short actions

### P3 · 客户端-only 需求

- 若 CTF 目标只是 App 内显示已实名，hook 即可  
- **与改昵称/服务端资料无关**

---

## 6. 明确不可行 / 不应做的方向

| 方向 | 原因 |
|---|---|
| 使用他人身份证 + 深度伪造人脸 | 违法；本文档禁止 |
| 伪造支付宝/公安身份凭证 | 违法 |
| 攻击阿里云金融级刷脸服务本身 | 超出本 APK CTF 范围且可能违法 |
| 仅改客户端显示为已实名后指望 resetNew | 已证伪 |

---

## 7. 与现有漏洞的关系

| 漏洞 | 与实名关系 |
|---|---|
| F-001 无验证码登录 | **不依赖实名**即可拿 token，扩大后续探测面 |
| F-003 签名可伪造 | 便于脚本化打实名相关接口 |
| resetNew 403 | 证明 **资料写操作有服务端实名门禁**（相对登录反而更严） |

有趣对比：

- **登录门禁极弱**（F-001）  
- **改资料门禁较强**（服务端实名）  
- **实名结果回传字面量极弱**（恒 `T`），但 **未转化为服务端状态伪造**（当前）

---

## 8. 推荐后续实验（合法、可记录）

1. **用户本人完成一次真实人脸实名**（你已计划自行操作）  
   - 抓包或日志记录：Init → certifyId → Describe 真实响应  
   - 对比实名前/后 `rp_verify_time`、`getUserAttributesMe1`  
   - 再打 `resetNew` 改 `Vom`  
   - 写入 `03_TEST_LOG.md` T05  

2. **差分分析**  
   - 实名成功前后，Describe/Save 响应 body 是否不仅是 `T`  
   - 是否有额外 cookie / 服务端 session  

3. **接口枚举**  
   - 对 `verify*` / `*RP*` / `*cert*` / `*real*` 类 do= 做只读探测  

4. **门禁覆盖面**  
   - 未实名状态下，哪些写接口 403、哪些放行（水平越权与业务绕过）  

---

## 9. 代码索引

| 文件 | 作用 |
|---|---|
| `Main5Branch/RPVerifyActivity.java` | 人脸实名主流程 |
| `Main5Branch/RPManualActivity.java` | 人工实名 |
| `OkHttpInstance.getRPVerifyToken` | InitFaceVerify0.php |
| `OkHttpInstance.saveRPVerifyToken` | DescribeFaceVerify0.php |
| `OkHttpInstance.saveRPVerifyFail` | SaveRPVerifyInfo |
| `NicknameEditActivity` / `resetNew` | 改昵称（服务端查实名） |
| `MainActivity` verify_certNo | 二次身份证确认 |

---

## 10. 文档同步清单

- [x] 本文 `08_RP_VERIFY_BYPASS_ANALYSIS.md`  
- [x] `03_TEST_LOG.md` 追加 T06  
- [x] `04_FINDINGS.md` 追加 F-015 / F-016  
- [x] `07_NEXT.md` 更新实名相关待办  
- [x] `README.md` 索引  
- [x] `06_TOOLS.md` 登记 `rp_verify_probe.py`
