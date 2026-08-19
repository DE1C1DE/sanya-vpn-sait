#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY=""
REF="main"
INSTALL_ARGS=()

usage() {
    cat <<'EOF'
Установка «Три буквы от Сани» напрямую из GitHub

Пример:
  sudo bash install-from-github.sh \
    --repo OWNER/REPOSITORY \
    --domain vpn.example.com \
    --admin-password 'сложный-пароль'

Параметры --domain, --admin-password, --public-origin и --force-db
передаются во внутренний install.sh.
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
ARCHIVE_URL="https://github.com/${REPOSITORY}/archive/refs/heads/${REF}.tar.gz"
WORK_DIR="$(mktemp -d -t tri-bukvy-github-XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "Скачивание ${REPOSITORY}, ref=${REF}"
curl --fail --location --silent --show-error "${ARCHIVE_URL}" -o "${WORK_DIR}/project.tar.gz"
tar -xzf "${WORK_DIR}/project.tar.gz" -C "${WORK_DIR}"
PROJECT_DIR="$(find "${WORK_DIR}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
if [[ ! -f "${PROJECT_DIR}/install.sh" ]]; then
    echo "В репозитории не найден install.sh." >&2
    exit 1
fi
chmod +x "${PROJECT_DIR}/install.sh"
exec bash "${PROJECT_DIR}/install.sh" "${INSTALL_ARGS[@]}"
