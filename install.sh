#!/usr/bin/env bash
# CRYMODER — быстрая установка.
#
#   curl -fsSL https://raw.githubusercontent.com/Yaroslavtatar/CRYMODER/main/install.sh | bash
#
# Переменные окружения (для неинтерактивной установки):
#   BOT_TOKEN=...      токен бота
#   INSTALL_DIR=...    куда клонировать (по умолчанию ./CRYMODER)
#   MODE=docker|venv   способ запуска (по умолчанию docker, если он есть)
set -euo pipefail

REPO_URL="https://github.com/Yaroslavtatar/CRYMODER.git"
INSTALL_DIR="${INSTALL_DIR:-CRYMODER}"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_off=$'\033[0m'
info() { echo "${c_grn}==>${c_off} $*"; }
warn() { echo "${c_ylw}!!${c_off} $*"; }
die()  { echo "${c_red}ОШИБКА:${c_off} $*" >&2; exit 1; }

# Ввод читаем с терминала, чтобы работало и при `curl | bash`
ask() {
  local prompt="$1" var
  if [ -r /dev/tty ]; then read -r -p "$prompt" var </dev/tty || true; fi
  echo "${var:-}"
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---------- 1. Исходники ----------
if [ ! -f "crymoder/__main__.py" ]; then
  have git || die "Нужен git (apt install git)."
  if [ -d "$INSTALL_DIR/.git" ]; then
    info "Обновляю $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
  else
    info "Клонирую репозиторий в $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  cd "$INSTALL_DIR"
fi

# ---------- 2. Токен ----------
if [ ! -f .env ]; then
  cp .env.example .env
fi

current_token="$(grep -E '^BOT_TOKEN=' .env | head -n1 | cut -d= -f2- || true)"
if [ -z "${BOT_TOKEN:-}" ] && { [ -z "$current_token" ] || [ "$current_token" = "123456:replace_me" ]; }; then
  BOT_TOKEN="$(ask 'Токен бота от @BotFather: ')"
fi
if [ -n "${BOT_TOKEN:-}" ]; then
  [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "Токен выглядит неверно. Формат: 123456789:AA..."
  # Экранируем для sed и записываем в .env
  esc=$(printf '%s' "$BOT_TOKEN" | sed -e 's/[\/&|]/\\&/g')
  sed -i.bak "s|^BOT_TOKEN=.*|BOT_TOKEN=${esc}|" .env && rm -f .env.bak
  chmod 600 .env
elif [ "$current_token" = "123456:replace_me" ] || [ -z "$current_token" ]; then
  die "BOT_TOKEN не задан. Запустите: BOT_TOKEN=... ./install.sh либо впишите его в .env"
fi

# ---------- 3. Запуск ----------
MODE="${MODE:-}"
if [ -z "$MODE" ]; then
  if have docker && docker compose version >/dev/null 2>&1; then
    MODE=docker
  else
    MODE=""
  fi
fi

if [ -z "$MODE" ]; then
  warn "Docker с плагином compose не найден."
  ans="$(ask 'Установить Docker автоматически (get.docker.com)? [y/N]: ')"
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    have curl || die "Нужен curl."
    info "Устанавливаю Docker"
    curl -fsSL https://get.docker.com | sh
    MODE=docker
  else
    MODE=venv
  fi
fi

SUDO=""
if [ "$MODE" = "docker" ] && ! docker info >/dev/null 2>&1; then
  have sudo && SUDO="sudo" || die "Нет доступа к Docker. Запустите от root или добавьте себя в группу docker."
fi

if [ "$MODE" = "docker" ]; then
  info "Собираю и запускаю контейнер"
  $SUDO docker compose up -d --build
  echo
  info "Готово! CRYMODER запущен."
  echo "   Логи:        $SUDO docker compose logs -f"
  echo "   Остановить:  $SUDO docker compose down"
  echo "   Обновить:    git pull && $SUDO docker compose up -d --build"
else
  have python3 || die "Нужен python3 (3.10+)."
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Нужен Python 3.10 или новее."
  info "Создаю виртуальное окружение и ставлю зависимости"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  echo
  info "Готово! Запуск без Docker:"
  echo "   .venv/bin/python -m crymoder"
  echo "   (для автозапуска используйте systemd или tmux/screen)"
fi

echo
echo "Дальше: добавьте бота в группу и сделайте админом (удаление сообщений + блокировка участников)."
