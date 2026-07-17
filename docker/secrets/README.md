# Docker secrets

该目录只允许提交本说明和 `.gitignore`。生产 secret 由服务器执行：

```bash
./docker/init-secrets.sh
```

生成或写入的文件包括：

- `postgres_password`
- `app_master_key`
- `credential_keyring`
- `phone_hmac_key`
- `session_hmac_key`
- `admin_initial_password`
- `txim_secret_key`
- `roomkit_business_token`
- `r2_access_key_id`
- `r2_secret_access_key`
- `turnstile_secret_key`

两项协议凭据必须从当前有效的上游配置安全迁移到 Secret 文件。源码中曾出现过的旧值应视为已泄露；具备相应控制台权限后必须轮换。不要把这些值写入 `.env`、部署文档或 Git。

目录必须保持 `0700`，实际 Secret 文件保持只读 `0444`。这是普通 Docker Compose file-backed secret 的兼容要求：不同非 root 容器 UID 只会看到显式挂载给自己的文件，而宿主机其他用户仍因目录不可遍历而无法读取。不能提交到 Git、复制进镜像或写入日志。
