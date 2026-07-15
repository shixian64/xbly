# 00 · 项目总览

**最后更新：** 2026-07-15

## 1. 目标

对小贝乐园 / 贝贝屋（`xin.banghua.beiyuan0`）做 CTF 向静态逆向 + 协议分析与授权测试，并提供可复现的 **协议客户端** 与 **多用户 Web 壳**。

## 2. 应用指纹

| 项 | 值 |
|---|---|
| APK | `xbly.apk`（当前 v154）/ `beibeiwu.apk`（v148 基线） |
| 包名 | `xin.banghua.beiyuan0` |
| versionCode | **154**（旧 148） |
| Application | v154 直接 `App`；v148 基线曾使用 Sophix 壳 |
| 后端 | 微擎 `do=` + `m=socialchat` |
| 主域名 | `applet.banghua.xin` / `redis.banghua.xin` / `oss.banghua.xin` |

## 3. 仓库结构

```
xbly/  (repo root)
├── README.md
├── .gitignore
├── bbw_protocol/     # 协议核
├── bbw_web/          # 多用户 Web（与核隔离）
├── docs/             # 本目录文档 + api_catalog.json
├── tools/            # 可选早期脚本
├── session.json      # CLI 会话（本地 gitignore）
└── *.apk             # 本地 gitignore
```

文档列表见 [README.md](./README.md)。

## 4. 进度

| 阶段 | 状态 |
|---|---|
| 静态鉴权 / 密钥梳理 | ✅ |
| 登录 / 改密 / 一键登录洞 | ✅ |
| 实名门禁与绕过面分析 | ✅（伪造不可行） |
| 游客能力矩阵 | ✅ |
| 协议客户端 `bbw_protocol` | ✅ |
| 原生 adapters + 多用户 `bbw_web` | ✅ |
| 跟版 v154 | ✅ |
| 用户自行实名后改昵称 | ⏳ |
| 支付/越权深测 | ⏳ |

## 5. 测试账号（摘要）

详见 [05_ACCOUNT.md](./05_ACCOUNT.md)。手机号与 uid 用于研究记录；**密码不入库**。

## 6. 重要约定

1. 文档为 CTF / 安全研究记录。  
2. 实名由用户本人在官方客户端完成，不伪造刷脸/支付。  
3. 协议核与 Web 分模块；Web 不反向污染核。  
