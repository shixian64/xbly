# 两个 APK 客户端静态安全审计报告

**日期：** 2026-07-16
**样本：** `beibeiwu.apk`（v148）、`xbly.apk`（v154）
**范围：** 仅 APK 客户端离线静态分析；未访问、探测或修改 Web/API；未安装、未运行、未编译。
**说明：** 报告中的源码行号对应本轮 JADX/Manifest 解码结果。第三方密钥只记录存在性与用途，不保存完整原值。

---

## 1. 结论

v154 已移除 Sophix、关闭 Android 备份、删除 BootReceiver 与 EPUB 小说模块，但两个版本仍共享多项高风险缺陷。当前最应优先处理的是：

1. 任意 URL 会进入业务 WebView，且 WebView 把完整 `AUTHOR-TOKEN` 发给目标站点。
2. 无权限导出的任意 URL WebView 会自动批准网页摄像头、麦克风请求。
3. 腾讯 IM UserSig 服务端 HMAC 密钥被完整打包，可为任意用户生成 7 天签名。
4. 主业务网络主机名校验失效；HFOpen 音乐 SDK 的业务可达网络链为完整 Trust-All。
5. Meihu 贴纸 ZIP 解压存在 Zip Slip，且未发现资源级独立发布签名；但静态代码未找到 `MHSDK.init(Context,key)`，正常运行时可达性仍需确认。若该链启用且资源后台/CDN/响应被控制，可越界覆盖应用私有文件。
6. SudMGP/Egret 从网络取得 JAR/SO，缺少独立发布者签名，解压存在 Zip Slip，之后使用 `DexClassLoader` 动态加载。
7. v148 额外泄露 Sophix 应用密钥及有效 RSA 私钥，并存在用户可导入 EPUB 的 Zip Slip。
8. 微信 OAuth 使用固定 `state` 且回调不校验；客户端辅助签名、防重放值均可离线伪造。
9. 生产日志、URL、本地明文存储和大量导出组件继续扩大令牌、隐私、崩溃及权限代理风险。

**总体判断：** 两个 APK 都不应把客户端本身视作可信边界。v154 只是减少了部分旧组件，并未消除最关键的会话泄露、权限代理、第三方根密钥、TLS 和动态资源供应链问题。

---

## 2. 样本与攻击面基线

| 项目 | `beibeiwu.apk` | `xbly.apk` |
|---|---:|---:|
| 版本 | 148 | 154 |
| 包名 | `xin.banghua.beiyuan0` | 同左 |
| SHA-256 | `abc76819a11d54f794fbc83f36580c3309fba44c99372072c1f328fa0838a4d0` | `96d38c8795fab8c72b307393bb240276b56081f9351fed11290643aa9dc32f7e` |
| 文件大小 | 265,596,830 bytes | 305,734,322 bytes |
| min/target/compile SDK | 24 / 30 / 33 | 24 / 30 / 34 |
| Application | `SophixStubApplication` | `App` |
| DEX / arm64 SO | 12 / 63 | 11 / 62 |
| 组件总数 | 503 | 492 |
| 导出组件 | 52 | 51 |
| 无组件权限保护的导出组件 | 42 | 40 |
| uses-permission | 72 条，71 个唯一值 | 63 条，63 个唯一值 |
| `allowBackup` | `true` | `false` |
| 明文流量 | 全局允许 | 全局允许 |

组件构成：

| 版本 | Activity | Service | Receiver | Provider | 导出 Activity / Service / Receiver / Provider |
|---|---:|---:|---:|---:|---:|
| v148 | 394 | 47 | 22 | 40 | 26 / 10 / 15 / 1 |
| v154 | 384 | 46 | 22 | 40 | 26 / 9 / 15 / 1 |

两版使用相同签名证书。APK v2 签名校验有效；minSdk=24 与 v2-only 匹配，因此不把“只有 v2 签名”误报为可篡改漏洞。

---

## 3. 风险总表

| ID | 问题 | v148 | v154 | 等级 | 状态 |
|---|---|---|---|---|---|
| APK-01 | 任意 URL WebView 泄露 `AUTHOR-TOKEN` | 是 | 是 | 严重 | 代码链确认 |
| APK-02 | 导出 WebView 自动批准摄像头/麦克风 | 是 | 是 | 严重 | 代码链确认，媒体采集待动态复现 |
| APK-03 | 腾讯 IM UserSig 服务端密钥硬编码 | 是 | 是 | 严重 | 密钥与离线签发能力确认，有效性待平台确认 |
| APK-04 | 主业务网络 HostnameVerifier 失效 | 是 | 是 | 高 | 确认 |
| APK-05 | HFOpen 音乐 SDK 完整 Trust-All | 是 | 是 | 高 | 确认且业务可达 |
| APK-06 | Meihu 贴纸资源 Zip Slip | 是 | 是 | 高，条件性 | 解压缺陷确认；SDK初始化与内容控制条件待验证 |
| APK-07 | SudMGP/Egret 无独立签名的动态 JAR/SO 加载 | 是 | 是 | 高，条件性 | 下载、解压、加载链确认 |
| APK-08 | Sophix 密钥、RSA 私钥泄露 | 是 | 否 | 高，平台侧可达时严重 | v148 确认 |
| APK-09 | EPUB 导入 Zip Slip | 是 | 否 | 高 | v148 用户输入链确认 |
| APK-10 | 微信 OAuth 固定且未校验 `state` | 是 | 是 | 中高，条件性 | 客户端缺陷确认 |
| APK-11 | 客户端签名和防重放值可预测 | 是 | 是 | 中高，条件性 | 算法确认，服务端后果待验证 |
| APK-12 | Token 放入 URL并交给外部浏览器 | 是 | 是 | 中高 | 确认 |
| APK-13 | 生产环境敏感日志 | 是 | 是 | 中高 | 确认 |
| APK-14 | 会话和完整用户对象明文落盘 | 是 | 是 | 中 | 确认，v148 备份风险更大 |
| APK-15 | 大量无权限导出组件及确定性崩溃入口 | 是 | 是 | 中 | 多条代码链确认 |
| APK-16 | 全局允许明文流量、WebView 混合内容 | 是 | 是 | 中高 | 配置确认，APK-01 有实际链 |
| APK-17 | FileProvider 路径过宽 | 是 | 是 | 中，条件性 | 危险配置确认，完整 grant 链未确认 |
| APK-18 | RoomKit `BusinessToken` 固定在客户端 | 是 | 是 | 中，条件性 | 使用确认，服务端赋权待验证 |
| APK-19 | 隐私同意前可能上报页面和设备标识 | 是 | 是 | 中，合规 | 缺少统一 consent gate |
| APK-20 | 自定义 scheme、更新链、过宽权限和旧 Native 栈 | 是 | 是 | 低至中/待核查 | 加固项 |

---

# 4. 严重与高危问题

## APK-01：任意 URL WebView 向目标站点泄露 `AUTHOR-TOKEN`

**影响版本：** v148、v154
**等级：** 严重
**置信度：** 高，完整输入到网络请求链已确认

### 证据链

1. `cn.leyuan.base_library.utils.CommonUtil.scanResult()` 接受二维码内容：
   - 只要字符串以 `http` 开头且不是 `https://www.xiaobei.love`，就原样进入 ARouter `/base/web_view`。
   - 不符合内部二维码格式的其他长字符串，最终也可能进入同一路由。
   - v154 代表行：`CommonUtil.java:606-639`。
2. ARouter 生成表把 `/base/web_view` 映射到：
   - `cn.leyuan.base_library.SliderWebViewActivity`。
3. 该 Activity 对传入 URL执行：

```java
Map<String, String> headers = new HashMap<>();
headers.put("AUTHOR-TOKEN", CommonUtil.getInstance().getAuthorToken());
webView.loadUrl(url, headers);
```

   - v154 代表行：`SliderWebViewActivity.java:191-214`。
4. 论坛帖子、轮播图、图片、系统消息等服务端返回字段也会把 `share_url`/`web_url` 原样送入该 WebView：
   - `LuntanAdapter.java:582-610`
   - `LuntanSliderAdapter.java:577-605,765-778`
   - `PictureAdapter.java:129-132`
   - `SystemMessageFragment` 的 `web_url`。
5. WebView 同时允许 JavaScript、DOM Storage 和 `MIXED_CONTENT_ALWAYS_ALLOW`。

注意：App 内固定的 `www.chengzijianzhan.com` 页面显式启动的是另一套 `xin.banghua.beiyuan0.SliderWebViewActivity`，该类只调用 `loadUrl(url)`，没有附加 `AUTHOR-TOKEN`；因此不把该固定第三方入口作为 Token 泄露证据。已确认泄露的是 ARouter `/base/web_view` 映射的 `cn.leyuan.base_library.SliderWebViewActivity` 链。

### 可利用场景

- 设备为 Android 9/API 28 或更高。该 Activity 在 `SDK_INT > 27` 且页面名不是“新版本/我的语音库”时才进入带 Header 的 WebView；Android 7/8 分支改用外部浏览器，不走本条 Header 泄露链。
- 已登录用户扫描攻击者制作的二维码。
- 用户点击攻击者可控制或被污染的论坛分享、轮播图、系统消息链接。
- URL 使用 `http://` 时，令牌不仅到达目标站点，还会以明文经过网络。

`loadUrl(url, headers)` 的额外 Header主要用于首个主文档请求，不代表所有子资源都会携带；但攻击者控制的首个 HTTP(S) 服务已经可以直接读取完整 `AUTHOR-TOKEN`，足以构成会话泄露。

### 影响

- 账号会话被窃取和复用。
- 攻击者可模拟官方客户端调用用户接口。
- 可能读取用户资料、私聊、位置、房间或资产信息，并执行账号操作。
- 实际权限上限取决于服务端 Token 的作用域、有效期、设备绑定和撤销机制，需后续 API 审计确认。

### 修复

1. WebView 只允许明确列出的自有 `https` 域名、端口和路径；使用 URI 解析后做精确匹配，禁止 `contains()`/`startsWith()` 式域名判断。
2. 第三方 URL一律交给系统浏览器，且绝不附加业务 Token。
3. 不要把主会话 Bearer Token传给网页；确需 Web SSO 时，由服务端签发一次性、短期、限定 audience/path 的换票。
4. 所有重定向和 `shouldOverrideUrlLoading` 再次执行同一白名单检查。
5. 立即评估已泄露 Token 的撤销、轮换和异常使用检测。

---

## APK-02：导出的任意 URL WebView 自动批准摄像头、麦克风

**影响版本：** v148、v154
**等级：** 严重
**置信度：** 危险调用链高；真实 `getUserMedia()` 采集需按系统 WebView版本动态确认

### 证据链

Manifest 中：

```text
cn.rongcloud.profile.webview.ActCommentWeb
```

- 存在 `io.rong.intent.action.commonwebpage` intent-filter。
- targetSdk=30 下未显式 `exported` 但因 filter 实际导出。
- 无 `android:permission` 保护。

Activity：

- `ActCommentWeb.init()` 从外部 Intent读取 `key_basis`。
- `key_basis` 直接作为 `loadUrl`，无 scheme、host、port、path 白名单。
- 代表行：`ActCommentWeb.java:35-45`。

`RCWebView` 默认开启：

- JavaScript、DOM Storage、数据库缓存。
- `setAllowContentAccess(true)`。
- `setAllowFileAccess(true)`。
- `setAllowFileAccessFromFileURLs(true)`。
- `setAllowUniversalAccessFromFileURLs(true)`。
- `setMixedContentMode(MIXED_CONTENT_ALWAYS_ALLOW)`。

最关键的是默认 `WebChromeClient`：

```java
public void onPermissionRequest(PermissionRequest request) {
    if (customClient == null) {
        request.grant(request.getResources());
    }
}
```

- 代表行：`RCWebView.java:42-51,85-89`。
- `ActCommentWeb` 没有安装自定义 ChromeClient，因此走自动 grant 分支。

### 利用条件

1. 恶意本地 App 显式启动该 Activity，并传入攻击者 HTTPS 页面。
2. 小贝乐园之前已获得 Android 层的 `CAMERA` 或 `RECORD_AUDIO` 权限。
3. 网页调用 WebRTC `getUserMedia()`。

现代 Android 对后台直接拉起 Activity 有限制，实际触发通常还要求攻击 App位于前台或结合用户点击；这不改变组件无权限导出和权限代理代码链本身。

如果宿主尚未取得系统运行时权限，WebView 的 `grant()` 不能替代 Android 权限授予；但该 App 本身具有视频、语音、直播功能，用户事先授权的概率较高。

### 影响

- 恶意网页借宿主 App权限采集摄像头和麦克风数据。
- App 成为本地恶意应用的权限代理/confused deputy。
- file URL 跨域、content access 和混合内容设置扩大旧 Android/WebView 上的本地文件读取、脚本注入和网络降级面。
- 最坏导致私密音视频、令牌、缓存文件或用户隐私泄露。

### 修复

- 若无需跨应用调用，`ActCommentWeb` 明确设置 `android:exported="false"`。
- 必须跨应用时使用自有 signature 权限，并校验调用 UID、包名与签名。
- URL仅允许固定可信 HTTPS origin。
- `onPermissionRequest()` 默认 `deny()`；只有固定 origin、明确用户操作、明确资源类型时才逐项授权。
- 关闭 file URL跨域、非必要 file/content access 与混合内容。

---

## APK-03：腾讯 IM UserSig 服务端密钥硬编码

**影响版本：** v148、v154
**等级：** 严重；取决于密钥当前是否仍有效
**置信度：** 密钥、算法和任意 identifier 离线签发能力已确认

### 证据

类：

```text
xin.banghua.beiyuan0.TencentIM.signature.GenerateTestUserSig
```

包含：

- SDKAppID：`1600039823`
- 完整 HMAC `SECRETKEY`，本报告已脱敏
- `EXPIRETIME = 604800`，即 7 天
- `genTestUserSig(identifier)` 可对任意 identifier 生成 UserSig

v154 代表行：`GenerateTestUserSig.java:13-20`。应用实际使用相同 SDKAppID 初始化并登录 TIM：`LaunchActivity.java:521,804`。

当前业务登录似乎从服务端响应取得 UserSig，而不是直接调用测试生成函数；这并不能降低密钥泄露风险，因为任何取得 APK 的人都可以复用内置实现自行签名。

### 影响

如果密钥仍有效，攻击者可能：

- 冒充任意 TIM identifier 登录。
- 发送消息、骚扰用户、加入或操作群组。
- 根据腾讯 IM 权限配置读取会话或群组数据。
- 批量创建机器人并消耗配额、费用。

业务用户 ID 与 TIM identifier 的映射、历史消息权限和群权限需要平台侧确认。

### 修复

1. 立即在腾讯云控制台吊销并轮换该密钥，不要只发新版 APK。
2. UserSig 只能由服务端/HSM 基于当前业务会话签发。
3. 使用短有效期，并绑定用户、设备风险和当前登录事务。
4. 审计异常 identifier、批量登录、群操作及历史消息访问。

---

## APK-04：主业务网络 HostnameVerifier 失效

**影响版本：** v148、v154
**等级：** 高
**置信度：** 高

### 证据

两个网络工具存在相同逻辑：

- `cn.leyuan.base_library.utils.OkHttpInstance.getInstance()`
- `com.basis.OkHttpInstance.getInstance()`

核心逻辑：

```java
if (!hostname.contains("banghua.xin")) {
    return true;
}
// 对 banghua.xin 也没有调用标准 SAN/CN 域名校验，
// 只遍历证书链 subject，查找包含 RapidSSL 的 CN。
```

v154 代表行：

- `cn/.../OkHttpInstance.java:173-199`
- `com/basis/OkHttpInstance.java:47-73`

### 准确影响边界

- 默认系统 TrustManager 仍会校验证书链，因此主业务栈不是“接受任意自签名证书”。
- 但对非 `banghua.xin` 域，只要攻击者提供任意系统信任 CA 签发的证书，即使证书域名完全不匹配请求主机，也会通过自定义 HostnameVerifier。
- 对包含 `banghua.xin` 的域，也没有验证叶子证书 SAN/CN；无关域名但证书链 subject 含 RapidSSL 的证书可能被接受。

### 已确认的敏感组合

微信流程调用：

```java
OkHttpInstance.startHttp(
    null,
    "https://api.weixin.qq.com/sns/userinfo?access_token=...&openid=...",
    callback
);
```

而通用 `startHttp()` 对任何完整 URL仍统一附加：

- `AUTHOR-TOKEN`
- `EXPIRE-TOKEN`
- `SIGN-TOKEN`

代表行：

- `WXEntryActivity.java:223-233`
- `OkHttpInstance.java:13479-13524`

因此微信官方接口会收到这些 Header 的当前值；首次登录时其中部分值可能为空，但已登录账号执行微信绑定等流程时可能包含完整会话值。若网络攻击者利用错误主机名校验劫持 `api.weixin.qq.com`，还可能同时取得微信 access token、openid 与非空的小贝乐园会话头，并伪造微信用户信息响应。

### 修复

- 删除自定义 HostnameVerifier，恢复 OkHttp 默认 RFC 2818/6125 域名校验。
- 不得通过解析证书 Subject DN 自行验证域名或 CA 品牌。
- 如需 pinning，使用 OkHttp `CertificatePinner` 固定受控域公钥，并准备轮换与备用 pin。
- 通用请求函数不得向第三方域附加首方认证头；按 origin 分离网络客户端和拦截器。

---

## APK-05：HFOpen 音乐 SDK 完整 Trust-All

**影响版本：** v148、v154
**等级：** 高
**置信度：** 高，初始化和业务入口可达

### 证据

`com.hfopen.sdk.net.LiveRetrofitFactory.initClient()` 同时配置：

```java
.hostnameVerifier(new RxUtils.TrustAllHostnameVerifier())
.sslSocketFactory(RxUtils.createSSLSocketFactory(), new TrustAllCerts())
```

- `TrustAllHostnameVerifier.verify()` 恒为 `true`。
- `TrustAllCerts.checkServerTrusted()` 为空。
- `RxUtils.createSSLSocketFactory()` 使用 Trust-All Manager。
- v154 代表行：`LiveRetrofitFactory.java:86-90`。

业务可达链：

```text
App.initConfig
→ MusicInit
→ HFOpenApi.registerApp
→ MusicControlManager
→ HFOpen ServiceImpl/DataRepository
→ LiveRetrofitFactory
```

`GameRoomActivity` 等会调用 `MusicControlManager.showDialog()`，不是仅存在未使用的 SDK 类。

### 影响

同网段、恶意热点、代理或 DNS/路由攻击者可使用任意证书：

- 读取或篡改音乐 SDK Token、请求参数、播放统计。
- 替换音乐元数据、试听/播放 URL，并把后续下载引向攻击者控制的位置。
- 音频文件随后由另一套 `cn.rongcloud.corekit.net.oklib.OkApi` 下载，不应直接表述为“媒体二进制本身也经 HFOpen Trust-All 下载”；但若后续下载接受被篡改的 URL，恶意媒体仍可能进入播放器或 Native 解码栈，需动态验证。
- 污染订单、授权或报表类 SDK 请求，具体业务后果取决于 HFOpen 服务端校验。

### 修复

升级或修补 SDK，使用平台默认 TrustManager 和 HostnameVerifier；将所有 Trust-All 类从生产包删除，并对音乐 API、媒体 URL和重定向分别验证 HTTPS origin。

---

## APK-06：Meihu 贴纸资源缺少独立签名，解压存在 Zip Slip

**影响版本：** v148、v154
**等级：** 高，条件性；与可执行文件覆盖组合时可能达到严重
**置信度：** 解压缺陷高；SDK初始化、正常下载可达性和攻击者控制资源内容的现实条件需验证

### 证据链

#### 1. 贴纸 ZIP 的实际下载链

```text
LiveRoom/Beauty UI（链启用时）
→ MhDataManager.downloadTieZhi
→ MHSDK/MHBeautyManager.downloadSticker
→ com.meihu.beautylibrary.utils.DownloadUtil.download
→ Kalle.post(resource)
```

- `com.meihu.beautylibrary.utils.DownloadUtil.java:53-58`
- `MHBeautyManager.java:876-877`

Kalle 在 `C9321b.m18807a()` 中加载内置 `assets/server.cer`，并使用 `C9322c` 校验 CN/SAN 主机名。当前证据不支持“Meihu 贴纸下载走 Trust-All OkGo”这一结论。

APK 中确实包含 `com.lzy.okgo.OkGo` 的 Trust-All 默认实现，以及另一套 `com.meihu.beauty.utils.DownloadUtil` 的 OkGo 包装；但未找到该包装的实际业务调用。`MhDataManager` 只调用了 `OkGo.cancelTag()`。因此本报告不把这套 OkGo 定义作为已确认的活跃下载漏洞。

此外，静态 Java/Smali 调用扫描没有找到 `MHSDK.init(Context,key)`。`MhDataManager.create()` 虽会进入贴纸列表逻辑，但 SDK 在 app_key 为空时可能提前返回。因此“正常用户操作一定能触发下载”尚未证实；仍需通过运行态、反射/JNI调用或抓包确认初始化是否发生。

即使传输层使用证书文件，仍未发现贴纸 ZIP 自身的独立发布者签名或由已签名清单保护的文件哈希。服务端响应、资源后台、证书对应私钥、CDN或发布流程被控制时，恶意归档仍会被接受。

#### 2. 解压存在 Zip Slip

下载完成后：

```java
File output = new File(destination, zipEntry.getName());
new FileOutputStream(output);
```

没有拒绝绝对路径、`..`、符号链接，也没有 canonical path 前缀校验：

- `MHBeautyManager.java:623-640`
- `com.meihu.beautylibrary.utils.FileUtil.java:67-107`

### 可利用条件

攻击者需要控制或污染贴纸资源内容，例如：

- 贴纸后台/资源发布账号被接管。
- 业务响应中的 resource URL 可被服务端漏洞篡改。
- CDN、对象存储或证书私钥失陷。
- Kalle/证书装载链另有尚未确认的绕过。

同时需要该 SDK 在运行时已正确初始化并实际执行下载。当前不能把普通同网段攻击者、单纯 DNS 劫持或普通用户操作直接写成已确认利用条件。

### 影响

- 越界覆盖应用 UID 可写的私有文件、缓存、配置或数据库。
- 破坏会话、业务配置，造成持续崩溃或磁盘耗尽。
- 若覆盖后续会被动态加载的 JAR/SO、Web 资源或其他可信输入，可能升级为应用 UID 代码执行。
- 该升级依赖目标文件路径和后续加载时机；静态结果不应直接写成“已确认远程 RCE”。

### 修复

1. 保留严格 TLS和主机名校验，并确认 `server.cer` 的轮换、有效期和失败关闭行为。
2. 对资源清单和每个 ZIP使用应用内置公钥验证发布者签名；哈希值必须来自已签名清单，不能与文件同源裸传。
3. 解压前计算 canonical path，并要求：

```text
outputCanonical == destCanonical
或 outputCanonical 以 destCanonical + File.separator 开头
```

4. 拒绝绝对路径、`..`、链接和异常文件类型。
5. 限制条目数、单文件大小、总解压大小和压缩比，使用临时目录、原子替换。

---

## APK-07：SudMGP/Egret 下载并动态加载 JAR/SO，缺少独立签名

**影响版本：** v148、v154
**等级：** 高，条件性
**置信度：** 下载、解压、安装和加载链高；普通攻击者能否控制内容源需进一步验证

### 证据链

1. SDK 从网络元数据指定 URL下载 ZIP。
2. 未发现基于应用内置发布公钥的独立代码签名验证。
3. 解压直接以 `new File(dest, entry.getName())` 生成输出路径，存在 Zip Slip。
4. 解压后复制或安装：
   - `egret-dex.jar`
   - `libegret.so`
5. 安装判断主要基于文件存在和大小，不构成来源真实性验证。
6. `org.egret.wx.b` 使用 `DexClassLoader` 加载 `egret-dex.jar` 中的类，并提供 Native library path。
7. `GameRoomActivity` 在两版均存在且可达，v154 仍未移除该 SDK。

### 影响边界

在游戏后台、元数据接口、CDN、下载 URL或归档内容被控制时：

- Zip Slip 可越界写入应用目录。
- 恶意 JAR在应用 UID 中执行 Java 代码。
- 恶意 SO可执行 Native 代码。
- 攻击代码可读取会话、聊天、位置和媒体，并借用宿主已有权限。

当前证据不能证明普通远程攻击者无需控制内容源即可直接 RCE，因此定性为“供应链/内容源被控时的应用 UID 代码执行链”。

### 修复

最佳方案是停止从网络动态加载 DEX/JAR/SO，并把代码随签名 APK/AAB发布。若业务上无法立即移除：

- 使用离线/HSM 保存的发布私钥签名清单及每个文件。
- 客户端只内置公钥，并验证包名、版本、文件哈希、有效期和防回滚计数。
- 签名验证必须先于解压和加载，失败时 fail closed。
- 修复 Zip Slip，限制容量并原子安装。

---

## APK-08：v148 Sophix APPSECRET/IDSECRET 与 RSA 私钥泄露

**影响版本：** 仅 v148
**等级：** 高；平台错误信任客户端密钥时可达到严重
**置信度：** 密钥存在和 SDK使用确认；恶意补丁 RCE 未确认

### 证据

v148 Manifest 包含：

- Sophix `IDSECRET`
- Sophix `APPSECRET`
- Sophix `RSASECRET`

其中 `RSASECRET` 已离线验证为有效的 2048-bit PKCS#8 RSA 私钥。Sophix 代码会读取这些 metadata，并用 RSA 私钥和 AES链解密补丁。完整原值未写入本报告。

另外：

- `setEnableDebug(true)`。
- 打开完整日志。
- 补丁 tag 使用 `test`。
- `com.taobao.sophix.aidl.DownloadService` 导出且无权限保护。
- Binder 接口可接收 URL、普通路径和 `content://` URI。

### 重要限制

Sophix 代码明确存在 release 模式禁止本地补丁加载的保护，因此不能仅凭导出 AIDL Service认定“任意本地 App 已可加载补丁并 RCE”。

### 影响

- 任意取得旧 APK 的人均可提取平台密钥和私钥。
- 热补丁机密性失效；可解密攻击者能够获得的补丁、风控和未发布逻辑。
- 可模拟客户端查询或滥用平台接口、配额。
- 导出 Service 可被用于未授权 IPC、查询、下载、复制或资源消耗。
- 如果补丁平台/服务端错误地把这些客户端密钥视为发布或投递授权，最坏可形成供应链级恶意补丁执行。

v154 移除 Sophix 不会自动使旧密钥失效；旧 APK仍可被下载和分析。

### 修复

- 立即在 Sophix/阿里云平台吊销并轮换所有旧凭据和密钥对。
- 审计历史补丁发布、灰度范围、查询和下载日志。
- 强制淘汰 v148；服务端拒绝旧版本访问敏感更新能力。
- 补丁只接受离线发布私钥签名，客户端只保留公钥。
- 导出 Service 改为非导出或挂 signature 权限，并校验 Binder 调用 UID。

---

## APK-09：v148 用户导入 EPUB 时存在 Zip Slip

**影响版本：** 仅 v148
**等级：** 高
**置信度：** 高，用户输入到解压写文件链已确认

### 证据链

1. `BookshelfFragment.onActivityResult()` 接受用户选择的 `.epub` 文件。
2. 只按扩展名和约 100MB 文件大小做检查。
3. `BookshelfModel.unZipEpub()` 调用 `EpubUtils.unZip()`。
4. `EpubUtils.unZip()` 直接执行：

```java
File out = new File(dest + "/" + zipEntry.getName());
new FileOutputStream(out);
```

无 canonical path 校验：

- `BookshelfFragment.java:197-242`
- `BookshelfModel.java:44-70`
- `EpubUtils.java:28-59`

目标目录位于应用私有 `files/epubFile`，恶意条目可以用多级 `../` 逃逸。

### 影响

- 覆盖 SharedPreferences、数据库、缓存或其他应用私有文件。
- 造成账号状态污染、持续崩溃、数据破坏。
- 如果覆盖后续会加载的动态 JAR/SO或可信资源，可能形成条件性代码执行组合链。
- 恶意 EPUB/压缩炸弹还可导致 CPU、磁盘和内存 DoS。

v154 已移除小说/EPUB模块，但仍需处理存量 v148。

### 修复

除通用 Zip Slip修复外，应校验 EPUB MIME、结构、条目数、解压总量和压缩比；在隔离临时目录解析后再复制允许的内容文件，禁止归档直接写入业务私有根目录。

---

# 5. 中高危与架构性问题

## APK-10：微信 OAuth 固定 `state` 且回调未校验

**影响版本：** v148、v154
**等级：** 中高，条件性
**置信度：** 客户端缺陷高；账号绑定/登录后果需服务端确认

### 证据

多个授权入口固定使用：

```text
state = "wechat_sdk_demo_test"
```

例如：

- `Signin/LoginSmsActivity`
- `Signin/SigninActivity`
- `Main5Branch/SettingFragment`

导出的 `WXEntryActivity.onResp()`：

- 只判断响应类型。
- 直接读取 `SendAuth.Resp.code`。
- 不比较返回 `state`。
- 不检查并消费本地待完成授权事务。
- 不把回调绑定到发起页面、当前账号或一次性 nonce。

v154 代表行：`WXEntryActivity.java:188-215`。

### 可能影响

恶意本地 App如果能够获得同一微信 AppID 下仍有效、未使用的授权 code，并构造 SDK可接受的回调 Intent，可能实施：

- 登录 CSRF。
- 把攻击者微信绑定到受害者当前账号。
- 强制客户端切换到错误身份。
- 污染账号绑定关系。

能否完成完整回调注入以及 `weinxinregister0`、`/user/resign` 的最终语义需动态和服务端联合确认。

### 修复

- 每次授权生成高熵、一次性 state，安全保存并在回调严格比较、一次消费。
- state同时绑定当前账号、用途（登录/绑定）、发起时间和页面事务。
- 服务端也必须验证 state/nonce/当前会话；账号绑定要求二次认证和明确确认。
- 不要把固定字符串当 CSRF保护。

---

## APK-11：客户端辅助签名和防重放值可完全离线生成

**影响版本：** v148、v154
**等级：** 中高，具体业务影响取决于服务端
**置信度：** 算法确认

APK 内可恢复的算法包括：

```text
SIGN-TOKEN = MD5(uid + "socialchat" + uid)

EXPIRE-TOKEN = SHA1("xiaobei" + epochSeconds) + epochSeconds

uniquelogintoken = SHA1("xiaobei" + uid)

AUTHOR-SIGNATURE = SHA1(SHA1(uid) + nonce + timestamp)
nonce = Random.nextInt(10000)
```

代表位置：

- `MD5Tool`
- `CommonUtil.getExpireToken()`
- `Uniquelogin.saveToken()`
- `OkHttpInstance.addHeader()`

这些常量和算法对所有客户端公开，没有服务端秘密；nonce 只有 10,000 种可能，也不能构成可靠重放保护。

### 影响

- 第三方可以编写与官方 APK同格式的自动化客户端。
- 可用于爬取、机器人、批量请求、刷消息和资源消耗。
- 如果服务端把这些字段当身份认证、对象授权或交易签名，可能进一步产生 IDOR、重放、赠礼/购买/提现等业务伪造。

当前 APK静态分析不能证明服务端确实仅依赖这些字段，因此不直接认定登录或交易绕过。

### 修复

这些问题主要必须在服务端修复：授权依赖短期会话、服务端签发 nonce、一次性幂等键、对象级权限和服务端金额计算。客户端公开算法只能作为版本兼容字段，不能作为安全边界。

---

## APK-12：Token 被放入 URL，并交给外部浏览器/Handler

**影响版本：** v148、v154
**等级：** 中高
**置信度：** 高

已确认示例：

```text
...Voice_play?...&token=<AUTHOR-TOKEN>
.../user_audio/#/?token=<AUTHOR-TOKEN>
```

- `MatchActivity.java:593`
- `MeNewFragment.java:634`

“我的语音库”使用 `slidername="我的语音库"`，`SliderWebViewActivity` 会调用：

```java
startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
```

即把含完整 Token 的 URI交给外部浏览器或其他已注册 URL handler。fragment 中的 Token通常不发到 HTTP服务器，但仍对浏览器进程、页面 JavaScript、历史记录、监控 SDK及恶意默认 Handler可见；query 中的 Token还会进入服务器、代理和访问日志。

### 修复

- Bearer Token不得出现在 query、fragment、文件名或 Intent URI。
- Web SSO使用一次性短期 code，服务端换票后立即失效。
- 必须指定可信接收包时使用显式 Intent，并仍避免携带主会话 Token。

---

## APK-13：生产环境输出大量敏感日志

**影响版本：** v148、v154
**等级：** 中高
**置信度：** 高

典型日志包括：

- 完整 `AUTHOR-TOKEN`。
- 保存/读取 Token时打印原值。
- 完整用户 JSON。
- 微信 access token、refresh token、openid、unionid。
- 一键登录 token、实名 meta/token。
- 通用网络请求的完整响应体。
- 消息内容和媒体上传结果。
- 阿里 APM `openDebug(true)`。

Android 普通第三方 App通常不能直接读取其他应用日志，但以下场景仍有风险：

- 已授权 ADB、Root、取证环境。
- OEM日志服务、系统级诊断工具。
- 崩溃/APM采集、客服日志上传。
- 同 UID代码执行或设备被攻陷。

### 影响

会话、OAuth凭据、手机号、位置、余额、用户画像和聊天内容可能进入超出预期的数据系统，造成账号接管和个人信息合规事件。

### 修复

- Release 构建编译期裁剪 debug/verbose 日志。
- 统一日志拦截器，禁止记录 Authorization、Cookie、Token、身份证、手机号、位置、聊天正文和响应 body。
- 生产关闭第三方 SDK debug和 BODY日志。
- 审计 APM/崩溃平台已有数据并轮换可能泄露的凭据。

---

## APK-14：会话和完整用户对象明文落盘

**影响版本：** v148、v154；v148 更严重
**等级：** 中
**置信度：** 高

`SharedHelper` 使用普通 SharedPreferences保存：

- `AUTHORIZATION` / `AUTHOR-TOKEN`
- 融云 Token
- UniqueToken、多账号 UID
- 完整 `UserInfoList`

用户对象字段包含或可能包含：

- phone、password、openid
- 余额、收入等资产字段
- 经纬度、相册、关系资料
- 房间和房间密码类字段

应用还尝试把全局 `CommonUtil` 通过 Java 序列化写到硬编码 `files/userinfo` 路径。该路径包名与真实包名不一致，运行时很可能失败，不能把它作为已确认的可利用反序列化入口；但这种持久化设计本身应移除。

v148 `allowBackup=true` 且没有备份排除规则，增加旧 Android、OEM云备份和设备迁移中的导出风险；v154 已改为 `false`。

### 修复

- 仅保存业务必需的最小会话状态。
- 使用 Android Keystore保护的 AEAD 加密存储。
- Token短期、可撤销、设备绑定；不保存服务端不应返回的 password 字段。
- 删除整个全局对象的 Java 序列化。
- v148 增加备份排除并推动强制升级。

---

## APK-15：大量导出组件可被外部触达，存在崩溃和权限滥用入口

**影响版本：** v148、v154
**等级：** 中；部分入口与 APK-02 组合后更高
**置信度：** 导出状态高，具体业务副作用按项区分

两版均有 26 个导出 Activity，且 26/26 均无组件权限。以下为已经确认代码后果或高优先级入口。

### 15.1 确定性崩溃/DoS

| 组件 | 版本 | 证据 | 可能后果 |
|---|---|---|---|
| `xin.banghua.beiyuan0.chat.MessageReceivedService` | 两版 | exported、无权限；`onBind()` 直接抛 `UnsupportedOperationException` | 外部反复 bind 可使主进程崩溃 |
| `xin.banghua.beiyuan0.ConversationActivity` | 两版 | exported；`targetId` 来自 extra，缺失时直接调用 `equals/startsWith` | 无参数启动可触发 NPE；显式参数可强制导航到指定会话 UI |
| `xin.banghua.beiyuan0.ConversationListActivity` | v154 | `address_book` 未初始化就 `setOnClickListener()`；赋值错误地放在点击回调末尾 | 任意启动即 NPE，外部 App可稳定触发进程崩溃 |

v154 `ConversationListActivity.java:14-29` 明确显示初始化顺序错误；v148 使用 ButterKnife 先绑定控件，不存在该退化。

### 15.2 悬浮窗 Service

`cn.leyuan.base_library.custom_ui.FloatingButtonService`：

- exported、无权限。
- `onCreate()` 直接调用 `WindowManager.addView(TYPE_APPLICATION_OVERLAY)`。
- 已有悬浮窗权限时，外部 App可强制显示覆盖层；没有权限时可能触发异常崩溃。
- 点击覆盖层会打开业务 WebView。

### 15.3 v148 BootReceiver

`xin.banghua.beiyuan0.BootReceiver`：

- 因 `BOOT_COMPLETED` filter 隐式导出。
- `onReceive()` 不校验 action。
- 任意显式广播都可进入消息前台/后台 Service启动逻辑，造成后台初始化、耗电或资源消耗。

v154 已移除。

### 15.4 通话、房间、会话和推送入口

无权限导出的入口还包括：

- `SingleCallActivity`
- `MultiVideoCallActivity`
- `MultiAudioCallActivity`
- `DialActivity`
- `GameRoomActivity`
- `VoiceRoomActivity`
- `RadioRoomActivity`
- `LiveRoomActivity`
- `RongBridgeActivity`
- 多个 Xiaomi/Meizu/Vivo/Rong/TIM 推送 Receiver

外部 App可启动或投递参数，可能强制打开通话/房间/会话界面、发起网络初始化、伪造推送路由、骚扰用户或触发畸形参数崩溃。但当前静态证据不足以直接宣称“自动发消息”“免费通话”或“任意入房成功”，最终权限仍应由服务端和 SDK状态机验证。

Manifest 虽定义了融云 signature 权限：

- `...permission.RONG_ACCESS_RECEIVER`
- `...permission.RONG_BRIDGE_ACTIVITY`

但没有实际挂到对应 `PushReceiver`/`RongBridgeActivity`，定义权限本身不会自动保护组件。

### 修复

- 默认所有内部组件显式 `android:exported="false"`。
- 必须跨应用的组件挂自有 signature 权限，并校验调用 UID/签名。
- Receiver要求发送方权限；不能只比较 action。
- 所有 extras/URI做空值、类型、长度、枚举和业务状态校验。
- 敏感操作必须由服务端重新授权，Activity 是否可启动不能决定权限。

---

## APK-16：全局允许明文流量与 WebView 混合内容

**影响版本：** v148、v154
**等级：** 中高
**置信度：** 配置高，APK-01 已证明实际可进入 HTTP

Manifest：

```xml
android:usesCleartextTraffic="true"
android:networkSecurityConfig="@xml/network_security_config"
```

网络安全配置的 `base-config` 又设置：

```xml
cleartextTrafficPermitted="true"
```

多个 WebView使用 `MIXED_CONTENT_ALWAYS_ALLOW`。

虽然首方硬编码接口多数为 HTTPS，但二维码、分享链接、服务端字段和 SDK元数据可以引入 HTTP。APK-01 中的恶意二维码已经是一条明确链：`http://攻击者站点 → WebView → AUTHOR-TOKEN 明文发送`。

### 修复

- 全局设 `usesCleartextTraffic=false`、`cleartextTrafficPermitted=false`。
- 生产环境删除所有明文例外；确有遗留域时只做最小 domain-config并设迁移期限。
- WebView设 `MIXED_CONTENT_NEVER_ALLOW`。
- 更新、资源和动态代码即使使用 HTTPS，也仍需独立签名，不能只依赖 TLS。

---

## APK-17：多个 FileProvider 暴露范围过宽

**影响版本：** v148、v154
**等级：** 中，条件性
**置信度：** 危险配置确认；尚无完整“外部可控路径 → grant URI”利用链

多个 provider 的 paths XML包含：

- `root-path path=""` 或 `path="."`
- `files-path path="."`
- `cache-path path="."`
- 整个 external 路径

涉及 `rc_file_path.xml`、`file_paths_public.xml`、`kit_file_paths_public.xml`、`ps_file_paths.xml` 等。

Provider本身大多 `exported=false`，这是重要缓解；但都允许临时 URI grant。若某个导出组件、分享流程、图片/更新接口允许外部影响要包装的绝对路径，应用就可能把私有 SharedPreferences、数据库、聊天文件或缓存的 URI临时授权给不可信接收者。

当前未证明这条完整路径，因此不认定为已确认任意私有文件读取。

### 修复

删除所有 `root-path` 和全根 `path="."`；按用途拆分 provider，只暴露固定子目录，默认只读、显式接收包、短时 grant，并及时 `revokeUriPermission()`。

---

## APK-18：RoomKit `BusinessToken` 固定写入客户端请求

**影响版本：** v148、v154
**等级：** 中，服务端若单独信任则可升高
**置信度：** 硬编码与请求使用确认

证据链：

- `BuildConfig.BUSINESS_TOKEN` 包含固定值，本报告已脱敏。
- `App.initConfig()` 把值传入 `AppConfig`。
- `cn.rongcloud.config.init.OKModule` 对请求添加 `BusinessToken`。
- 登录后还可能附加用户 `Authorization`。

任何 APK内置固定值都应视为公开标识，而不是秘密。第三方可提取后模拟官方 RoomKit客户端。

### 可能影响

- 调用未登录公共接口、枚举房间、消耗 RTC/API配额。
- 污染客户端来源和统计。
- 如果部分接口仅凭 BusinessToken授权，则可能形成直接越权。

### 修复

服务端不得把该值作为身份或授权凭据。应用级标识只能用于路由/统计；真正权限必须依赖用户会话、对象授权、限流和风控。

---

## APK-19：隐私同意前可能发生页面分析上报

**影响版本：** v148、v154
**等级：** 中，合规风险
**置信度：** 缺少统一同意门禁已确认；首次启动时实际字段值需动态抓包

`App.onCreate()` 无条件注册 `frontOrBack()` 的 Activity 生命周期回调。`onActivityResumed()` 在有前一页面时构造并调用 `addPageAnalysis`：

- `uid`
- `page`
- `device_id`
- `os`
- `duration`

该上报路径未检查 `readPrivateAgreement()` 或其他 consent 状态。阿里 APM还配置了 `openDebug(true)`。

首次未登录时 UID可能为空，且第三方 SDK是否立刻联网需动态确认；但架构上没有“同意前禁止所有非必要采集”的统一门禁。

### 修复

建立单一隐私状态机：同意前只初始化运行必需组件，禁止分析、广告、设备标识和非必要第三方 SDK；同意后再统一初始化。撤回同意后停止采集并清理标识。

---

# 6. 其他加固与待核查项

| 项目 | 证据/边界 | 建议 |
|---|---|---|
| 自定义 scheme 可被抢占 | `pushscheme://com.tencent.qcloud.uniapp/detail` 无法验证应用归属 | 改为 HTTPS App Links + `autoVerify`，参数加时效/nonce并由服务端复验 |
| 更新 APK缺少客户端 hash/发布者预检 | 下载后直接调系统安装器；Android仍会用包签名阻止不同签名覆盖 | 使用签名清单和 SHA-256预检；仅允许固定 HTTPS域；失败时清理下载文件 |
| 权限声明过宽 | `QUERY_ALL_PACKAGES`、位置、电话、悬浮窗、安装 APK、相机、麦克风、存储等；部分 signature权限声明普通应用实际得不到 | 做最小权限审计，按功能按需申请；删除无效、拼写错误和历史权限 |
| targetSdk=30 | 两版均未跟进较新的组件、存储、后台和权限安全模型 | 分阶段升级 targetSdk，先修复 exported、前台服务、存储和通知兼容 |
| Native 媒体栈庞大 | 62/63 个 arm64 SO，包含多套 FFmpeg/播放器/美颜/RTC组件 | 精确识别版本并做 SCA、恶意媒体 fuzz、ASAN/HWASan测试；没有版本证据前不绑定具体 CVE |
| ZIP bomb/资源耗尽 | 多个通用解压工具缺少总量、条目数和压缩比限制 | 统一安全解压库并设置硬上限 |
| v154 实名流程退化需复核 | 从活体链改为姓名+身份证二要素；成功后客户端修改 `rp_verify_time` | 服务端必须原子持久化实名状态，所有敏感功能服务端复验；当前不能直接认定实名绕过 |

---

# 7. v148 → v154 差异

## 7.1 v154 已改善

- 移除 Sophix Application、Service、metadata、Java库和 `libsophix.so`。
- `allowBackup=true` 改为 `false`。
- 移除 `BootReceiver`。
- 移除 EPUB小说模块及其用户导入 Zip Slip。
- uses-permission 从 72 条/71 唯一值降到 63 条。
- 移除后台定位、开机启动等部分权限。
- 组件数 `503 → 492`。
- 导出组件 `52 → 51`。
- 无权限保护的导出组件 `42 → 40`。

## 7.2 v154 仍未修复

- 任意 URL泄露 `AUTHOR-TOKEN`。
- 导出 WebView自动批准摄像头/麦克风。
- 腾讯 IM UserSig密钥。
- 主网络 HostnameVerifier错误。
- HFOpen 完整 Trust-All。
- Meihu 贴纸 Zip Slip、Egret动态代码下载与 Zip Slip。
- 微信 OAuth state问题。
- Token URL、敏感日志、明文存储。
- 客户端可预测签名。
- 全局明文 HTTP、混合内容。
- 大量无权限导出组件和过宽 FileProvider。
- targetSdk仍为 30。

## 7.3 v154 新增退化或关注

- `ConversationListActivity` 控件初始化顺序错误，任意启动即 NPE。
- 实名流程从活体认证改为姓名+身份证二要素，服务端最终状态和敏感功能复验必须重点确认。

---

# 8. 可组合攻击链

## 8.1 二维码直接窃取账号会话

```text
恶意二维码/分享链接
→ /base/web_view
→ SliderWebViewActivity 对攻击者 URL附加 AUTHOR-TOKEN
→ 攻击者服务器取得会话
→ 模拟官方客户端调用业务接口
```

## 8.2 恶意本地 App 借宿主权限采集音视频

```text
恶意 App
→ 启动导出的 ActCommentWeb
→ key_basis 指向攻击者网页
→ RCWebView 自动 grant 摄像头/麦克风
→ 网页采集并上传音视频
```

前提是小贝乐园已有对应 Android运行时权限。

## 8.3 贴纸内容源被控后覆盖应用私有文件

```text
SDK 已初始化并启用贴纸下载
→ 控制贴纸后台/资源响应/CDN
→ 客户端接受缺少独立发布签名的恶意 ZIP
→ FileUtil Zip Slip
→ 覆盖 SharedPreferences/数据库/缓存/动态资源
→ 会话破坏、DoS；命中可执行加载目标时可能升级代码执行
```

## 8.4 内容源被控后通过 Egret 执行代码

```text
控制 Egret 元数据/CDN/归档
→ 下载无独立发布签名的 ZIP
→ Zip Slip/替换 egret-dex.jar、libegret.so
→ DexClassLoader/Native loader
→ 应用 UID 代码执行
```

## 8.5 直接冒充 TIM 用户

```text
从 APK提取 UserSig HMAC密钥
→ 为任意 identifier 生成 7 天 UserSig
→ 登录腾讯 IM
→ 冒充、骚扰、群组或消息能力滥用
```

## 8.6 v148 恶意 EPUB 文件覆盖私有目录

```text
诱导用户导入恶意 EPUB
→ EpubUtils 无路径边界校验
→ ../ 越界写入应用私有目录
→ 配置/数据库破坏或条件性动态代码覆盖
```

---

# 9. APK 端修复优先级

## P0：立即处理

1. 停止向任意 WebView/第三方 URL发送 `AUTHOR-TOKEN`。
2. 关闭或保护 `ActCommentWeb`，删除自动 `PermissionRequest.grant()`。
3. 吊销并轮换腾讯 IM UserSig密钥。
4. 吊销 v148 Sophix全部旧密钥，强制淘汰 v148。
5. 删除主网络错误 HostnameVerifier，以及 HFOpen 的 Trust-All实现。
6. 修复 Meihu、Egret和所有解压路径的 Zip Slip；增加容量限制。
7. 停止网络动态加载 DEX/JAR/SO；暂时不能停止时加入离线发布签名、防回滚和原子安装。
8. 将 HTTP和 WebView混合内容改为默认拒绝。

## P1：近期版本完成

1. 修复微信 OAuth高熵一次性 state和回调事务绑定。
2. 禁止 Token出现在 URL/Intent URI。
3. 删除生产敏感日志，关闭 APM和网络库 debug日志。
4. 最小化并加密本地会话数据。
5. 默认关闭所有非必要 exported组件；必要入口加 signature权限和参数校验。
6. 修复 v154 `ConversationListActivity`、`MessageReceivedService`、`ConversationActivity`崩溃入口。
7. 按 origin拆分网络客户端，第三方请求绝不附加首方认证头。

## P2：持续加固

1. 收紧 FileProvider目录和 URI grant生命周期。
2. 清理过宽/无效权限，升级 targetSdk。
3. 建立隐私同意前统一 SDK初始化门禁。
4. 对 Native媒体、美颜、RTC和播放器做版本 SCA与 fuzz。
5. 更新包、媒体和资源统一采用签名清单、哈希和防回滚。

---

# 10. 后续 Web/API 审计清单

以下是 APK暴露出的服务端验证重点，不代表本轮已确认服务端漏洞：

1. `AUTHOR-TOKEN` 的作用域、TTL、设备绑定、撤销和并发会话策略。
2. 服务端是否把 `SIGN-TOKEN`、`EXPIRE-TOKEN`、`AUTHOR-SIGNATURE` 或 `uniquelogintoken` 当真实鉴权。
3. `BusinessToken` 单独可访问哪些 RoomKit接口。
4. 用户、房间、群组、会话、礼物、订单等对象是否做服务端对象级授权。
5. 微信登录/绑定是否验证 state、当前会话、一次性事务和 code重放。
6. 腾讯 IM泄露密钥是否仍有效，identifier能访问哪些消息/群权限。
7. 通话、入房、建房、礼物和资产操作是否全部在服务端重新授权和计算。
8. 实名状态是否由服务端原子持久化，客户端 `rp_verify_time` 是否只用于展示。
9. 支付是否只依赖服务端向支付平台验签、查单、金额核对和幂等入账。
10. Sophix、Egret、贴纸、更新 APK和资源 CDN是否有独立发布签名、防回滚和异常审计。
11. 短信验证码、密码重置、CAPTCHA是否绑定手机号、会话、用途和一次性 ticket。

---

# 11. 明确排除或暂不下结论的项目

为避免把攻击面误写成已利用漏洞，本轮明确保留以下边界：

- APK v2-only签名与 minSdk 24兼容，不是 APK可篡改漏洞。
- 支付回调代码主要重新查询服务端状态，未发现客户端回调直接入账，不能认定免费充值。
- FileProvider虽然路径过宽，但未证明完整外部路径控制和 URI grant链，不能认定任意私有文件读取。
- Sophix导出 AIDL Service在 release模式有本地补丁限制，不能认定已确认本地 RCE。
- 主业务 OkHttp仍使用系统 CA链校验，不能称为“接受任意自签名证书”；业务可达的完整 Trust-All目前只在 HFOpen链确认。
- 包中 OkGo默认 Trust-All实现确实存在，但未找到 Meihu贴纸实际下载调用它；实际下载使用带 `server.cer` 和主机名校验的 Kalle，不能把 OkGo死代码误报为活跃下载链。
- 更新 APK缺少客户端 hash不等于可用不同签名 APK静默覆盖；Android安装器仍会校验包签名。
- `RPVerifyActivity` 导出不等于实名绕过。
- 硬编码错误包名路径的 `ObjectInputStream` 不能作为直接可利用反序列化结论。
- Native库未完成精确版本/SCA前，不把某个公开 CVE直接绑定到样本。
- 导出通话、房间、会话组件不等于已经证明可自动发消息、免费通话或绕过服务端入房权限。

---

## 12. 本轮验证状态

- 已完成：APK哈希、签名、Manifest、权限、组件导出、Java/Smali关键链、资源路径、DEX/SO基线和两个版本差异静态分析。
- 未执行：安装运行、ADB动态触发、WebView音视频 PoC、网络抓包、TLS代理、API请求、第三方平台密钥有效性验证、服务端授权测试。
- 未修改：两个 APK及现有业务代码。
