#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY="DE1C1DE/sanya-vpn-sait"
REF="main"
INSTALL_ARGS=()

usage() {
    cat <<'EOF'
Установка «Три буквы от Сани» одной командой из GitHub

Запуск без параметров:
  curl -fsSL https://raw.githubusercontent.com/DE1C1DE/sanya-vpn-sait/main/install-from-github.sh | sudo bash

Скрипт скачает проект, установит приложение и спросит домен и пароль
администратора. Если оставить поля пустыми, будет настроен HTTP-режим,
а пароль сгенерирован автоматически (будет выведен в конце).

Неинтерактивная установка (CI или без терминала):
  sudo bash install-from-github.sh --domain vpn.example.com --admin-password 'пароль'

Параметры:
  --repo OWNER/REPOSITORY   Репозиторий (по умолчанию DE1C1DE/sanya-vpn-sait)
  --ref NAME                Ветка или тег (по умолчанию main)
  --domain NAME             Домен для HTTPS; без него — HTTP-режим
  --admin-password VALUE    Пароль администратора; без него — генерируется
  --public-origin URL       Публичный URL
  --force-db                Перезаписать базу при наличии локальной
  --help                    Показать эту справку
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPOSITORY="${2:?Не указан OWNER/REPOSITORY после --repo}"; shift 2 ;;
        --ref) REF="${2:?Не указана ветка или тег после --ref}"; shift 2 ;;
        --domain|--admin-password|--public-origin)
            INSTALL_ARGS+=("$1" "${2:?Не указано значение после $1}"); shift 2 ;;
        --force-db) INSTALL_ARGS+=("--force-db"); shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Неизвестный параметр: $1" >&2; usage; exit 2 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запустите через sudo или от root." >&2
    exit 1
fi
if [[ ! "${REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "--repo должен иметь формат OWNER/REPOSITORY." >&2
    exit 1
fi

command -v curl >/dev/null 2>&1 || { apt-get update && apt-get install -y curl ca-certificates; }

has_arg() {
    local key="$1"
    local arg
    for arg in "${INSTALL_ARGS[@]}"; do
        if [[ "${arg}" == "${key}" ]]; then
            return 0
        fi
    done
    return 1
}

ask() {
    local label="$1" var="$2" hidden="$3" answer=""
    if [[ "${hidden}" == "1" ]]; then
        read -r -s -p "${label}" answer < /dev/tty 2>/dev/null || return 0
        printf '\n'
    else
        read -r -p "${label}" answer < /dev/tty 2>/dev/null || return 0
    fi
    if [[ -n "${answer}" ]]; then
        printf -v "${var}" '%s' "${answer}"
    fi
}

echo "Скачивание ${REPOSITORY}, ref=${REF}"
WORK_DIR="$(mktemp -d -t tri-bukvy-github-XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT
curl --fail --location --silent --show-error "https://github.com/${REPOSITORY}/archive/refs/heads/${REF}.tar.gz" -o "${WORK_DIR}/project.tar.gz"
tar -xzf "${WORK_DIR}/project.tar.gz" -C "${WORK_DIR}"
PROJECT_DIR="$(find "${WORK_DIR}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
if [[ ! -f "${PROJECT_DIR}/install.sh" ]]; then
    echo "В репозитории не найден install.sh." >&2
    exit 1
fi

if ! has_arg "--domain" && ! has_arg "--public-origin"; then
    DOMAIN_VALUE=""
    ask "Введите домен для HTTPS (Enter — HTTP-режим без сертификата): " DOMAIN_VALUE 0
    if [[ -n "${DOMAIN_VALUE}" ]]; then
        INSTALL_ARGS+=("--domain" "${DOMAIN_VALUE}")
    fi
fi
if ! has_arg "--admin-password"; then
    PASSWORD_VALUE=""
    ask "Пароль администратора (Enter — сгенерировать автоматически): " PASSWORD_VALUE 1
    if [[ -n "${PASSWORD_VALUE}" ]]; then
        INSTALL_ARGS+=("--admin-password" "${PASSWORD_VALUE}")
    fi
fi

chmod +x "${PROJECT_DIR}/install.sh"
exec bash "${PROJECT_DIR}/install.sh" "${INSTALL_ARGS[@]}"