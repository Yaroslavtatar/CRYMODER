#!/usr/bin/env bash
# CRYMODER — установщик и менеджер сервера.
#
#   Установка одной командой:
#     curl -fsSL https://raw.githubusercontent.com/Yaroslavtatar/CRYMODER/main/install.sh | bash
#
#   После установки доступна команда `crymoder`:
#     crymoder                 меню управления
#     crymoder update          обновить (с бэкапом БД и откатом при ошибке)
#     crymoder domain          настроить домен + HTTPS (nginx + Let's Encrypt)
#     crymoder status | logs | restart | stop | start | backup
#     crymoder owner           владельцы сервера (OWNER_IDS)
#     crymoder token           сменить токен бота
#     crymoder autoupdate on|off
#     crymoder uninstall
#
# Каждая установка автономна: свой .env, своя база, своя панель, свои владельцы.
#
# Неинтерактивно (переменные окружения):
#   BOT_TOKEN=...  OWNER_IDS=123,456  DOMAIN=bot.example.com  EMAIL=me@example.com
#   MODE=docker|venv  AUTO_UPDATE=1  INSTALL_DIR=/opt/crymoder  BRANCH=main
# Флаги: -y|--yes (не спрашивать)  --auto (для таймера)  --force
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Yaroslavtatar/CRYMODER.git}"
BRANCH="${BRANCH:-main}"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_cyn=$'\033[36m'; c_off=$'\033[0m'
info() { echo "${c_grn}==>${c_off} $*"; }
warn() { echo "${c_ylw}!!${c_off} $*" >&2; }
die()  { echo "${c_red}ОШИБКА:${c_off} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

ASSUME_YES="${ASSUME_YES:-0}"; AUTO=0; FORCE=0
HAVE_TTY=0; (: </dev/tty) 2>/dev/null && HAVE_TTY=1

# ---------------------------------------------------------------- пути
resolve_dir() {
  local self_dir=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -n "${INSTALL_DIR:-}" ]; then echo "$INSTALL_DIR"
  elif [ -n "$self_dir" ] && [ -f "$self_dir/crymoder/__main__.py" ]; then echo "$self_dir"
  elif [ -f "crymoder/__main__.py" ]; then pwd
  elif [ "$(id -u)" -eq 0 ]; then echo "/opt/crymoder"
  else echo "$HOME/crymoder"; fi
}

# ---------------------------------------------------------------- .env / конфиг установки
kv_get() { # файл ключ
  [ -f "$1" ] || return 0
  { grep -E "^$2=" "$1" 2>/dev/null || true; } | head -n1 | cut -d= -f2-
}
kv_set() { # файл ключ значение — заменяет строку или дописывает; права файла сохраняются
  local f="$1" k="$2" v="$3" tmp line found=0
  tmp="$(mktemp)"
  if [ -f "$f" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      if [[ "$line" == "$k="* ]]; then printf '%s=%s\n' "$k" "$v" >>"$tmp"; found=1
      else printf '%s\n' "$line" >>"$tmp"; fi
    done <"$f"
  fi
  [ "$found" = 1 ] || printf '%s=%s\n' "$k" "$v" >>"$tmp"
  cat "$tmp" >"$f"; rm -f "$tmp"
}
env_get()  { kv_get "$INSTALL_DIR/.env" "$1"; }
env_set()  { kv_set "$INSTALL_DIR/.env" "$1" "$2"; }
conf_get() { kv_get "$INSTALL_DIR/.install.conf" "$1"; }
conf_set() { kv_set "$INSTALL_DIR/.install.conf" "$1" "$2"; }

# ---------------------------------------------------------------- ввод
ask() { # подсказка [по умолчанию]
  local prompt="$1" def="${2:-}" ans=""
  if [ "$ASSUME_YES" = 1 ] || [ "$HAVE_TTY" != 1 ]; then echo "$def"; return 0; fi
  read -r -p "$prompt" ans </dev/tty || true
  echo "${ans:-$def}"
}
confirm() { # вопрос [y|n по умолчанию]
  local def="${2:-n}" hint ans
  if [ "$ASSUME_YES" = 1 ] || [ "$HAVE_TTY" != 1 ]; then [ "$def" = y ]; return; fi
  [ "$def" = y ] && hint="Y/n" || hint="y/N"
  ans="$(ask "$1 [$hint]: " "$def")"
  [[ "$ans" =~ ^[YyДд] ]]
}

# ---------------------------------------------------------------- проверки значений
valid_token()  { [[ "$1" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; }
valid_owners() { [[ "$1" =~ ^[0-9]+(,[0-9]+)*$ ]]; }
valid_domain() { [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$ ]]; }
valid_email()  { [[ "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; }
# первый ID из ЛИЧНОГО сообщения боту в ответе getUpdates (группы игнорируем)
parse_sender_id() {
  sed 's/{"update_id"/\n&/g' | grep '"type":"private"' | grep -o '"from":{"id":[0-9]*' | head -n1 | grep -o '[0-9]*$' || true
}

# ---------------------------------------------------------------- права и пакеты
SUDO=""; PM=""; APT_UPDATED=0
setup_privileges() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""
  elif have sudo; then SUDO="sudo"
  else die "Нужны права root: запустите от root или установите sudo."; fi
}
sys() { if [ -n "$SUDO" ]; then sudo "$@"; else "$@"; fi; }

detect_pm() {
  if have apt-get; then PM=apt; elif have dnf; then PM=dnf; elif have yum; then PM=yum; else PM=""; fi
}
pkg_install() {
  case "$PM" in
    apt)
      if [ "$APT_UPDATED" = 0 ]; then sys env DEBIAN_FRONTEND=noninteractive apt-get update -qq || true; APT_UPDATED=1; fi
      sys env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf) sys dnf install -y -q "$@" ;;
    yum) sys yum install -y -q "$@" ;;
    *)   die "Не удалось определить пакетный менеджер. Установите вручную: $*" ;;
  esac
}

ensure_base() {
  local need=()
  have git  || need+=(git)
  have curl || need+=(curl)
  [ "${#need[@]}" -eq 0 ] && return 0
  info "Устанавливаю зависимости: ${need[*]}"
  pkg_install "${need[@]}" ca-certificates
}

DC_SUDO=""
ensure_docker() {
  if ! { have docker && docker compose version >/dev/null 2>&1; }; then
    info "Устанавливаю Docker (get.docker.com)"
    have curl || pkg_install curl ca-certificates
    curl -fsSL https://get.docker.com | sys sh || die "Не удалось установить Docker."
    if have systemctl; then sys systemctl enable --now docker >/dev/null 2>&1 || true; fi
  fi
  have docker && docker compose version >/dev/null 2>&1 || die "Docker с плагином compose не найден."
  if docker info >/dev/null 2>&1; then DC_SUDO=""; else DC_SUDO="$SUDO"; fi
}

ensure_python() {
  have python3 || { info "Устанавливаю Python"; [ "$PM" = apt ] && pkg_install python3 python3-venv python3-pip || pkg_install python3 python3-pip; }
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Нужен Python 3.10+. На этой системе его нет — выберите режим docker."
  if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
    [ "$PM" = apt ] && { info "Устанавливаю python3-venv"; pkg_install python3-venv python3-pip; }
  fi
  python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || die "Не хватает модуля venv (python3-venv)."
}

# ---------------------------------------------------------------- Telegram
TOKEN=""
tg_api() { # метод[?query] [таймаут]; токен не попадает в список процессов
  printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOKEN" "$1" | curl -fsS --max-time "${2:-20}" -K -
}
check_token() { # проверяет токен у Telegram, печатает @username
  local out
  out="$(tg_api getMe 2>/dev/null)" || return 1
  [[ "$out" == *'"ok":true'* ]] || return 1
  echo "$out" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p'
}
detect_owner() { # ждёт НОВОЕ личное сообщение боту до 90 секунд (старые переписки не учитываются)
  local deadline=$((SECONDS + 90)) out id last off=""
  tg_api deleteWebhook >/dev/null 2>&1 || true
  last="$(tg_api 'getUpdates?offset=-1' 2>/dev/null | grep -o '"update_id":[0-9]*' | tail -n1 | grep -o '[0-9]*$' || true)"
  [ -z "$last" ] || off="&offset=$((last + 1))"
  while [ "$SECONDS" -lt "$deadline" ]; do
    out="$(tg_api "getUpdates?timeout=20&allowed_updates=%5B%22message%22%5D$off" 35 2>/dev/null)" || out=""
    id="$(parse_sender_id <<<"$out")"
    if [ -n "$id" ]; then echo "$id"; return 0; fi
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------- управление сервисом
MODE="${MODE:-}"; INSTALL_DIR="${INSTALL_DIR:-}"
interactive() { [ "$HAVE_TTY" = 1 ] && [ "$ASSUME_YES" != 1 ]; }
dc() { ( cd "$INSTALL_DIR" && $DC_SUDO docker compose "$@" ); }
has_systemd() { have systemctl && [ -d /run/systemd/system ]; }
ver() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "${1:-$INSTALL_DIR/crymoder/__init__.py}" 2>/dev/null | head -n1; }

svc_apply() { # применить новый код/настройки (пересборка и запуск)
  if [ "$MODE" = docker ]; then
    dc up -d --build --force-recreate || return 1
  else
    "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip || return 1
    "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt" || return 1
    has_systemd && { sys systemctl restart crymoder || return 1; }
  fi
}
svc_restart() {
  if [ "$MODE" = docker ]; then dc up -d --force-recreate
  elif has_systemd; then sys systemctl restart crymoder
  else warn "systemd недоступен: перезапустите вручную: $INSTALL_DIR/.venv/bin/python -m crymoder"; fi
}
svc_start() { if [ "$MODE" = docker ]; then dc up -d; elif has_systemd; then sys systemctl start crymoder; fi; }
svc_stop()  { if [ "$MODE" = docker ]; then dc stop; elif has_systemd; then sys systemctl stop crymoder; fi; }
svc_logs()  {
  if [ "$MODE" = docker ]; then dc logs -f --tail=100
  elif has_systemd; then sys journalctl -u crymoder -f -n 100
  else warn "Логов systemd нет."; fi
}
healthy() {
  if [ "$MODE" = docker ]; then
    local cid; cid="$(dc ps -q crymoder 2>/dev/null | head -n1)"; [ -n "$cid" ] || return 1
    [ "$($DC_SUDO docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$cid" 2>/dev/null)" = "true 0" ]
  elif has_systemd; then sys systemctl is-active --quiet crymoder
  else return 1; fi
}
wait_healthy() { # поднялся и не падает в цикле перезапусков
  local i up=0
  for i in 1 2 3 4 5 6 7 8 9 10; do if healthy; then up=1; break; fi; sleep 2; done
  [ "$up" = 1 ] || return 1
  sleep 10
  healthy
}

install_unit() {
  has_systemd || { warn "systemd не найден — автозапуск не настроен. Запуск: $INSTALL_DIR/.venv/bin/python -m crymoder"; return 0; }
  local u; u="$(stat -c %U "$INSTALL_DIR")"
  mkdir -p "$INSTALL_DIR/data"; sys chown "$u" "$INSTALL_DIR/data" 2>/dev/null || true
  sys tee /etc/systemd/system/crymoder.service >/dev/null <<EOF
[Unit]
Description=CRYMODER Telegram moderation bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$u
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart=$INSTALL_DIR/.venv/bin/python -m crymoder
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR/data

[Install]
WantedBy=multi-user.target
EOF
  sys systemctl daemon-reload
  sys systemctl enable crymoder >/dev/null 2>&1 || true
}

install_wrapper() { # команда `crymoder` в PATH
  local dir="/usr/local/bin"
  if [ ! -w "$dir" ] && [ -z "$SUDO" ]; then dir="$HOME/.local/bin"; mkdir -p "$dir"; fi
  printf '#!/bin/sh\nexec env INSTALL_DIR="%s" bash "%s/install.sh" "$@"\n' "$INSTALL_DIR" "$INSTALL_DIR" \
    | sys tee "$dir/crymoder" >/dev/null && sys chmod 755 "$dir/crymoder"
  case ":$PATH:" in *":$dir:"*) ;; *) warn "Добавьте $dir в PATH, чтобы работала команда crymoder." ;; esac
}

# ---------------------------------------------------------------- бэкап БД
BACKUP_PY='import glob, os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(dst), exist_ok=True)
s = sqlite3.connect(src); d = sqlite3.connect(dst); s.backup(d); d.close(); s.close()
for f in sorted(glob.glob(os.path.join(os.path.dirname(dst), "crymoder-*.db")))[:-7]:
    os.remove(f)'

db_backup() { # печатает путь копии на хосте; возвращает 1 при неудаче
  local name host_dir="$INSTALL_DIR/backups"
  name="crymoder-$(date +%Y%m%d-%H%M%S).db"
  mkdir -p "$host_dir"
  if [ "$MODE" = docker ]; then
    healthy || [ -n "$(dc ps -q crymoder 2>/dev/null)" ] || return 1
    dc exec -T crymoder python -c "$BACKUP_PY" /data/crymoder.db "/data/backups/$name" >/dev/null 2>&1 || return 1
    dc cp "crymoder:/data/backups/$name" "$host_dir/$name" >/dev/null 2>&1 || return 1
  else
    [ -f "$INSTALL_DIR/data/crymoder.db" ] || return 1
    "$INSTALL_DIR/.venv/bin/python" -c "$BACKUP_PY" "$INSTALL_DIR/data/crymoder.db" "$INSTALL_DIR/data/backups/$name" || return 1
    cp "$INSTALL_DIR/data/backups/$name" "$host_dir/$name"
  fi
  ls -1t "$host_dir"/crymoder-*.db 2>/dev/null | tail -n +8 | while IFS= read -r f; do rm -f "$f"; done
  echo "$host_dir/$name"
}

# ---------------------------------------------------------------- состояние установки
load_install() {
  INSTALL_DIR="$(resolve_dir)"
  [ -f "$INSTALL_DIR/crymoder/__main__.py" ] && [ -f "$INSTALL_DIR/.install.conf" ] || return 1
  MODE="$(conf_get MODE)"; BRANCH="$(conf_get BRANCH || true)"; BRANCH="${BRANCH:-main}"
  [ -n "$MODE" ] || return 1
  if [ "$MODE" = docker ]; then
    if docker info >/dev/null 2>&1 || [ "$(id -u)" -eq 0 ]; then DC_SUDO=""; else DC_SUDO="sudo"; fi
  fi
  return 0
}

# ---------------------------------------------------------------- домен и HTTPS
NGINX_CONF=""
nginx_paths() {
  if [ -d /etc/nginx/sites-available ]; then NGINX_CONF="/etc/nginx/sites-available/crymoder"
  else NGINX_CONF="/etc/nginx/conf.d/crymoder.conf"; fi
}
public_ip()  { curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4fsS --max-time 8 https://ifconfig.me 2>/dev/null || true; }
resolve_ip() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1; exit}' || true; }
open_firewall() {
  local st
  if have ufw; then
    st="$(sys ufw status 2>/dev/null || true)"
    [[ "$st" == *"Status: active"* ]] && { sys ufw allow 80/tcp >/dev/null; sys ufw allow 443/tcp >/dev/null; info "ufw: открыты порты 80 и 443"; }
  fi
  if have firewall-cmd && sys firewall-cmd --state >/dev/null 2>&1; then
    sys firewall-cmd --permanent --add-service=http --add-service=https >/dev/null && sys firewall-cmd --reload >/dev/null
    info "firewalld: открыты http и https"
  fi
  return 0
}
certbot_has_nginx() { # certbot может стоять без плагина nginx (snap, pip, голый пакет)
  local out; out="$(sys certbot plugins 2>/dev/null || true)"
  [[ "$out" == *nginx* ]]
}
panel_port() { # в docker порт на хосте зафиксирован в docker-compose.yml
  local p; [ "$MODE" = docker ] && { echo 8000; return; }
  p="$(env_get WEB_PORT)"; [[ "$p" =~ ^[0-9]+$ ]] || p=8000; echo "$p"
}

nginx_write_conf() { # домен порт
  nginx_paths
  sed -e "s/__DOMAIN__/$1/g" -e "s/__PORT__/$2/g" <<'EOF' | sys tee "$NGINX_CONF" >/dev/null
server {
    listen 80;
    listen [::]:80;
    server_name __DOMAIN__;
    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:__PORT__;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
EOF
  if [ -d /etc/nginx/sites-enabled ]; then sys ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/crymoder; fi
}

setup_domain() { # домен [email]
  local domain="$1" email="${2:-}" my_ip dns_ip port scheme="http" args
  valid_domain "$domain" || die "Некорректный домен: «$domain». Пример: bot.example.com"
  [ -z "$email" ] || valid_email "$email" || die "Некорректный email: $email"
  setup_privileges; detect_pm

  info "Проверяю DNS для $domain"
  my_ip="$(public_ip)"; dns_ip="$(resolve_ip "$domain")"
  if [ -z "$dns_ip" ]; then
    warn "Домен $domain пока не указывает ни на какой IP. Создайте A-запись на ${my_ip:-IP этого сервера}."
    [ "$FORCE" = 1 ] || confirm "Продолжить всё равно (Let's Encrypt, скорее всего, откажет)?" n || return 1
  elif [ -n "$my_ip" ] && [ "$dns_ip" != "$my_ip" ]; then
    warn "$domain указывает на $dns_ip, а IP этого сервера — $my_ip."
    [ "$FORCE" = 1 ] || confirm "Продолжить всё равно?" n || return 1
  else
    info "DNS в порядке: $domain → ${dns_ip}"
  fi

  have nginx   || { info "Устанавливаю nginx";   pkg_install nginx   || die "Не удалось установить nginx."; }
  have certbot || { info "Устанавливаю certbot"; pkg_install certbot || die "Не удалось установить certbot."; }
  if ! certbot_has_nginx; then
    info "Устанавливаю плагин certbot для nginx"
    pkg_install python3-certbot-nginx || true
  fi
  certbot_has_nginx || die "У certbot нет плагина nginx (certbot поставлен не из пакетов дистрибутива?). Установите python3-certbot-nginx или certbot из apt/snap и повторите: crymoder domain $domain"
  has_systemd && sys systemctl enable --now nginx >/dev/null 2>&1 || true

  port="$(panel_port)"
  nginx_write_conf "$domain" "$port"
  sys nginx -t >/dev/null 2>&1 || { sys nginx -t; die "nginx отклонил конфигурацию (см. выше)."; }
  sys nginx -s reload 2>/dev/null || { has_systemd && sys systemctl reload nginx; } || true
  open_firewall

  args=(--nginx -d "$domain" --non-interactive --agree-tos --redirect)
  if [ -n "$email" ]; then args+=(-m "$email"); else args+=(--register-unsafely-without-email); fi
  info "Получаю сертификат Let's Encrypt"
  if sys certbot "${args[@]}"; then scheme="https"
  else warn "Сертификат не выдан. Панель доступна по http://$domain. Повторить: crymoder domain $domain"; fi

  env_set PUBLIC_URL "$scheme://$domain"
  conf_set DOMAIN "$domain"; [ -z "$email" ] || conf_set EMAIL "$email"
  info "PUBLIC_URL = $scheme://$domain — перезапускаю бота"
  svc_restart
  info "Готово. Напишите боту в личку «панель» — он пришлёт ссылку для входа."
}

remove_domain() {
  setup_privileges; nginx_paths
  sys rm -f "$NGINX_CONF" /etc/nginx/sites-enabled/crymoder 2>/dev/null || true
  have nginx && { sys nginx -s reload 2>/dev/null || true; }
  env_set PUBLIC_URL "http://localhost:$(panel_port)"
  conf_set DOMAIN ""
  svc_restart
  info "Домен отключён. Панель снова только на localhost."
}

# ---------------------------------------------------------------- автообновление
autoupdate_state() { [ -f /etc/systemd/system/crymoder-update.timer ] && echo "включено" || echo "выключено"; }
cmd_autoupdate() {
  setup_privileges
  has_systemd || die "Автообновление работает через systemd, а он здесь недоступен."
  case "${1:-}" in
    on)
      sys tee /etc/systemd/system/crymoder-update.service >/dev/null <<EOF
[Unit]
Description=CRYMODER auto-update

[Service]
Type=oneshot
Environment=INSTALL_DIR=$INSTALL_DIR
ExecStart=/usr/bin/env bash $INSTALL_DIR/install.sh update --auto
EOF
      sys tee /etc/systemd/system/crymoder-update.timer >/dev/null <<EOF
[Unit]
Description=CRYMODER daily update check

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
      sys systemctl daemon-reload; sys systemctl enable --now crymoder-update.timer >/dev/null
      info "Автообновление включено (раз в сутки, с бэкапом БД и откатом при сбое)." ;;
    off)
      sys systemctl disable --now crymoder-update.timer >/dev/null 2>&1 || true
      sys rm -f /etc/systemd/system/crymoder-update.timer /etc/systemd/system/crymoder-update.service
      sys systemctl daemon-reload
      info "Автообновление выключено." ;;
    *) echo "Автообновление: $(autoupdate_state). Использование: crymoder autoupdate on|off" ;;
  esac
}

# ---------------------------------------------------------------- обновление
cmd_update() {
  load_install || die "CRYMODER не установлен (или это не каталог установки). Запустите установку без аргументов."
  setup_privileges; ensure_base
  cd "$INSTALL_DIR"
  local old_hash new_hash old_ver new_ver backup=""
  git fetch --depth 1 origin "$BRANCH" -q || die "Не удалось получить обновления с GitHub."
  new_hash="$(git rev-parse FETCH_HEAD)"; old_hash="$(git rev-parse HEAD)"
  old_ver="$(ver)"; new_ver="$(git show FETCH_HEAD:crymoder/__init__.py 2>/dev/null | sed -n 's/^__version__ = "\(.*\)"/\1/p' | head -n1)"
  if [ "$new_hash" = "$old_hash" ] && [ "$FORCE" != 1 ]; then
    info "Уже установлена последняя версия: ${old_ver:-?} (${old_hash:0:7})."; return 0
  fi
  info "Обновление: ${old_ver:-?} (${old_hash:0:7}) → ${new_ver:-?} (${new_hash:0:7})"
  if [ "$AUTO" != 1 ]; then confirm "Обновить сейчас?" y || { info "Отменено."; return 0; }; fi

  if backup="$(db_backup)"; then info "Бэкап базы: $backup"
  elif [ "$FORCE" = 1 ]; then warn "Бэкап не создан — продолжаю из-за --force."
  else die "Не удалось сделать бэкап базы (бот запущен?). Запустите его или используйте --force."; fi

  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git stash push -q -m "crymoder-update-$(date +%s)"
    warn "Ваши локальные правки файлов репозитория отложены: git -C $INSTALL_DIR stash list"
  fi
  git reset -q --hard "$new_hash"

  if svc_apply && wait_healthy; then
    info "Обновлено до ${new_ver:-?}. База мигрирует сама при запуске."
    return 0
  fi
  warn "Новая версия не запустилась — откатываюсь на ${old_ver:-?}."
  git reset -q --hard "$old_hash"
  if svc_apply && wait_healthy; then
    warn "Откат выполнен. Лог ошибки: crymoder logs. Бэкап базы: ${backup:-нет}"
  else
    die "Откат тоже не запустился. Бэкап базы: ${backup:-нет}. Смотрите: crymoder logs"
  fi
  return 1
}

# ---------------------------------------------------------------- команды управления
cmd_status() {
  load_install || die "CRYMODER не установлен."
  local run="нет" dom own url n
  healthy && run="да"
  dom="$(conf_get DOMAIN)"; own="$(env_get OWNER_IDS)"; url="$(env_get PUBLIC_URL)"
  n="$(ls -1 "$INSTALL_DIR"/backups/crymoder-*.db 2>/dev/null | wc -l | tr -d ' ')"
  echo "CRYMODER $(ver) · режим: $MODE · запущен: $run"
  echo "Каталог:      $INSTALL_DIR"
  echo "Панель:       ${url:-http://localhost:8000}${dom:+ (домен: $dom)}"
  echo "Владельцы:    ${own:-не заданы (админка недоступна — выполните: crymoder owner)}"
  echo "Бэкапов БД:   $n"
  echo "Автообновл.:  $(autoupdate_state)"
}

cmd_owner() {
  load_install || die "CRYMODER не установлен."
  local cur new="${1:-}" botname
  cur="$(env_get OWNER_IDS)"
  echo "Текущие владельцы (OWNER_IDS): ${cur:-не заданы}"
  if [ -z "$new" ]; then
    new="$(ask 'Новые ID через запятую (Enter — определить автоматически по сообщению боту): ' "")"
    if [ -z "$new" ]; then
      TOKEN="$(env_get BOT_TOKEN)"
      if healthy; then warn "Автоопределение работает, только пока бот остановлен. Останавливаю на минуту."; svc_stop; fi
      botname="$(check_token || true)"
      info "Напишите боту${botname:+ @$botname} в личку любое сообщение (например /start). Жду до 90 секунд…"
      new="$(detect_owner || true)"
      [ -n "$new" ] && info "Ваш ID: $new" || { warn "Сообщение не получено."; svc_start; return 1; }
      # автоопределённый владелец добавляется к уже заданным
      [ -z "$cur" ] || new="$cur,$new"
    fi
  fi
  valid_owners "$new" || die "ID должны быть числами через запятую: 123,456"
  env_set OWNER_IDS "$(echo "$new" | tr ',' '\n' | sort -un | paste -sd, -)"
  info "OWNER_IDS обновлён. Перезапускаю бота."
  svc_restart
}

cmd_token() {
  load_install || die "CRYMODER не установлен."
  local t="${1:-}" name
  [ -n "$t" ] || t="$(ask 'Новый токен бота от @BotFather: ' "")"
  valid_token "$t" || die "Токен выглядит неверно. Формат: 123456789:AA..."
  TOKEN="$t"; name="$(check_token)" || die "Telegram не принял токен."
  env_set BOT_TOKEN "$t"; chmod 600 "$INSTALL_DIR/.env"
  info "Токен принят (@$name). Перезапускаю бота."
  svc_restart
}

cmd_domain() {
  load_install || die "CRYMODER не установлен."
  local d="${1:-}" e="${2:-${EMAIL:-}}"
  if [ "$d" = off ]; then remove_domain; return; fi
  [ -n "$d" ] || d="${DOMAIN:-}"
  [ -n "$d" ] || d="$(ask "Домен для панели (например bot.example.com; сейчас: $(conf_get DOMAIN || true)): " "")"
  [ -n "$d" ] || die "Домен не указан."
  [ -n "$e" ] || e="$(conf_get EMAIL || true)"
  [ -n "$e" ] || e="$(ask "Email для Let's Encrypt (Enter — без email): " "")"
  setup_domain "$d" "$e"
}

cmd_backup() {
  load_install || die "CRYMODER не установлен."
  local p; p="$(db_backup)" || die "Не удалось сделать бэкап: база ещё не создана или бот не запущен."
  info "Бэкап: $p"
}

cmd_uninstall() {
  load_install || die "CRYMODER не установлен."
  setup_privileges
  confirm "Удалить сервис CRYMODER с этого сервера?" n || return 0
  local drop_data=0
  confirm "Удалить также ДАННЫЕ (база, настройки чатов, .env)? Это необратимо." n && drop_data=1
  if [ "$MODE" = docker ]; then
    if [ "$drop_data" = 1 ]; then dc down -v || true; else dc down || true; fi
  else
    has_systemd && { sys systemctl disable --now crymoder 2>/dev/null || true; sys rm -f /etc/systemd/system/crymoder.service; }
  fi
  has_systemd && cmd_autoupdate off >/dev/null 2>&1 || true
  nginx_paths; sys rm -f "$NGINX_CONF" /etc/nginx/sites-enabled/crymoder 2>/dev/null || true
  have nginx && { sys nginx -s reload 2>/dev/null || true; }
  sys rm -f /usr/local/bin/crymoder "$HOME/.local/bin/crymoder" 2>/dev/null || true
  if [ "$drop_data" = 1 ]; then cd /; sys rm -rf "$INSTALL_DIR"; info "Удалено вместе с данными."
  else info "Сервис удалён. Данные и .env сохранены в $INSTALL_DIR"; fi
}

cmd_menu() {
  load_install || die "CRYMODER не установлен."
  [ "$HAVE_TTY" = 1 ] || { cmd_status; return; }
  local c
  while true; do
    echo
    echo "${c_cyn}CRYMODER $(ver)${c_off} — управление сервером  [$INSTALL_DIR]"
    echo "  1) Статус              6) Владельцы сервера"
    echo "  2) Обновить            7) Резервная копия БД"
    echo "  3) Домен и HTTPS       8) Автообновление: $(autoupdate_state)"
    echo "  4) Логи (Ctrl+C — выход)   9) Сменить токен бота"
    echo "  5) Перезапустить       u) Удалить      0) Выход"
    c="$(ask 'Выбор: ' 0)"
    case "$c" in
      1) cmd_status ;;
      2) cmd_update || true ;;
      3) cmd_domain || true ;;
      4) svc_logs || true ;;
      5) svc_restart && info "Перезапущено." ;;
      6) cmd_owner || true ;;
      7) cmd_backup || true ;;
      8) if [ "$(autoupdate_state)" = включено ]; then cmd_autoupdate off; else cmd_autoupdate on; fi ;;
      9) cmd_token || true ;;
      u|U) cmd_uninstall || true ;;
      0|q|"") return 0 ;;
      *) warn "Нет такого пункта." ;;
    esac
  done
}

# ---------------------------------------------------------------- установка
fetch_source() {
  if [ -f "$INSTALL_DIR/crymoder/__main__.py" ]; then return 0; fi
  [ ! -e "$INSTALL_DIR" ] || [ -z "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ] \
    || die "Каталог $INSTALL_DIR не пуст и это не CRYMODER. Задайте другой: INSTALL_DIR=/путь"
  info "Скачиваю CRYMODER в $INSTALL_DIR"
  sys mkdir -p "$(dirname "$INSTALL_DIR")"
  sys git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || die "Не удалось клонировать репозиторий."
  sys chown -R "$(id -u):$(id -g)" "$INSTALL_DIR"
}

choose_mode() {
  [ -n "$MODE" ] || MODE="$(conf_get MODE 2>/dev/null || true)"
  if [ -z "$MODE" ]; then
    if [ "$ASSUME_YES" = 1 ] || [ "$HAVE_TTY" != 1 ]; then MODE=docker
    else
      echo "Способ запуска:"
      echo "  1) Docker — проще и изолированнее (рекомендуется)"
      echo "  2) Python venv + systemd — без Docker"
      case "$(ask 'Выбор [1]: ' 1)" in 2) MODE=venv ;; *) MODE=docker ;; esac
    fi
  fi
  case "$MODE" in docker|venv) ;; *) die "MODE должен быть docker или venv." ;; esac
}

cmd_install() {
  INSTALL_DIR="$(resolve_dir)"
  if [ -f "$INSTALL_DIR/.install.conf" ] && [ -f "$INSTALL_DIR/crymoder/__main__.py" ]; then
    info "CRYMODER уже установлен в $INSTALL_DIR."
    cmd_menu; return
  fi
  echo "${c_cyn}CRYMODER — установка${c_off}  (каталог: $INSTALL_DIR)"
  setup_privileges; detect_pm
  ensure_base
  fetch_source
  cd "$INSTALL_DIR"
  [ -f .env ] || cp .env.example .env
  chmod 600 .env

  # --- токен
  local name tok cur
  cur="$(env_get BOT_TOKEN)"
  tok="${BOT_TOKEN:-}"
  [ -n "$tok" ] || { [ "$cur" = "123456:replace_me" ] || [ -z "$cur" ] || tok="$cur"; }
  while true; do
    if [ -z "$tok" ]; then
      interactive || die "BOT_TOKEN не задан. Запустите: BOT_TOKEN=... bash install.sh"
      tok="$(ask 'Токен бота от @BotFather: ' "")"
    fi
    if ! valid_token "$tok"; then
      warn "Токен выглядит неверно. Формат: 123456789:AA..."
      interactive || exit 1
      tok=""; continue
    fi
    TOKEN="$tok"
    if name="$(check_token)" && [ -n "$name" ]; then info "Бот найден: @$name"; break; fi
    warn "Telegram не принял этот токен (или нет доступа к api.telegram.org)."
    interactive || exit 1
    tok=""
  done
  env_set BOT_TOKEN "$tok"

  # --- владелец сервера
  local owners="${OWNER_IDS:-$(env_get OWNER_IDS)}"
  if [ -z "$owners" ] && [ "$HAVE_TTY" = 1 ] && [ "$ASSUME_YES" != 1 ]; then
    echo
    echo "Владелец сервера получает админку в веб-панели: все чаты этого бота, администраторы, рассылки."
    owners="$(ask 'Ваш Telegram ID (число) или Enter — определить автоматически: ' "")"
    if [ -z "$owners" ]; then
      info "Напишите боту @$name в личку любое сообщение (например /start). Жду до 90 секунд…"
      owners="$(detect_owner || true)"
      if [ -n "$owners" ]; then info "Ваш ID: $owners"
      else warn "Сообщение не пришло. Задать позже: crymoder owner"; fi
    fi
  fi
  if [ -n "$owners" ]; then
    valid_owners "$owners" || die "OWNER_IDS: ID через запятую, только цифры."
    env_set OWNER_IDS "$owners"
  else
    warn "Владелец не задан — админка панели будет недоступна. Исправить: crymoder owner"
  fi

  # --- способ запуска и зависимости
  choose_mode
  if [ "$MODE" = docker ]; then ensure_docker; else ensure_python; fi
  conf_set MODE "$MODE"; conf_set BRANCH "$BRANCH"
  chmod 600 .install.conf

  # --- запуск
  if [ "$MODE" = docker ]; then
    info "Собираю и запускаю контейнер"
    dc up -d --build || die "Docker не смог собрать и запустить бота."
  else
    env_set WEB_HOST 127.0.0.1
    info "Создаю виртуальное окружение и ставлю зависимости"
    python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt \
      || die "Не удалось установить Python-зависимости."
    install_unit
    has_systemd && sys systemctl restart crymoder
  fi
  install_wrapper || warn "Не удалось создать команду crymoder."

  info "Проверяю запуск…"
  if wait_healthy; then info "Бот запущен."; else warn "Бот не запущен или падает при старте. Смотрите: crymoder logs"; fi

  # --- домен
  local domain="${DOMAIN:-}"
  if [ -z "$domain" ] && [ "$HAVE_TTY" = 1 ] && [ "$ASSUME_YES" != 1 ]; then
    echo
    domain="$(ask 'Домен для веб-панели (например bot.example.com; Enter — пропустить): ' "")"
  fi
  if [ -n "$domain" ]; then
    local email="${EMAIL:-}"
    [ -n "$email" ] || email="$(ask "Email для Let's Encrypt (Enter — без email): " "")"
    setup_domain "$domain" "$email" || warn "Домен не настроен. Повторить: crymoder domain $domain"
  fi

  # --- автообновление
  if [ "${AUTO_UPDATE:-}" = 1 ] || { [ -z "${AUTO_UPDATE:-}" ] && has_systemd && confirm "Включить автообновление (раз в сутки, с бэкапом и откатом)?" n; }; then
    cmd_autoupdate on || true
  fi

  echo
  info "Готово! CRYMODER установлен: $INSTALL_DIR"
  echo "   1. Добавьте бота в группу и сделайте админом (удаление сообщений + блокировка участников)."
  echo "   2. Напишите боту в личку «панель» — получите ссылку для входа."
  if [ -z "$(conf_get DOMAIN)" ]; then
    echo "   Домена нет: панель слушает только localhost сервера. Доступ с вашего компьютера:"
    echo "      ssh -L 8000:127.0.0.1:8000 пользователь@сервер   →   http://localhost:8000"
    echo "   Или настройте домен одной командой: crymoder domain bot.example.com"
  fi
  echo "   Управление: crymoder   (меню)  ·  crymoder update  ·  crymoder status"
}

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//' || true
}

main() {
  local args=() a
  for a in "$@"; do
    case "$a" in
      -y|--yes) ASSUME_YES=1 ;;
      --auto)   AUTO=1; ASSUME_YES=1 ;;
      --force)  FORCE=1 ;;
      -h|--help|help) usage; return 0 ;;
      *) args+=("$a") ;;
    esac
  done
  set -- ${args[@]+"${args[@]}"}
  local cmd="${1:-}"; [ "$#" -gt 0 ] && shift
  case "$cmd" in
    ""|install) if load_install 2>/dev/null; then cmd_menu; else cmd_install; fi ;;
    menu)       cmd_menu ;;
    update|upgrade) cmd_update ;;
    domain)     cmd_domain "$@" ;;
    status)     cmd_status ;;
    logs)       load_install || die "CRYMODER не установлен."; svc_logs ;;
    restart)    load_install || die "CRYMODER не установлен."; svc_restart ;;
    start)      load_install || die "CRYMODER не установлен."; svc_start ;;
    stop)       load_install || die "CRYMODER не установлен."; svc_stop ;;
    backup)     cmd_backup ;;
    owner|admin) cmd_owner "$@" ;;
    token)      cmd_token "$@" ;;
    autoupdate) load_install || die "CRYMODER не установлен."; cmd_autoupdate "$@" ;;
    uninstall)  cmd_uninstall ;;
    version|--version) load_install >/dev/null 2>&1 || INSTALL_DIR="$(resolve_dir)"; ver ;;
    *) warn "Неизвестная команда: $cmd"; usage; return 1 ;;
  esac
}

# Весь скрипт — в функциях, а запуск одной строкой: безопасно, даже если git заменит файл во время обновления.
if [ -z "${CRYMODER_SOURCED:-}" ]; then main "$@"; exit $?; fi
