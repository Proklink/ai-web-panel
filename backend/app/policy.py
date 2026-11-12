"""Политика доступа к инструментам Claude.

Каждый вызов инструмента в чате проходит через PreToolUse-хук -> /internal/hook -> decide().
Результат: ("allow" | "deny" | "ask", причина). "ask" означает: показать запрос в UI и ждать решения владельца чата.

Админ: всё разрешено, но деструктивные действия с ОС и изменения вне workspace требуют подтверждения.
Обычные пользователи: только выданные админом права; файловые операции — только внутри папки чата;
деструктивные/системные команды запрещены совсем.
"""
import os
import re
from pathlib import Path

from . import config

# Инструменты, которые сами ничего не делают с системой (планирование, задачи, субагенты, вопросы).
META_TOOLS = {
    "TodoWrite", "Task", "Agent", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput",
    "ExitPlanMode", "EnterPlanMode", "Skill", "ToolSearch", "SendUserMessage",
}
READ_TOOLS = {"Read", "Glob", "Grep", "LS"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
BASH_TOOLS = {"Bash", "BashOutput", "KillShell", "KillBash", "Monitor"}

RIGHT_TOOLS = {
    "web": ["WebSearch", "WebFetch"],
    "read": ["Read", "Glob", "Grep"],
    "write": ["Write", "Edit", "NotebookEdit"],
    "bash": ["Bash"],
}

SAFE_WRITE_ROOTS = [config.WORKSPACE, Path("/tmp")]

# Деструктивные / системные команды. Для админа — подтверждение, для остальных — запрет.
_DESTRUCTIVE = [
    (r"\brm\s+(-[^\s]*\s+)*-[^\s]*[rRf]", "рекурсивное/принудительное удаление (rm -r/-f)"),
    (r"\brm\s+[^|;&]*\s/(\s|$|[a-z])", "удаление в системных путях"),
    (r"\b(mkfs|fdisk|parted|sfdisk|wipefs|mkswap|swapoff)\b", "операции с дисками"),
    (r"\bdd\b[^|;&]*\bof=", "запись dd на устройство/файл"),
    (r"\b(shutdown|reboot|poweroff|halt|init\s+[06])\b", "выключение/перезагрузка"),
    (r"\bsystemctl\s+(stop|disable|mask|kill|restart|isolate|poweroff|reboot|halt)\b", "управление службами"),
    (r"\bservice\s+\S+\s+(stop|restart)\b", "управление службами"),
    (r"\b(apt|apt-get|dpkg|snap|pip3?|npm)\s+[^|;&]*\b(remove|purge|uninstall|autoremove|-r\b|-P\b)", "удаление пакетов"),
    (r"\b(chmod|chown|chgrp)\s+(-[^\s]*R|--recursive)", "рекурсивная смена прав"),
    (r"\b(chmod|chown)\s+[^|;&]*\s/(etc|usr|bin|sbin|lib|boot|var|root)?(\s|/|$)", "смена прав в системных путях"),
    (r"\b(kill|pkill|killall)\b", "завершение процессов"),
    (r"\b(iptables|nft|ufw|ip6tables)\b", "изменение файрвола"),
    (r"\b(useradd|userdel|usermod|passwd|chpasswd|groupdel|visudo)\b", "управление пользователями ОС"),
    (r"\bdocker\s+(rm|rmi|kill|stop|system\s+prune|volume\s+rm|network\s+rm|compose\s+down)\b", "удаление/остановка docker"),
    (r"\bgit\s+(push\s+[^|;&]*(--force|-f\b)|reset\s+--hard|clean\s+-[^\s]*f|branch\s+-D)", "необратимые git-операции"),
    (r"\bcrontab\s+-r\b", "удаление crontab"),
    (r">\s*/dev/(sd|vd|nvme|hd)", "запись на блочное устройство"),
    (r"(^|[\s;&|])>{1,2}\s*/(etc|usr|bin|sbin|lib|boot|var|root/\.ssh)/", "запись в системные файлы"),
    (r"\b(tee|cp|mv|install|ln|sed\s+-i|truncate|shred)\b[^|;&]*\s/(etc|usr|bin|sbin|lib|lib64|boot|var|opt|root/\.ssh|root/\.claude)/", "изменение системных файлов"),
    (r"\bshred\b|\bunlink\b", "необратимое удаление"),
    (r"\btmux\b[^|;&]*\bkill-(server|session)\b", "завершение tmux-сессий"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", "выполнение скрипта из интернета"),
    (r":\(\)\s*\{", "fork-бомба"),
    (r"\bsudo\b|\bsu\s", "повышение привилегий"),
    (r"\b(psql|dropdb|pg_ctl)\b[^|;&]*\b(drop|truncate|delete)\b", "удаление данных в БД"),
    (r"(/etc/webpanel|/var/lib/webpanel|\.claude/projects)", "доступ к данным webpanel/Claude"),
]
_DESTRUCTIVE_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in _DESTRUCTIVE]


def destructive_reason(command: str) -> str | None:
    for rx, why in _DESTRUCTIVE_RE:
        if rx.search(command or ""):
            return why
    return None


def _resolve(path: str, cwd: Path) -> Path:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = cwd / p
    return Path(os.path.realpath(p))


def _within(p: Path, roots) -> bool:
    for r in roots:
        r = Path(os.path.realpath(r))
        if p == r or r in p.parents:
            return True
    return False


def _tool_paths(tool: str, inp: dict) -> list[str]:
    keys = ("file_path", "path", "notebook_path")
    return [inp[k] for k in keys if isinstance(inp.get(k), str) and inp.get(k)]


def tools_for_rights(rights) -> list[str]:
    tools = []
    for r in rights or []:
        tools += RIGHT_TOOLS.get(r, [])
    return tools


def decide(user, chat_cwd: Path, tool: str, inp: dict, uploads: Path | None = None) -> tuple[str, str]:
    """uploads — папка вложений этого чата: её можно читать (с правом read) и упоминать в командах."""
    inp = inp if isinstance(inp, dict) else {}
    if user["is_admin"]:
        return _decide_admin(chat_cwd, tool, inp, uploads)
    return _decide_user(set(user["rights"] or []), chat_cwd, tool, inp, uploads)


def _strip_uploads(command: str, uploads: Path | None) -> str:
    """Убирает из команды путь к вложениям своего чата, чтобы он не считался «данными webpanel»."""
    if not uploads:
        return command
    return command.replace(str(uploads), "./uploads")


def _decide_admin(cwd: Path, tool: str, inp: dict, uploads: Path | None = None):
    if tool == "AskUserQuestion":
        return "deny", "Интерактивные вопросы недоступны в веб-чате: задай вопрос обычным текстом"
    if tool in BASH_TOOLS:
        why = destructive_reason(_strip_uploads(str(inp.get("command", "")), uploads))
        return ("ask", f"Опасная команда: {why}") if why else ("allow", "")
    if tool in WRITE_TOOLS:
        for p in _tool_paths(tool, inp):
            rp = _resolve(p, cwd)
            if not _within(rp, SAFE_WRITE_ROOTS + [cwd]):
                return "ask", f"Изменение файла вне workspace: {rp}"
            if _within(rp, [config.CLAUDE_HOME, Path("/etc/webpanel"), config.DATA_DIR]):
                return "ask", f"Изменение служебных файлов: {rp}"
        return "allow", ""
    return "allow", ""


def _decide_user(rights: set, cwd: Path, tool: str, inp: dict, uploads: Path | None = None):
    if tool in META_TOOLS:
        return "allow", ""
    if tool.startswith("mcp__"):
        return "deny", "MCP-инструменты недоступны для этого пользователя"
    if tool in WEB_TOOLS:
        return ("allow", "") if "web" in rights else ("deny", "Нет права на доступ в интернет")
    if tool in READ_TOOLS or tool in WRITE_TOOLS:
        need = "read" if tool in READ_TOOLS else "write"
        if need not in rights:
            return "deny", "Нет права на " + ("чтение" if need == "read" else "изменение") + " файлов"
        paths = _tool_paths(tool, inp) or [str(cwd)]
        roots = [cwd] + ([uploads] if uploads and need == "read" else [])
        for p in paths:
            rp = _resolve(p, cwd)
            if not _within(rp, roots):
                return "deny", f"Доступ разрешён только внутри папки чата ({cwd})"
        return "allow", ""
    if tool in BASH_TOOLS:
        if "bash" not in rights:
            return "deny", "Нет права на выполнение команд"
        cmd = _strip_uploads(str(inp.get("command", "")), uploads)
        why = destructive_reason(cmd)
        if why:
            return "deny", f"Запрещено для пользователя: {why}"
        return "allow", ""
    if tool == "AskUserQuestion":
        return "deny", "Интерактивные вопросы недоступны: задай вопрос обычным текстом"
    return "deny", f"Инструмент {tool} не разрешён"
