#!/usr/bin/env bash
# Webpanel — управление установкой и запуском. Справка: ./webpanel.sh help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE=/etc/webpanel/webpanel.env
SETUP_CODE=/etc/webpanel/setup-code
DATA_DIR=/var/lib/webpanel
UNIT=/etc/systemd/system/webpanel.service
CADDYFILE=/etc/caddy/Caddyfile
TMUX="tmux -L webpanel"
PY="$ROOT/.venv/bin/python"

c_ok() { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || die "нужны права root (sudo $0 $*)"; }
psql_wp() { sudo -u postgres psql -qtAX -d webpanel "$@"; }

usage() {
  cat <<'EOF'
Webpanel — веб-интерфейс для диалогов с Claude Code (tmux + FastAPI + React + PostgreSQL)

Использование: ./webpanel.sh <команда> [параметры]

Установка и обновление
  install [--ip IP | --domain ДОМЕН] [--no-firewall]
                     Полная установка: пакеты, PostgreSQL, venv, сборка фронта,
                     systemd-служба, Caddy (HTTPS), правило ufw для 443/tcp.
                     По умолчанию — HTTPS на внешний IP с самоподписанным
                     сертификатом Caddy (для отладки, пока нет домена).
  update             Пересобрать фронтенд, обновить зависимости и перезапустить
                     службу. Открытые диалоги (tmux) не прерываются.
  build              Только собрать фронтенд (frontend/dist).

Работа службы
  start | stop | restart | status
                     Управление systemd-службой webpanel.
                     stop не закрывает открытые диалоги; stop --all закрывает и их.
  logs [-f] [N]      Журнал службы (последние N строк, -f — следить).
  dev                Запустить бэкенд в терминале (без systemd) для отладки.
                     Фронтенд в режиме разработки: cd frontend && npm run dev

Пользователи и диалоги
  setup-code         Показать код первичной настройки (для создания админа).
  passwd ЛОГИН       Сбросить пароль пользователя (спросит новый пароль).
  users              Список пользователей.
  sessions           Список открытых диалогов (tmux-сессий) с памятью.
  attach ID          Подключиться к tmux-сессии диалога (ID или начало ID).
                     Выход без закрытия диалога: Ctrl+B, затем D.

Доступ (сертификаты устройств, mTLS)
  Без сертификата устройства сервер не принимает даже TLS-соединение — боты не видят
  ничего. Сертификаты выпускаются во вкладке «Управление» (скачивается .p12).
  open-window [МИН]  Временно (по умолчанию 30 мин, максимум 240) пустить без сертификата
                     — только администратора. Нужно, чтобы выпустить первый сертификат или
                     зайти с нового устройства.
  close-window       Закрыть окно сразу.
  access             Текущий режим и до какого времени открыто окно.

Обслуживание
  backup [ПАПКА]     Резервная копия БД и данных чатов (по умолчанию /root/webpanel-backups).
  bans               Заблокированные fail2ban IP (unban IP — разблокировать).
  ca-cert            Путь к корневому сертификату Caddy: установите его на устройства,
                     чтобы браузер не ругался на самоподписанный сертификат.
  help               Эта справка.

Примеры
  sudo ./webpanel.sh install                    # HTTPS на внешний IP сервера (самоподписанный)
  sudo ./webpanel.sh install --domain chat.example.com   # Let's Encrypt для домена
  ./webpanel.sh setup-code                      # код для первого входа (создание админа)
  sudo ./webpanel.sh open-window 20             # 20 минут вход админа без сертификата
  ./webpanel.sh access                          # открыто ли сейчас окно
  ./webpanel.sh logs -f                         # смотреть журнал в реальном времени
  ./webpanel.sh sessions                        # какие диалоги сейчас в памяти
  ./webpanel.sh attach 7bd6                     # посмотреть, что делает Claude в диалоге
  ./webpanel.sh passwd phone                    # сменить пароль устройству «phone»
  ./webpanel.sh update                          # после изменения кода
  ./webpanel.sh stop --all                      # остановить всё, включая открытые диалоги

Файлы
  /etc/webpanel/webpanel.env   секреты (пароль БД, ключ), права 600
  /var/lib/webpanel/chats/     история и контекст диалогов
  /etc/webpanel/client-ca/     CA сертификатов устройств (ключ — только root)
  /etc/webpanel/caddy.json     адрес сервера для генерации Caddyfile
  /etc/caddy/Caddyfile         генерируется автоматически (не править вручную)
EOF
}

detect_ip() { curl -s -m 5 https://ifconfig.me || hostname -I | awk '{print $1}'; }

cmd_install() {
  need_root install
  local ip="" domain="" firewall=1 address="" first_window=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --ip) ip="$2"; shift 2 ;;
      --domain) domain="$2"; shift 2 ;;
      --no-firewall) firewall=0; shift ;;
      *) die "неизвестный параметр $1" ;;
    esac
  done
  command -v claude >/dev/null || [ -x /root/.local/bin/claude ] || die "Claude Code не установлен (нужен /root/.local/bin/claude) и должен быть залогинен"

  c_ok "1/7 Пакеты"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq postgresql nodejs npm caddy tmux python3-venv python3-dev curl fail2ban >/dev/null

  c_ok "2/7 PostgreSQL"
  local pgconf; pgconf=$(ls -d /etc/postgresql/*/main/conf.d | tail -1)
  cat > "$pgconf/webpanel.conf" <<'PG'
# Облегчённый профиль для маленького сервера (webpanel)
listen_addresses = 'localhost'
max_connections = 20
shared_buffers = 32MB
work_mem = 2MB
maintenance_work_mem = 16MB
effective_cache_size = 256MB
password_encryption = scram-sha-256
PG
  systemctl enable -q --now postgresql
  systemctl restart postgresql
  mkdir -p /etc/webpanel && chmod 700 /etc/webpanel
  if [ ! -f "$ENV_FILE" ]; then
    local pw; pw=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
    sudo -u postgres psql -qc "CREATE ROLE webpanel LOGIN PASSWORD '$pw';"
    sudo -u postgres psql -qc "CREATE DATABASE webpanel OWNER webpanel;"
    printf 'WEBPANEL_DATABASE_URL=postgresql://webpanel:%s@127.0.0.1:5432/webpanel\nWEBPANEL_SECRET=%s\n' \
      "$pw" "$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  else
    echo "   $ENV_FILE уже есть — база и секреты не трогаются"
  fi

  c_ok "3/7 Python-окружение"
  [ -d "$ROOT/.venv" ] || python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q --upgrade pip
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

  c_ok "4/7 Фронтенд"
  cmd_build

  c_ok "5/7 systemd"
  mkdir -p "$DATA_DIR/chats" && chmod 700 "$DATA_DIR"
  sed "s#__ROOT__#$ROOT#g" "$ROOT/deploy/webpanel.service" > "$UNIT"
  systemctl daemon-reload
  systemctl enable -q webpanel
  systemctl restart webpanel

  c_ok "6/7 Caddy (HTTPS + сертификаты устройств)"
  [ -f "$CADDYFILE" ] && [ ! -f "$CADDYFILE.orig" ] && cp "$CADDYFILE" "$CADDYFILE.orig"
  if [ -n "$domain" ]; then
    printf '{"address": "%s", "tls_internal": false, "default_sni": null}\n' "$domain" > /etc/webpanel/caddy.json
    address="https://$domain"
  else
    [ -n "$ip" ] || ip=$(detect_ip)
    [ -n "$ip" ] || die "не удалось определить IP, укажите --ip"
    printf '{"address": "https://%s", "tls_internal": true, "default_sni": "%s"}\n' "$ip" "$ip" > /etc/webpanel/caddy.json
    address="https://$ip"
  fi
  chmod 600 /etc/webpanel/caddy.json
  systemctl enable -q caddy
  local certs_n; certs_n=$(psql_wp -c "SELECT count(*) FROM client_certs WHERE revoked_at IS NULL" 2>/dev/null || echo 0)
  if [ "${certs_n:-0}" = 0 ]; then
    caddyconf open 60 >/dev/null
    first_window=1
  fi
  caddyconf sync --force >/dev/null

  c_ok "7/7 Файрвол и fail2ban"
  if [ "$firewall" = 1 ] && command -v ufw >/dev/null; then
    ufw allow 443/tcp comment 'Webpanel HTTPS' >/dev/null
    [ -n "$domain" ] && ufw allow 80/tcp comment 'Webpanel ACME' >/dev/null
    echo "   открыт 443/tcp${domain:+ и 80/tcp}"
  fi
  # fail2ban: бан IP после 5 неудачных входов за 15 минут (второй слой после сертификатов устройств)
  apt-get install -y -qq fail2ban >/dev/null
  cp "$ROOT/deploy/fail2ban/filter-webpanel.conf" /etc/fail2ban/filter.d/webpanel.conf
  cp "$ROOT/deploy/fail2ban/jail-webpanel.conf" /etc/fail2ban/jail.d/webpanel.conf
  systemctl enable -q --now fail2ban
  fail2ban-client reload >/dev/null && echo "   fail2ban: jail webpanel включён"

  sleep 2
  echo
  c_ok "Готово: $address"
  if [ -f "$SETUP_CODE" ]; then
    echo "Код первичной настройки (создание админа): $(cat "$SETUP_CODE")"
  fi
  if [ "$first_window" = 1 ]; then
    c_warn "Сертификатов устройств ещё нет, поэтому вход без сертификата (только для админа) открыт на 60 минут."
    c_warn "Войдите, выпустите себе сертификат во вкладке «Управление», установите его. Окно закроется через 60 минут или кнопкой «Закрыть сейчас»."
  else
    echo "Вход только с сертификатом устройства. Временно пустить админа без сертификата: ./webpanel.sh open-window 30"
  fi
  [ -z "$domain" ] && c_warn "HTTPS-сертификат сервера самоподписанный: браузер предупредит. Убрать — ./webpanel.sh ca-cert"
}

caddyconf() { (cd "$ROOT/backend" && "$PY" -m app.caddyconf "$@"); }

cmd_build() {
  cd "$ROOT/frontend"
  if [ -f package-lock.json ]; then npm ci --silent --no-audit --no-fund; else npm install --silent --no-audit --no-fund; fi
  npm run build --silent
  cd "$ROOT"
}

cmd_update() {
  need_root update
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
  cmd_build
  systemctl restart webpanel
  c_ok "Обновлено, служба перезапущена (открытые диалоги не тронуты)"
}

cmd_stop() {
  need_root stop
  systemctl stop webpanel
  if [ "${1:-}" = "--all" ]; then
    $TMUX kill-server 2>/dev/null || true
    psql_wp -c "UPDATE chats SET state = 'suspended', notice = 'Сервер остановлен администратором (stop --all). Контекст сохранён.' WHERE state = 'running'" || true
    c_ok "Служба и все открытые диалоги остановлены"
  else
    c_ok "Служба остановлена. Открытые диалоги продолжают работать в tmux (stop --all — закрыть и их)"
  fi
}

cmd_logs() {
  local follow="" n=100
  for a in "$@"; do case "$a" in -f) follow="-f" ;; *[0-9]*) n="$a" ;; esac; done
  journalctl -u webpanel -n "$n" --no-pager $follow
}

cmd_dev() {
  need_root dev
  systemctl is-active -q webpanel && die "служба уже запущена (./webpanel.sh stop), порт 8100 занят"
  set -a; . "$ENV_FILE"; set +a
  cd "$ROOT/backend"
  exec "$ROOT/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8100 --proxy-headers --forwarded-allow-ips 127.0.0.1 --reload
}

cmd_setup_code() {
  if [ -f "$SETUP_CODE" ]; then
    echo "Код первичной настройки: $(cat "$SETUP_CODE")"
  else
    echo "Код не нужен: администратор уже создан."
  fi
}

cmd_passwd() {
  need_root passwd
  local u="${1:-}"; [ -n "$u" ] || die "укажите логин: ./webpanel.sh passwd ЛОГИН"
  [ -n "$(psql_wp -v u="$u" <<< "SELECT 1 FROM users WHERE lower(username) = lower(:'u');")" ] || die "пользователь $u не найден"
  local p1 p2
  read -rsp "Новый пароль для $u (от 10 символов): " p1; echo
  read -rsp "Повторите: " p2; echo
  [ "$p1" = "$p2" ] || die "пароли не совпадают"
  [ ${#p1} -ge 10 ] || die "пароль короче 10 символов"
  local h; h=$(printf '%s' "$p1" | "$PY" -c 'import sys; from argon2 import PasswordHasher; print(PasswordHasher().hash(sys.stdin.read()))')
  psql_wp -v u="$u" -v h="$h" <<< "UPDATE users SET password_hash = :'h' WHERE lower(username) = lower(:'u');
DELETE FROM auth_sessions WHERE user_id = (SELECT id FROM users WHERE lower(username) = lower(:'u'));"
  c_ok "Пароль изменён, все устройства пользователя разлогинены"
}

cmd_users() {
  psql_wp -F $'\t' -c "SELECT id, username, CASE WHEN is_admin THEN 'admin' ELSE array_to_string(rights, ',') END, max_sessions, coalesce(to_char(last_login, 'YYYY-MM-DD HH24:MI'), '-') FROM users ORDER BY id" \
    | (printf 'ID\tЛОГИН\tПРАВА\tЛИМИТ\tПОСЛЕДНИЙ ВХОД\n'; cat) | column -t -s $'\t'
}

cmd_sessions() {
  local names; names=$($TMUX list-sessions -F '#{session_name} #{pane_pid}' 2>/dev/null | grep '^wp-' || true)
  if [ -z "$names" ]; then echo "Открытых диалогов нет"; return; fi
  printf '%-38s %-12s %8s  %s\n' "ID" "ПОЛЬЗОВАТЕЛЬ" "RAM" "НАЗВАНИЕ"
  while read -r name pid; do
    local id="${name#wp-}" rss info
    rss=$("$PY" -c 'import sys, psutil
p = psutil.Process(int(sys.argv[1])); ps = [p] + p.children(recursive=True)
print(sum(x.memory_info().rss for x in ps) // 2**20)' "$pid" 2>/dev/null || echo "?")
    info=$(psql_wp -F '|' -v id="$id" <<< "SELECT u.username, c.title FROM chats c JOIN users u ON u.id = c.user_id WHERE c.id = :'id';" 2>/dev/null || true)
    printf '%-38s %-12s %5s МБ  %s\n' "$id" "${info%%|*}" "$rss" "${info#*|}"
  done <<< "$names"
}

cmd_attach() {
  local q="${1:-}"; [ -n "$q" ] || die "укажите ID диалога: ./webpanel.sh sessions"
  local m; m=$($TMUX list-sessions -F '#{session_name}' 2>/dev/null | grep "^wp-$q" || true)
  [ -n "$m" ] || die "сессия wp-$q* не найдена"
  [ "$(echo "$m" | wc -l)" = 1 ] || die "под «$q» подходит несколько сессий, уточните ID"
  echo "Подключение к $m. Выйти, не закрывая диалог: Ctrl+B, затем D"
  sleep 1
  exec $TMUX attach -t "=$m"
}

cmd_backup() {
  need_root backup
  local dir="${1:-/root/webpanel-backups}" ts; ts=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$dir" && chmod 700 "$dir"
  sudo -u postgres pg_dump -Fc webpanel > "$dir/webpanel-db-$ts.dump"
  tar -czf "$dir/webpanel-data-$ts.tar.gz" -C / "${DATA_DIR#/}" 2>/dev/null || true
  chmod 600 "$dir"/*
  c_ok "Резервная копия: $dir/webpanel-{db,data}-$ts.*"
  echo "Транскрипты активных диалогов лежат в ~/.claude/projects (выгруженные — внутри архива данных)."
}

cmd_ca() {
  local f=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
  if [ -f "$f" ]; then
    echo "Корневой сертификат Caddy: $f"
    echo "Скопируйте его на устройство и установите как доверенный (Android: Настройки → Безопасность → Шифрование → Установить сертификат CA;"
    echo "iOS: открыть файл → Профиль → Установить, затем Основные → Об этом устройстве → Доверие сертификатам)."
    echo "Скачать на свой компьютер: scp root@<сервер>:$f ./webpanel-ca.crt"
  else
    echo "Сертификат ещё не создан (Caddy создаёт его при первом HTTPS-запросе) или используется домен с Let's Encrypt."
  fi
}

case "${1:-help}" in
  install) shift; cmd_install "$@" ;;
  update) cmd_update ;;
  build) cmd_build ;;
  start) need_root start; systemctl start webpanel; c_ok "Запущено" ;;
  stop) shift; cmd_stop "$@" ;;
  restart) need_root restart; systemctl restart webpanel; c_ok "Перезапущено (открытые диалоги не тронуты)" ;;
  status) systemctl --no-pager status webpanel caddy postgresql | grep -E '●|Active:' ; echo; cmd_sessions ;;
  logs) shift; cmd_logs "$@" ;;
  dev) cmd_dev ;;
  setup-code) cmd_setup_code ;;
  passwd) shift; cmd_passwd "$@" ;;
  users) cmd_users ;;
  sessions) cmd_sessions ;;
  attach) shift; cmd_attach "$@" ;;
  backup) shift; cmd_backup "$@" ;;
  ca-cert) cmd_ca ;;
  bans) fail2ban-client status webpanel ;;
  unban) need_root unban; [ -n "${2:-}" ] || die "укажите IP"; fail2ban-client set webpanel unbanip "$2" ;;
  open-window) need_root open-window; caddyconf open "${2:-30}" ;;
  close-window) need_root close-window; caddyconf close ;;
  access) caddyconf status ;;
  help|-h|--help) usage ;;
  *) c_warn "Неизвестная команда: $1"; echo; usage; exit 1 ;;
esac
