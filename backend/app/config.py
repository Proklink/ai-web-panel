"""Конфигурация сервера. Секреты приходят из /etc/webpanel/webpanel.env (EnvironmentFile в systemd)."""
import os
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
PROJECT_DIR = BASE_DIR.parent                               # webpanel/
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"

DATABASE_URL = os.environ.get("WEBPANEL_DATABASE_URL", "")
SECRET = os.environ.get("WEBPANEL_SECRET", "")

DATA_DIR = Path(os.environ.get("WEBPANEL_DATA_DIR", "/var/lib/webpanel"))
CHATS_DIR = DATA_DIR / "chats"
SETUP_CODE_FILE = Path(os.environ.get("WEBPANEL_SETUP_CODE_FILE", "/etc/webpanel/setup-code"))

WORKSPACE = Path(os.environ.get("WEBPANEL_WORKSPACE", "/root/workspace")).resolve()
CLAUDE_HOME = Path(os.environ.get("WEBPANEL_CLAUDE_HOME", str(Path.home() / ".claude")))
CLAUDE_BIN = os.environ.get("WEBPANEL_CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")

HOST = "127.0.0.1"
PORT = int(os.environ.get("WEBPANEL_PORT", "8100"))
HOOK_URL = f"http://{HOST}:{PORT}/internal/hook"

# Отдельный сокет tmux: наши сессии не смешиваются с личными сессиями пользователя.
TMUX_SOCKET = "webpanel"
TMUX_PREFIX = "wp-"

PYTHON = sys.executable
RUNNER = BASE_DIR / "runner.py"
HOOK = BASE_DIR / "hook.py"

# Куки сессии
COOKIE_NAME = "wp_session"
SESSION_TTL_DAYS = 30

# Значения по умолчанию для настраиваемых админом параметров (таблица settings)
DEFAULT_SETTINGS = {
    "ram_reserve_mb": 1024,        # всегда оставлять свободной системе
    "ram_critical_mb": 512,        # ниже — экстренное завершение занятых чатов обычных пользователей
    "session_estimate_mb": 400,    # минимальная оценка памяти на одну сессию claude
    "default_max_sessions": 2,     # лимит активных сессий для новых пользователей
    "approval_timeout_s": 600,     # сколько ждать подтверждения опасного действия
}

UPLOAD_MAX_MB = int(os.environ.get("WEBPANEL_UPLOAD_MAX_MB", "25"))                # один файл
UPLOAD_CHAT_QUOTA_MB = int(os.environ.get("WEBPANEL_UPLOAD_CHAT_QUOTA_MB", "500"))  # все вложения диалога

RIGHTS = {
    "web": "Поиск и чтение веб-страниц (WebSearch, WebFetch)",
    "read": "Чтение файлов в папке чата/проекта (Read, Glob, Grep)",
    "write": "Изменение файлов в папке чата/проекта (Write, Edit)",
    "bash": "Выполнение команд (Bash) — опасно: процесс работает от root",
}
