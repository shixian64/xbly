# 13 · APK 更新对比：beibeiwu(148) vs xbly(154)

**日期：** 2026-07-15  
**旧包：** `beibeiwu.apk`  
**新包：** `xbly.apk`  
**协议核跟版：** `sign.VERSION_CODE=154`，见 T13

---

## 1. 结论

| 问题 | 答案 |
|---|---|
| 是否换协议？ | **否**。主域名、签名盐、Header、包名、支付/IM 密钥串仍在 |
| 协议核是否要大改？ | **否** |
| 是否要小更新？ | **是**：版本号 154、catalog、新 action `Id2MetaVerifyRequest` |

---

## 2. 包体

| 项 | 148 | 154 |
|---|---|---|
| 文件 | beibeiwu.apk | xbly.apk |
| 约大小 | 253MB | 292MB |
| version 标记 | About_app&version=**148** | About_app&version=**154** |
| 包名 | xin.banghua.beiyuan0 | 同左 |

---

## 3. 鉴权与第三方（未变）

仍存在：`SIGN/EXPIRE/AUTHOR-*`、`socialchat`/`xiaobei` 盐、`SigninOneKeyLogin1`、`InitFaceVerify0`、TIM SECRETKEY、`m7ua80gbmo0km`、微信 APP_ID、`tximsign.php`、`userregister.php`、`buyCoin*`、`getMatchNum`/`buyCard` 等。

`otherinterface/*` 路径集合 diff 为空。

---

## 4. Action 差集

### 新增

- `Id2MetaVerifyRequest`  
  - URL：`.../do=Id2MetaVerifyRequest&m=socialchat`  
  - 封装：`app.misc.id2_meta_verify(**params)`  
  - 无参实测：`401 接口调用失败`（需证件/元信息类参数，待抓包/jadx 补全）

### 客户端移除（小说）

- `getCategoryNovels` / `getChapterList` / `getDetailedChapter`
- `getMyChapterList` / `getMyDetailedChapter`
- `getNovelsSource` / `getRankNovels`

已写入 `api_catalog.json` → `deprecated_actions`。服务端可能仍响应，但新包不再调用。

### Catalog 扫描补录

`signin0` 与 `SigninOneKeyLogin1` 在 v154 登录流程中仍被调用，但 URL 的 `do=` 值由运行时
拼接，旧的“明文 URL / `startHttp(..., "action")`”正则扫描没有收录。现已作为
`dynamic_do_actions` 补录。修订后的口径为：

- v154 当前活跃 action：**398**；
- v148 下线但为溯源保留：**7**；
- catalog 历史并集：**405**；
- v154 完整/重建 URL：**328**。

---

## 5. 协议核变更清单

| 文件 | 变更 |
|---|---|
| `bbw_protocol/sign.py` | `VERSION_CODE="154"` |
| `bbw_protocol/modules/content.py` | `about()` 用 session/VERSION |
| `bbw_protocol/modules/misc.py` | `id2_meta_verify` |
| `api_catalog.json` | 新/弃用 action、About URL、counts |
| 探针脚本 UA/version | 154 |
| 文档 00/01/02/05/11/README | 版本与 T13 |

---

## 6. 后续可选

1. jadx `xbly.apk` 还原 `Id2MetaVerifyRequest` 完整表单字段  
2. 全量重扫 startHttp 是否有非 `do=` 明文新接口  
3. ✅ 已确认 v154 移除 Sophix 壳、service、`libsophix.so` 与相关 metadata；后续仅需评估历史版本影响
