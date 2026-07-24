#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${SECRETS_DIR:-${ROOT_DIR}/docker/secrets}"

umask 077
install -d -m 0700 "$SECRETS_DIR"

write_if_missing() {
    local name="$1"
    local value="$2"
    local path="${SECRETS_DIR}/${name}"

    if [[ -s "$path" ]]; then
        printf '保留已有 secret: %s\n' "$name"
        return
    fi
    if [[ -e "$path" ]]; then
        chmod 0600 "$path"
    fi
    printf '%s' "$value" >"$path"
    # Compose file-backed secrets are bind-mounted with host ownership.  The
    # secret directory is 0700, while files are read-only so non-root container
    # UIDs can read only the specific secrets mounted into that container.
    chmod 0444 "$path"
    printf '已创建 secret: %s\n' "$name"
}

read_required_secret() {
    local env_name="$1"
    local prompt="$2"
    local value="${!env_name:-}"

    if [[ -z "$value" ]]; then
        if [[ ! -t 0 ]]; then
            printf '缺少 %s；非交互模式请先设置同名环境变量。\n' "$env_name" >&2
            exit 1
        fi
        read -r -s -p "$prompt" value
        printf '\n'
    fi
    if [[ -z "$value" ]]; then
        printf '%s 不能为空。\n' "$env_name" >&2
        exit 1
    fi
    printf '%s' "$value"
}

write_optional_secret() {
    local name="$1"
    local env_name="$2"
    local prompt="$3"
    local path="${SECRETS_DIR}/${name}"
    local value="${!env_name:-}"

    if [[ -s "$path" ]]; then
        printf '保留已有 secret: %s\n' "$name"
        return
    fi
    if [[ -z "$value" && -t 0 ]]; then
        read -r -s -p "$prompt" value
        printf '\n'
    fi
    if [[ -e "$path" ]]; then
        chmod 0600 "$path"
    fi
    printf '%s' "$value" >"$path"
    chmod 0444 "$path"
    if [[ -z "$value" ]]; then
        printf '已创建 secret: %s（空，功能暂不启用）\n' "$name"
    else
        printf '已创建 secret: %s\n' "$name"
    fi
}

value="$(openssl rand -hex 32)"
write_if_missing postgres_password "$value"
value="$(openssl rand -base64 32 | tr -d '\n')"
write_if_missing app_master_key "$value"
write_if_missing credential_keyring '{}'
value="$(openssl rand -base64 32 | tr -d '\n')"
write_if_missing phone_hmac_key "$value"
value="$(openssl rand -base64 32 | tr -d '\n')"
write_if_missing session_hmac_key "$value"
value="$(openssl rand -base64 24 | tr -d '\n' | tr '+/' '-_')"
write_if_missing admin_initial_password "$value"
value="$(openssl rand -hex 32)"
write_if_missing deployment_control_token "$value"

if [[ ! -s "${SECRETS_DIR}/txim_secret_key" ]]; then
    value="$(read_required_secret TXIM_SECRET_KEY '请输入腾讯 IM Secret Key: ')"
    write_if_missing txim_secret_key "$value"
else
    printf '保留已有 secret: %s\n' txim_secret_key
fi

if [[ ! -s "${SECRETS_DIR}/roomkit_business_token" ]]; then
    value="$(read_required_secret ROOMKIT_BUSINESS_TOKEN '请输入 RoomKit Business Token: ')"
    write_if_missing roomkit_business_token "$value"
else
    printf '保留已有 secret: %s\n' roomkit_business_token
fi

if [[ ! -s "${SECRETS_DIR}/r2_access_key_id" ]]; then
    value="$(read_required_secret R2_ACCESS_KEY_ID '请输入具备 Object Read & Write 权限的 Cloudflare R2 Access Key ID: ')"
    write_if_missing r2_access_key_id "$value"
else
    printf '保留已有 secret: %s\n' r2_access_key_id
fi

if [[ ! -s "${SECRETS_DIR}/r2_secret_access_key" ]]; then
    value="$(read_required_secret R2_SECRET_ACCESS_KEY '请输入具备 Object Read & Write 权限的 Cloudflare R2 Secret Access Key: ')"
    write_if_missing r2_secret_access_key "$value"
else
    printf '保留已有 secret: %s\n' r2_secret_access_key
fi

write_optional_secret turnstile_secret_key TURNSTILE_SECRET_KEY \
    '请输入 Cloudflare Turnstile Secret Key（暂不启用可直接回车）: '

for secret_name in \
    postgres_password app_master_key credential_keyring phone_hmac_key session_hmac_key \
    admin_initial_password deployment_control_token txim_secret_key roomkit_business_token \
    r2_access_key_id r2_secret_access_key \
    turnstile_secret_key; do
    if [[ -e "${SECRETS_DIR}/${secret_name}" ]]; then
        chmod 0444 "${SECRETS_DIR}/${secret_name}"
    fi
done

cat <<EOF

Secrets 已保存到：${SECRETS_DIR}
初始管理员密码只保存在 admin_initial_password 文件中，请通过安全渠道留存。
不要把 docker/secrets 目录中的实际 secret 提交到 Git、镜像或备份日志。
EOF
