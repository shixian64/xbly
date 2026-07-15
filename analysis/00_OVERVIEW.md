# 00 · 项目总览

**最后更新：** 2026-07-15

## 1. 目标

对 `beibeiwu.apk`（小贝乐园 / 贝贝屋）做 CTF 向静态逆向 + 协议分析与授权测试，输出可复现结论与工具。

## 2. 应用指纹

| 项 | 值 |
|---|---|
| APK | `xbly.apk`（当前）/ `beibeiwu.apk`（v148 基线） |
| 包名 | `xin.banghua.beiyuan0` |
| versionCode / Name | **`154`**（旧包 148） |
| Application | `xin.banghua.beiyuan0.SophixStubApplication`（Sophix 热修复壳） |
| 真实 Application | `xin.banghua.beiyuan0.App` |
| 业务主包 | `xin.banghua.beiyuan0` + `cn.leyuan.base_library` |
| 后端形态 | 微擎 WeEngine 风格 `app/index.php?i=...&do=...&m=socialchat` |
| 主域名 | `applet.banghua.xin` / `redis.banghua.xin` / `oss.banghua.xin` |

## 3. 工作区结构（精简）

```
D:\project\AI\bbw\
├── xbly.apk / beibeiwu.apk   # 本地 APK（gitignore）
├── .gitignore
└── analysis\                 # 文档 + 协议核 + Web（入库主体）
    ├── README.md             # 索引
    ├── 00_…13_*.md           # 分析文档
    ├── api_catalog.json
    ├── bbw_protocol/         # 协议核
    ├── bbw_web/              # 多用户 Web
    ├── bbw_client.py …       # 可选早期探测脚本
    └── （本地）session.json / sessions/  # gitignore
```

历史反编译树 `jadx_out/`、提取物 `assets/` 等**已清理**，需要时从 APK 再生。  
文档列表见 [README.md](./README.md)。

## 4. 进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| APK 结构摸底 | ✅ | 结论已写入 01 |
| jadx 反编译 | ✅ | 本地树可删可再生 |
| 鉴权算法还原 | ✅ | SIGN/EXPIRE/UNIQUE/AUTHOR-SIG |
| 硬编码密钥梳理 | ✅ | 融云/腾讯 IM/微信/推送等 |
| 登录协议测试 | ✅ | 短信 + 一键登录洞 + 改密 + 密码登录 |
| 资料修改测试 | ✅ | 昵称/角色/VIP 探测 |
| 用户实名 | ⏳ | **用户自行在 App 操作** |
| 实名后改昵称 | ⏳ | 待实名完成再测 `resetNew` |
| 游客能力矩阵 | ✅ | 见 09 |
| 协议客户端落地 | ✅ | `bbw_protocol` + CLI + catalog |
| 原生/多用户 Web | ✅ | adapters + `bbw_web` |
| 协议跟版 v154 | ✅ | 见 13 |
| 越权/支付深测 | ⏳ | 见 07_NEXT |

## 5. 测试账号（摘要）

详见 [05_ACCOUNT.md](./05_ACCOUNT.md)。

- 手机号：`19122614669`
- uid：`726285`
- 当前昵称：`游客`（目标改 `Vom`，未实名拦截）
- 角色：`普通用户`；vip/svip：`0/0`

## 6. 工具链

| 工具 | 路径/说明 |
|---|---|
| jadx | `D:\tools\jadx\jadx-1.5.3\bin\jadx.bat`（JRE 来自 jadx-gui） |
| Python | 3.14，协议脚本 |
| adb | 已安装（动态未深入） |

## 7. 重要约定

1. 本项目文档为 CTF/安全研究记录。  
2. 用户声明测试手机号与密码由本人提供，用于协议验证。  
3. **实名认证由用户本人在客户端完成**，自动化脚本不伪造身份信息。  
4. 后续所有新测试必须写入 `03_TEST_LOG.md`。
