#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="tri-bukvy"
APP_DIR="/opt/${APP_NAME}"
ENV_FILE="/etc/${APP_NAME}.env"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
NGINX_FILE="/etc/nginx/sites-available/${APP_NAME}"
DOMAIN=""
ADMIN_PASSWORD="${VPN_PORTAL_ADMIN_PASSWORD:-}"
PUBLIC_ORIGIN=""
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE_DB="0"

usage() {
    cat <<'EOF'
Установка сервиса «Три буквы от Сани»

Использование:
  sudo bash install.sh --domain vpn.example.com --admin-password 'сложный-пароль'

Параметры:
  --domain NAME             Домен с A-записью на этот сервер, обязателен
  --admin-password VALUE    Пароль администратора; если не задан, будет сгенерирован
  --public-origin URL       Публичный URL, по умолчанию https://DOMAIN
  --force-db                Перезаписать /opt/tri-bukvy/data.sqlite3 локальной базой
  --help                    Показать эту справку
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="${2:?Не указан домен после --domain}"; shift 2 ;;
        --admin-password) ADMIN_PASSWORD="${2:?Не указан пароль после --admin-password}"; shift 2 ;;
        --public-origin) PUBLIC_ORIGIN="${2:?Не указан URL после --public-origin}"; shift 2 ;;
        --force-db) FORCE_DB="1"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Неизвестный параметр: $1" >&2; usage; exit 2 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запустите установщик через sudo или от root." >&2
    exit 1
fi
if [[ -z "${DOMAIN}" ]]; then
    echo "Нужно указать --domain. DNS A-запись домена должна уже указывать на этот сервер." >&2
    exit 1
fi
if [[ -z "${PUBLIC_ORIGIN}" ]]; then
    PUBLIC_ORIGIN="https://${DOMAIN}"
fi
if [[ -z "${ADMIN_PASSWORD}" ]]; then
    ADMIN_PASSWORD="$(openssl rand -hex 16)"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 sqlite3 nginx openssl certbot curl ca-certificates

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp
    ufw allow 443/tcp
fi

install -d -m 0750 "${APP_DIR}"
install -m 0644 "${SOURCE_DIR}/app.py" "${APP_DIR}/app.py"
if [[ -f "${SOURCE_DIR}/data.sqlite3" && ! -f "${APP_DIR}/data.sqlite3" ]]; then
    install -m 0600 "${SOURCE_DIR}/data.sqlite3" "${APP_DIR}/data.sqlite3"
elif [[ "${FORCE_DB}" == "1" && -f "${SOURCE_DIR}/data.sqlite3" ]]; then
    install -m 0600 "${SOURCE_DIR}/data.sqlite3" "${APP_DIR}/data.sqlite3"
fi

umask 077
printf 'VPN_PORTAL_HOST=127.0.0.1\nVPN_PORTAL_PORT=8080\nVPN_PORTAL_ADMIN_PASSWORD=%s\nVPN_PORTAL_PUBLIC_ORIGIN=%s\nVPN_PORTAL_XRAY_BINARY=/usr/local/x-ui/bin/xray-linux-amd64\n' "${ADMIN_PASSWORD}" "${PUBLIC_ORIGIN}" > "${ENV_FILE}"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Tri Bukvy ot Sani VPN portal
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${APP_DIR}/app.py
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

install -d -m 0755 /var/www/certbot
cat > "${NGINX_FILE}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
ln -sfn "${NGINX_FILE}" "/etc/nginx/sites-enabled/${APP_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now "${APP_NAME}"
systemctl reload nginx

certbot certonly --webroot -w /var/www/certbot -d "${DOMAIN}" --non-interactive --agree-tos --register-unsafely-without-email

cat > "${NGINX_FILE}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    server_name ${DOMAIN};
    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 4m;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
EOF
nginx -t
systemctl reload nginx
systemctl restart "${APP_NAME}"

echo
echo "Установка завершена."
echo "Сайт: https://${DOMAIN}/"
echo "Админка: https://${DOMAIN}/admin"
echo "Пароль администратора: ${ADMIN_PASSWORD}"
echo "Проверка: https://${DOMAIN}/health"
