#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "请使用 sudo 执行 docker/prepare-host.sh。" >&2
    exit 1
fi

DATA_ROOT="${BBW_DATA_ROOT:-/var/lib/bbw}"

# Caddy 官方镜像未承诺提供固定的非 root 默认用户。Compose 使用
# 1000:1000 并监听高端口，因此证书和自动配置目录必须预先授权。
install -d -o root -g root -m 0750 "$DATA_ROOT"
install -d -o root -g root -m 0750 "$DATA_ROOT/caddy"
install -d -o 1000 -g 1000 -m 0700 "$DATA_ROOT/caddy/data"
install -d -o 1000 -g 1000 -m 0700 "$DATA_ROOT/caddy/config"

echo "已准备 Caddy 持久目录：$DATA_ROOT/caddy"
