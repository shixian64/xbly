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

## 3. 工作区结构

```
D:\project\AI\bbw\
├── beibeiwu.apk
├── jadx_out\                 # jadx 反编译
│   ├── sources\
│   └── resources\
└── analysis\                 # 文档 + 脚本 + 提取物（本目录）
    ├── README.md
    ├── 00_OVERVIEW.md
    ├── 01_STATIC_ANALYSIS.md
    ├── 02_PROTOCOL.md
    ├── 03_TEST_LOG.md
    ├── 04_FINDINGS.md
    ├── 05_ACCOUNT.md
    ├── 06_TOOLS.md
    ├── 07_NEXT.md
    ├── DEEP_DIVE.md
    ├── bbw_client.py
    ├── auth_flow.py
    ├── profile_edit.py
    ├── login_session.json
    ├── assets\
    └── asset_408037528\
```

## 4. 进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| APK 结构摸底 | ✅ | 12 dex、63 so、assets 分析 |
| jadx 反编译 | ✅ | 输出 `jadx_out/`，约 4 万文件 |
| 鉴权算法还原 | ✅ | SIGN/EXPIRE/UNIQUE/AUTHOR-SIG |
| 硬编码密钥梳理 | ✅ | 融云/腾讯 IM/微信/推送等 |
| 登录协议测试 | ✅ | 短信 + 一键登录洞 + 改密 + 密码登录 |
| 资料修改测试 | ✅ | 昵称/角色/VIP 探测 |
| 用户实名 | ⏳ | **用户自行在 App 操作** |
| 实名后改昵称 | ⏳ | 待实名完成再测 `resetNew` |
| 游客能力矩阵 | ✅ | 见 09；L0/L1 协议批测完成 |
| 协议客户端落地 | ✅ | `bbw_protocol` + CLI + 402 action 目录 |
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
