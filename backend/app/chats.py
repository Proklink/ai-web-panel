"""Управление чатами: tmux-сессии с раннерами, связь с ними, память, выгрузка на диск, подтверждения."""
import asyncio
import gzip
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil
import re

from . import config, db, policy

log = logging.getLogger("webpanel.chats")
MB = 1024 * 1024


class StartError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def as_dict(self):
        return {"code": self.code, "message": self.message, **self.extra}


# --------------------------------------------------------------------------- WebSocket-хаб
class Client:
    def __init__(self, ws, user):
        self.ws, self.user = ws, user
        self.subs: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=5000)

    def push(self, msg: dict):
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # медленный клиент: сообщения теряются, при переподключении он догрузит историю


class Hub:
    def __init__(self):
        self.clients: dict[int, set[Client]] = {}

    def add(self, c: Client):
        self.clients.setdefault(c.user["id"], set()).add(c)

    def remove(self, c: Client):
        self.clients.get(c.user["id"], set()).discard(c)

    def to_user(self, user_id: int, msg: dict):
        for c in list(self.clients.get(user_id, ())):
            c.push(msg)

    def to_chat(self, user_id: int, chat_id: str, msg: dict):
        for c in list(self.clients.get(user_id, ())):
            if chat_id in c.subs:
                c.push(msg)

    def to_admins(self, msg: dict):
        for cs in self.clients.values():
            for c in list(cs):
                if c.user["is_admin"]:
                    c.push(msg)

    def kick_tokens(self, token_hashes: set):
        if not token_hashes:
            return
        for cs in self.clients.values():
            for c in list(cs):
                if c.user["token_hash"] in token_hashes:
                    c.push({"t": "logout"})

    def kick_user(self, user_id: int):
        for c in list(self.clients.get(user_id, ())):
            c.push({"t": "logout"})


hub = Hub()


# --------------------------------------------------------------------------- файлы чата
def chat_dir(chat_id) -> Path:
    return config.CHATS_DIR / str(chat_id)


def tmux_name(chat_id) -> str:
    return config.TMUX_PREFIX + str(chat_id)


def chat_cwd(chat) -> Path:
    if chat["project"]:
        return (config.WORKSPACE / chat["project"]).resolve()
    return chat_dir(chat["id"]) / "work"


def read_events(chat_id, after: int = 0) -> list[dict]:
    d = chat_dir(chat_id)
    out = []
    for p, opener in ((d / "events.jsonl.gz", gzip.open), (d / "events.jsonl", open)):
        if p.exists():
            try:
                with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if ev.get("seq", 0) > after:
                            out.append(ev)
            except (OSError, EOFError) as e:
                log.warning("read_events %s: %s", p, e)
    return out


def last_seq(chat_id) -> int:
    evs = read_events(chat_id)
    return max((e.get("seq", 0) for e in evs), default=0)


def append_offline_event(chat_id, kind: str, d: dict, seq_hint: int | None = None) -> dict:
    """Запись события, когда раннер не запущен (чат выгружен)."""
    cd = chat_dir(chat_id)
    seq = (seq_hint if seq_hint is not None else last_seq(chat_id)) + 1
    ev = {"seq": seq, "ts": time.time(), "k": kind, "d": d}
    line = json.dumps(ev, ensure_ascii=False) + "\n"
    gz = cd / "events.jsonl.gz"
    if gz.exists():
        with gzip.open(gz, "at", encoding="utf-8") as f:  # новый gzip-член, читается как один поток
            f.write(line)
    else:
        with (cd / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)
    return ev


def chat_session_ids(row) -> list[str]:
    ids = {str(row["claude_session_id"])}
    ids.update(str(x) for x in (row.get("past_session_ids") if hasattr(row, "get") else row["past_session_ids"]) or [])
    return sorted(ids)


def _claude_state_paths(session_ids) -> list[Path]:
    """Файлы, которые Claude Code хранит для сессий чата."""
    if isinstance(session_ids, (str, uuid.UUID)):
        session_ids = [session_ids]
    sids = [str(s) for s in session_ids]
    h = config.CLAUDE_HOME
    found = []
    proj = h / "projects"
    if proj.exists():
        for d in proj.iterdir():
            for sid in sids:
                for cand in (d / f"{sid}.jsonl", d / sid):
                    if cand.exists():
                        found.append(cand)
    for sub in ("session-env", "file-history", "todos", "tasks", "debug"):
        base = h / sub
        if base.exists():
            for p in base.iterdir():
                if any(sid in p.name for sid in sids):
                    found.append(p)
    return found


def _encoded_project_dir(cwd: Path) -> Path:
    return config.CLAUDE_HOME / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def archive_claude_state(chat_id, session_ids) -> int:
    """Сжимает транскрипт Claude в архив внутри папки чата и убирает оригиналы. Возвращает размер архива."""
    paths = [p for p in _claude_state_paths(session_ids) if (config.CLAUDE_HOME / "projects") in p.parents]
    arch = chat_dir(chat_id) / "claude-state.tar.gz"
    if not paths:
        return arch.stat().st_size if arch.exists() else 0
    tmp = arch.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
        for p in paths:
            tar.add(p, arcname=str(p.relative_to(config.CLAUDE_HOME)))
    tmp.replace(arch)
    for p in paths:
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
    return arch.stat().st_size


def restore_claude_state(chat_id):
    arch = chat_dir(chat_id) / "claude-state.tar.gz"
    if arch.exists():
        with tarfile.open(arch, "r:gz") as tar:
            tar.extractall(config.CLAUDE_HOME, filter="data")
        arch.unlink()


def compress_events(chat_id):
    cd = chat_dir(chat_id)
    src = cd / "events.jsonl"
    if not src.exists():
        return
    dst = cd / "events.jsonl.gz"
    with src.open("rb") as fi, gzip.open(dst, "ab", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    src.unlink()


def decompress_events(chat_id):
    cd = chat_dir(chat_id)
    gz = cd / "events.jsonl.gz"
    if not gz.exists():
        return
    with gzip.open(gz, "rb") as fi, (cd / "events.jsonl").open("ab") as fo:
        shutil.copyfileobj(fi, fo)
    gz.unlink()


def wipe_claude_state(session_ids, chat_id):
    """Полное удаление следов чата в ~/.claude."""
    sids = [str(s) for s in session_ids]
    cid = str(chat_id)
    for p in _claude_state_paths(sids):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    # папка проекта Claude для личной рабочей папки чата целиком принадлежит чату
    shutil.rmtree(_encoded_project_dir(chat_dir(cid) / "work"), ignore_errors=True)
    # реестр запущенных сессий (остаётся, если процесс убит SIGKILL)
    sess = config.CLAUDE_HOME / "sessions"
    if sess.exists():
        for f in sess.glob("*.json"):
            try:
                txt = f.read_text(errors="replace")
            except OSError:
                continue
            if cid in txt or any(s in txt for s in sids):
                f.unlink(missing_ok=True)
                for k in sess.glob(f.stem + ".*.key"):
                    k.unlink(missing_ok=True)
    hist = config.CLAUDE_HOME / "history.jsonl"
    try:
        if hist.exists():
            txt = hist.read_text(errors="replace")
            if any(s in txt for s in sids + [cid]):
                hist.write_text("".join(l for l in txt.splitlines(True) if not any(s in l for s in sids + [cid])))
    except OSError:
        pass


def dir_size(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------- tmux
_CLEAN_ENV = {
    "PATH": f"{Path.home()}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": str(Path.home()), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "xterm-256color",
    "USER": "root", "SHELL": "/bin/bash",
}


async def tmux(*args, check=False) -> tuple[int, str]:
    # Чистое окружение: секреты веб-сервера (пароль БД и т.п.) не должны попасть в tmux и в Claude.
    # new-session может запустить tmux-сервер, который унаследует наши pipe'ы и никогда их не закроет,
    # поэтому вывод пишем во временный файл, а не в pipe.
    with tempfile.TemporaryFile() as outf:
        p = await asyncio.create_subprocess_exec(
            "tmux", "-L", config.TMUX_SOCKET, "-f", "/dev/null", *args,
            stdin=asyncio.subprocess.DEVNULL, stdout=outf, stderr=outf, env=_CLEAN_ENV)
        await p.wait()
        outf.seek(0)
        out = outf.read()
    if check and p.returncode:
        raise RuntimeError(f"tmux {args[0]}: {out.decode(errors='replace').strip()}")
    return p.returncode, out.decode(errors="replace")


async def tmux_sessions() -> set[str]:
    code, out = await tmux("list-sessions", "-F", "#{session_name}")
    return set(out.split()) if code == 0 else set()


async def tmux_pane_pid(name: str) -> int | None:
    code, out = await tmux("list-panes", "-t", f"={name}", "-F", "#{pane_pid}")
    try:
        return int(out.split()[0]) if code == 0 else None
    except (IndexError, ValueError):
        return None


def proc_tree(pid: int | None) -> list[psutil.Process]:
    if not pid:
        return []
    try:
        p = psutil.Process(pid)
        return [p] + p.children(recursive=True)
    except psutil.Error:
        return []


def tree_rss(pid: int | None) -> int:
    total = 0
    for p in proc_tree(pid):
        try:
            total += p.memory_info().rss
        except psutil.Error:
            pass
    return total


def kill_pid(pid: int | None):
    """Убить процесс claude вместе с его группой (он запущен в отдельной сессии)."""
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_tree(pid: int | None):
    for p in reversed(proc_tree(pid)):
        try:
            p.kill()
        except psutil.Error:
            pass


# --------------------------------------------------------------------------- рантайм чатов
@dataclass
class Approval:
    id: str
    chat_id: str
    tool: str
    input: dict
    reason: str
    created: float
    future: asyncio.Future

    def public(self):
        return {"id": self.id, "chat_id": self.chat_id, "tool": self.tool, "input": self.input,
                "reason": self.reason, "created": self.created}


@dataclass
class Runtime:
    chat_id: str
    user_id: int
    is_admin: bool
    hook_token: str
    started: float = field(default_factory=time.time)
    busy: bool = False
    seq: int = 0
    pane_pid: int | None = None
    claude_pid: int | None = None
    reader: asyncio.Task | None = None
    writer: asyncio.StreamWriter | None = None
    stopping: str | None = None          # причина, если мы сами останавливаем
    exit_info: dict | None = None
    approvals: dict[str, Approval] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.time)


def rt_rss(rt: "Runtime") -> int:
    return tree_rss(rt.pane_pid)


class ChatManager:
    def __init__(self):
        self.rt: dict[str, Runtime] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.start_lock = asyncio.Lock()
        self.memory_task: asyncio.Task | None = None
        self.last_guard_action = 0.0

    def lock(self, chat_id) -> asyncio.Lock:
        return self.locks.setdefault(str(chat_id), asyncio.Lock())

    # ---------- сериализация для фронта
    def public_chat(self, row) -> dict:
        cid = str(row["id"])
        rt = self.rt.get(cid)
        return {
            "id": cid, "title": row["title"], "project": row["project"], "model": row["model"],
            "state": "running" if rt else "suspended", "busy": bool(rt and rt.busy),
            "notice": row["notice"], "created_at": row["created_at"].isoformat(),
            "last_activity": row["last_activity"].isoformat(),
            "approvals": [a.public() for a in rt.approvals.values()] if rt else [],
            "rss_mb": round(rt_rss(rt) / MB) if rt else 0,
            "has_transcript": row["has_transcript"],
        }

    async def push_chat(self, chat_id):
        row = await db.fetchrow("SELECT * FROM chats WHERE id = $1", uuid.UUID(str(chat_id)))
        if row:
            hub.to_user(row["user_id"], {"t": "chat", "chat": self.public_chat(row)})
            hub.to_admins({"t": "sessions_changed"})

    # ---------- запуск при старте сервера
    async def startup(self):
        config.CHATS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(config.DATA_DIR, 0o700)
        alive = await tmux_sessions()
        rows = await db.fetch("SELECT c.*, u.is_admin FROM chats c JOIN users u ON u.id = c.user_id WHERE state = 'running'")
        for row in rows:
            cid = str(row["id"])
            name = tmux_name(cid)
            tok_file = chat_dir(cid) / ".hook_token"
            if name in alive and (chat_dir(cid) / "runner.sock").exists() and tok_file.exists():
                rt = Runtime(cid, row["user_id"], row["is_admin"], tok_file.read_text().strip())
                rt.pane_pid = await tmux_pane_pid(name)
                self.rt[cid] = rt
                try:
                    await self._connect(rt)
                    continue
                except Exception as e:
                    log.warning("не удалось подключиться к раннеру %s: %s", cid, e)
                    self.rt.pop(cid, None)
                    await tmux("kill-session", "-t", f"={name}")
            await self._mark_stopped(cid, row, "Процесс чата не найден после перезапуска сервера — сессия выгружена, контекст сохранён.",
                                     kind="crash")
        # убрать «осиротевшие» tmux-сессии, которых нет в БД
        known = {tmux_name(r["id"]) for r in await db.fetch("SELECT id FROM chats")}
        for name in alive:
            if name.startswith(config.TMUX_PREFIX) and name not in known:
                await tmux("kill-session", "-t", f"={name}")
        self.memory_task = asyncio.create_task(self._memory_loop())

    async def shutdown(self):
        if self.memory_task:
            self.memory_task.cancel()
        for rt in list(self.rt.values()):
            if rt.reader:
                rt.reader.cancel()
            for a in rt.approvals.values():
                if not a.future.done():
                    a.future.set_result(("deny", "Веб-сервер перезапускается"))

    # ---------- проверки перед запуском
    async def estimate_mb(self) -> int:
        s = await db.get_settings()
        rss = [rt_rss(rt) for rt in self.rt.values()]
        rss = [r for r in rss if r > 50 * MB]
        avg = (sum(rss) / len(rss) / MB) if rss else 0
        return int(max(s["session_estimate_mb"], avg))

    async def check_can_start(self, user):
        s = await db.get_settings()
        mine = [rt for rt in self.rt.values() if rt.user_id == user["id"]]
        if len(mine) >= user["max_sessions"]:
            raise StartError("session_limit",
                             f"Достигнут лимит активных сессий: {user['max_sessions']}. Выгрузите один из чатов.",
                             suggest=await self._suggest(mine))
        need = await self.estimate_mb()
        avail = psutil.virtual_memory().available // MB
        if avail - need < s["ram_reserve_mb"]:
            raise StartError("no_memory",
                             f"Недостаточно памяти: свободно {avail} МБ, сессии нужно ~{need} МБ, "
                             f"а системе нужно оставить {s['ram_reserve_mb']} МБ.",
                             available_mb=avail, needed_mb=need, reserve_mb=s["ram_reserve_mb"],
                             suggest=await self._suggest(mine))

    async def _suggest(self, rts) -> list[dict]:
        if not rts:
            return []
        ids = [uuid.UUID(rt.chat_id) for rt in rts if not rt.busy]
        rows = await db.fetch("SELECT id, title FROM chats WHERE id = ANY($1) ORDER BY last_activity", ids)
        return [{"id": str(r["id"]), "title": r["title"]} for r in rows]

    # ---------- запуск / возобновление
    async def start(self, chat, user):
        cid = str(chat["id"])
        async with self.lock(cid):
            if cid in self.rt:
                return
            async with self.start_lock:
                await self.check_can_start(user)
                await self._spawn(chat, user)
        await self.push_chat(cid)

    async def _spawn(self, chat, user):
        cid = str(chat["id"])
        cd = chat_dir(cid)
        cd.mkdir(parents=True, exist_ok=True, mode=0o700)
        cwd = chat_cwd(chat)
        if chat["project"]:
            if not cwd.is_dir() or config.WORKSPACE not in cwd.parents:
                raise StartError("bad_project", f"Проект {chat['project']} не найден в workspace")
        else:
            cwd.mkdir(parents=True, exist_ok=True)

        decompress_events(cid)
        restore_claude_state(cid)

        sid = str(chat["claude_session_id"])
        resume = bool(chat["has_transcript"])
        if resume and not any(p.suffix == ".jsonl" for p in _claude_state_paths(sid)):
            # транскрипт пропал — начинаем новую сессию, предупреждаем
            sid = str(uuid.uuid4())
            resume = False
            await db.execute("UPDATE chats SET claude_session_id = $2, has_transcript = false WHERE id = $1",
                             chat["id"], uuid.UUID(sid))
            append_offline_event(cid, "sys", {"event": "notice", "level": "warn",
                                              "text": "Контекст прошлого разговора не найден — Claude начинает с чистого листа."})

        token = secrets.token_urlsafe(32)
        tok_file = cd / ".hook_token"
        tok_file.write_text(token)
        tok_file.chmod(0o600)

        settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"{shlex.quote(config.PYTHON)} {shlex.quote(str(config.HOOK))}", "timeout": 900}]}]}}
        (cd / "settings.json").write_text(json.dumps(settings))

        argv = [config.CLAUDE_BIN, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--settings", str(cd / "settings.json"),
                "--permission-prompts", "none"]
        argv += ["--resume", sid] if resume else ["--session-id", sid]
        if chat["model"]:
            argv += ["--model", chat["model"]]
        if user["is_admin"]:
            sp = (f"Ты общаешься с пользователем «{user['username']}» (администратор) через веб-чат webpanel. "
                  "Интерактивный AskUserQuestion недоступен — задавай вопросы текстом. Опасные действия с ОС и "
                  "изменения вне /root/workspace будут показаны пользователю на подтверждение.")
        else:
            tools = policy.tools_for_rights(user["rights"])
            argv += ["--restricted", "--strict-mcp-config", "--tools", ",".join(tools)]
            rights = ", ".join(config.RIGHTS[r] for r in user["rights"] if r in config.RIGHTS) or "нет (только диалог)"
            sp = (f"Ты общаешься с пользователем «{user['username']}» через веб-чат webpanel. Его права: {rights}. "
                  f"Рабочая папка: {cwd}. Не пытайся обойти ограничения; если нужно действие без прав — объясни, "
                  "что его может разрешить администратор.")
        argv += ["--append-system-prompt", sp]

        cfg = {"argv": argv, "cwd": str(cwd), "resume": resume, "env": {
            "WEBPANEL_HOOK_URL": config.HOOK_URL, "WEBPANEL_HOOK_TOKEN": token, "WEBPANEL_CHAT_ID": cid}}
        (cd / "config.json").write_text(json.dumps(cfg))
        (cd / "config.json").chmod(0o600)
        (cd / "runner.sock").unlink(missing_ok=True)

        name = tmux_name(cid)
        await tmux("kill-session", "-t", f"={name}")
        cmd = f"{shlex.quote(config.PYTHON)} {shlex.quote(str(config.RUNNER))} {shlex.quote(str(cd))}"
        await tmux("new-session", "-d", "-s", name, "-x", "160", "-y", "45", cmd, check=True)
        await tmux("set-option", "-t", f"={name}", "history-limit", "5000")

        rt = Runtime(cid, user["id"], user["is_admin"], token)
        rt.pane_pid = await tmux_pane_pid(name)
        self.rt[cid] = rt
        for _ in range(100):  # ждём сокет раннера до 10 c
            if (cd / "runner.sock").exists():
                break
            if name not in await tmux_sessions():
                break
            await asyncio.sleep(0.1)
        try:
            await self._connect(rt)
        except Exception as e:
            self.rt.pop(cid, None)
            await tmux("kill-session", "-t", f"={name}")
            raise StartError("start_failed", f"Не удалось запустить сессию: {e}")
        await db.execute("UPDATE chats SET state = 'running', notice = NULL, last_activity = now() WHERE id = $1", chat["id"])

    async def _connect(self, rt: Runtime):
        reader, writer = await asyncio.open_unix_connection(str(chat_dir(rt.chat_id) / "runner.sock"),
                                                            limit=64 * 1024 * 1024)
        rt.writer = writer
        rt.seq = last_seq(rt.chat_id)
        rt.reader = asyncio.create_task(self._read_loop(rt, reader))

    async def _read_loop(self, rt: Runtime, reader: asyncio.StreamReader):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if "hello" in msg:
                    rt.busy = msg["hello"].get("busy", False)
                    rt.claude_pid = msg["hello"].get("pid")
                elif "ev" in msg:
                    ev = msg["ev"]
                    if ev["seq"] <= rt.seq:
                        continue
                    rt.seq = ev["seq"]
                    await self._on_event(rt, ev)
                    hub.to_chat(rt.user_id, rt.chat_id, {"t": "events", "chat_id": rt.chat_id, "events": [ev]})
                elif "live" in msg:
                    hub.to_chat(rt.user_id, rt.chat_id, {"t": "live", "chat_id": rt.chat_id, "d": msg["live"]})
                elif "state" in msg:
                    rt.busy = msg["state"]["busy"]
                    rt.last_activity = time.time()
                    await self.push_chat(rt.chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("read loop %s: %s", rt.chat_id, e)
        await self._on_disconnect(rt)

    async def _on_event(self, rt: Runtime, ev: dict):
        d = ev.get("d") or {}
        if ev["k"] == "claude" and d.get("type") == "system" and d.get("subtype") == "init" and d.get("session_id"):
            await db.execute(
            "UPDATE chats SET past_session_ids = array_append(past_session_ids, claude_session_id), claude_session_id = $2 "
            "WHERE id = $1 AND claude_session_id <> $2", uuid.UUID(rt.chat_id), uuid.UUID(d["session_id"]))
        elif ev["k"] == "sys" and d.get("event") == "started":
            rt.claude_pid = d.get("pid")
        elif ev["k"] == "sys" and d.get("event") == "exited":
            rt.exit_info = d

    async def _on_disconnect(self, rt: Runtime):
        """Раннер отключился: либо мы его остановили, либо он упал."""
        cid = rt.chat_id
        if self.rt.get(cid) is not rt:
            return
        if rt.stopping:
            return  # остановку обрабатывает тот, кто её начал
        await asyncio.sleep(0.5)
        if rt.stopping or self.rt.get(cid) is not rt:
            return
        info = rt.exit_info or {}
        code = info.get("code")
        detail = (info.get("stderr") or info.get("error") or "").strip()
        msg = "Процесс Claude неожиданно завершился" + (f" (код {code})" if code is not None else "") + \
              ". Сессия выгружена, контекст сохранён — можно продолжить."
        await self._finalize_stop(rt, msg, kind="crash", details={"code": code, "stderr": detail[-800:]})

    # ---------- остановка
    async def _finalize_stop(self, rt: Runtime, notice: str, kind: str, details=None, incident_for_user=True):
        cid = rt.chat_id
        kill_tree(rt.pane_pid)
        kill_pid(rt.claude_pid)
        await tmux("kill-session", "-t", f"={tmux_name(cid)}")
        for a in rt.approvals.values():
            if not a.future.done():
                a.future.set_result(("deny", "Сессия остановлена"))
        self.rt.pop(cid, None)
        if rt.writer:
            try:
                rt.writer.close()
            except Exception:
                pass
        row = await db.fetchrow("SELECT * FROM chats WHERE id = $1", uuid.UUID(cid))
        if not row:
            return
        await self._mark_stopped(cid, row, notice, kind, details, incident_for_user, seq_hint=rt.seq)

    async def _mark_stopped(self, cid, row, notice, kind, details=None, incident_for_user=True, seq_hint=None):
        size = 0
        try:
            (chat_dir(cid) / "runner.sock").unlink(missing_ok=True)
            compress_events(cid)
            size = archive_claude_state(cid, chat_session_ids(row))
        except Exception as e:
            log.exception("архивация %s: %s", cid, e)
        ev = append_offline_event(cid, "sys", {"event": "notice", "kind": kind,
                                               "level": "info" if kind == "manual" else "warn", "text": notice},
                                  seq_hint=seq_hint)
        hub.to_chat(row["user_id"], cid, {"t": "events", "chat_id": cid, "events": [ev]})
        await db.execute("UPDATE chats SET state = 'suspended', notice = $2 WHERE id = $1", row["id"], notice)
        if kind != "manual":
            inc = await db.add_incident(kind, notice, row["user_id"] if incident_for_user else None, row["id"], row["title"],
                                        {**(details or {}), "archive_kb": size // 1024})
            hub.to_user(row["user_id"], {"t": "incident", "incident": incident_public(inc)})
            hub.to_admins({"t": "incident", "incident": incident_public(inc)})
        await self.push_chat(cid)

    async def suspend(self, chat_id, notice="Сессия выгружена из RAM на диск. Контекст сохранён.", kind="manual", details=None):
        cid = str(chat_id)
        async with self.lock(cid):
            rt = self.rt.get(cid)
            if not rt:
                return
            rt.stopping = kind
            if rt.writer:
                try:
                    rt.writer.write(b'{"op":"shutdown"}\n')
                    await rt.writer.drain()
                except Exception:
                    pass
            name = tmux_name(cid)
            for _ in range(150):  # до 15 c на корректное завершение
                if name not in await tmux_sessions():
                    break
                await asyncio.sleep(0.1)
            if rt.reader:
                rt.reader.cancel()
            await self._finalize_stop(rt, notice, kind, details)

    async def emergency_kill(self, chat_id, notice, kind="emergency", details=None):
        """Немедленное завершение (SIGKILL) — когда серверу грозит зависание или по решению админа."""
        cid = str(chat_id)
        rt = self.rt.get(cid)
        if not rt:
            return
        rt.stopping = kind
        if rt.reader:
            rt.reader.cancel()
        kill_tree(rt.pane_pid)
        kill_pid(rt.claude_pid)
        await self._finalize_stop(rt, notice, kind, details)

    async def delete(self, chat_id, *, by_admin=None, reason=None):
        cid = str(chat_id)
        async with self.lock(cid):
            row = await db.fetchrow("SELECT * FROM chats WHERE id = $1", uuid.UUID(cid))
            if not row:
                return
            rt = self.rt.pop(cid, None)
            if rt:
                rt.stopping = "delete"
                if rt.reader:
                    rt.reader.cancel()
                for a in rt.approvals.values():
                    if not a.future.done():
                        a.future.set_result(("deny", "Диалог удалён"))
            if rt:
                kill_tree(rt.pane_pid)
                kill_pid(rt.claude_pid)
            await tmux("kill-session", "-t", f"={tmux_name(cid)}")
            wipe_claude_state(chat_session_ids(row), cid)
            shutil.rmtree(chat_dir(cid), ignore_errors=True)
            await db.execute("DELETE FROM incidents WHERE chat_id = $1", row["id"])
            await db.execute("DELETE FROM chats WHERE id = $1", row["id"])
            if by_admin:
                msg = f"Администратор удалил диалог «{row['title']}»" + (f": {reason}" if reason else "")
                inc = await db.add_incident("admin_delete", msg, row["user_id"], None, row["title"], {"by": by_admin})
                hub.to_user(row["user_id"], {"t": "incident", "incident": incident_public(inc)})
        self.locks.pop(cid, None)
        hub.to_user(row["user_id"], {"t": "chat_deleted", "id": cid})
        hub.to_admins({"t": "sessions_changed"})

    # ---------- сообщения
    async def send(self, chat, user, text: str):
        cid = str(chat["id"])
        if cid not in self.rt:
            await self.start(chat, user)
        rt = self.rt.get(cid)
        if not rt or not rt.writer:
            raise StartError("not_running", "Сессия не запущена")
        if rt.busy:
            raise StartError("busy", "Claude ещё отвечает. Дождитесь окончания или остановите ответ.")
        if not chat["has_transcript"] or chat["title"] == "Новый диалог":
            first = next((l.strip() for l in text.splitlines() if l.strip()), "Новый диалог")
            title = first[:80] + ("…" if len(first) > 80 else "")
            await db.execute("UPDATE chats SET title = CASE WHEN has_transcript THEN title ELSE $2 END, "
                             "has_transcript = true WHERE id = $1", chat["id"], title)
        await db.execute("UPDATE chats SET last_activity = now() WHERE id = $1", chat["id"])
        rt.busy = True
        rt.last_activity = time.time()
        rt.writer.write((json.dumps({"op": "send", "text": text}, ensure_ascii=False) + "\n").encode())
        await rt.writer.drain()
        await self.push_chat(cid)

    async def interrupt(self, chat_id):
        rt = self.rt.get(str(chat_id))
        if rt and rt.writer:
            for a in rt.approvals.values():
                if not a.future.done():
                    a.future.set_result(("deny", "Пользователь остановил ответ"))
            rt.writer.write(b'{"op":"interrupt"}\n')
            await rt.writer.drain()

    async def note(self, rt: Runtime, d: dict):
        if rt.writer:
            rt.writer.write((json.dumps({"op": "note", "d": d}, ensure_ascii=False) + "\n").encode())
            await rt.writer.drain()

    # ---------- подтверждения (из хука)
    async def hook_decision(self, chat_id: str, token: str, tool: str, inp: dict) -> dict:
        rt = self.rt.get(chat_id)
        if not rt or not secrets.compare_digest(rt.hook_token, token or ""):
            return {"decision": "deny", "reason": "webpanel: неизвестная сессия"}
        chat = await db.fetchrow("SELECT * FROM chats WHERE id = $1", uuid.UUID(chat_id))
        user = await db.fetchrow("SELECT * FROM users WHERE id = $1", rt.user_id)
        if not chat or not user:
            return {"decision": "deny", "reason": "webpanel: чат не найден"}
        decision, reason = policy.decide(user, chat_cwd(chat), tool or "", inp or {})
        rt.last_activity = time.time()
        if decision != "ask":
            return {"decision": decision, "reason": reason}

        s = await db.get_settings()
        a = Approval(secrets.token_hex(8), chat_id, tool, inp if isinstance(inp, dict) else {}, reason, time.time(),
                     asyncio.get_running_loop().create_future())
        rt.approvals[a.id] = a
        await self.note(rt, {"event": "approval_request", **a.public()})
        await self.push_chat(chat_id)
        try:
            decision, why = await asyncio.wait_for(a.future, s["approval_timeout_s"])
        except asyncio.TimeoutError:
            decision, why = "deny", "Время на подтверждение истекло"
        rt.approvals.pop(a.id, None)
        if self.rt.get(chat_id) is rt:
            await self.note(rt, {"event": "approval_result", "id": a.id, "decision": decision, "reason": why})
            await self.push_chat(chat_id)
        return {"decision": decision, "reason": why or ("Разрешено пользователем" if decision == "allow" else "Запрещено")}

    def resolve_approval(self, chat_id: str, approval_id: str, allow: bool) -> bool:
        rt = self.rt.get(chat_id)
        a = rt.approvals.get(approval_id) if rt else None
        if not a or a.future.done():
            return False
        a.future.set_result(("allow", "Разрешено пользователем") if allow else ("deny", "Пользователь запретил это действие"))
        return True

    # ---------- защита памяти
    async def _memory_loop(self):
        from . import resources
        while True:
            try:
                resources.sample()
                await self._guard()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("memory guard")
            await asyncio.sleep(3)

    async def _guard(self):
        if not self.rt or time.time() - self.last_guard_action < 6:
            return
        s = await db.get_settings()
        avail = psutil.virtual_memory().available // MB
        if avail >= s["ram_reserve_mb"]:
            return
        now = time.time()
        idle = [rt for rt in self.rt.values() if not rt.busy and not rt.approvals and now - rt.started > 20]
        idle.sort(key=lambda r: (r.is_admin, r.last_activity))
        details = {"available_mb": avail, "reserve_mb": s["ram_reserve_mb"]}
        if idle:
            victim = idle[0]
            details["rss_mb"] = rt_rss(victim) // MB
            self.last_guard_action = now
            log.warning("память: свободно %s МБ < %s, выгружаю %s", avail, s["ram_reserve_mb"], victim.chat_id)
            await self.suspend(victim.chat_id, kind="auto_suspend", details=details, notice=(
                f"Сессия автоматически выгружена из RAM на диск: свободной памяти осталось {avail} МБ "
                f"(минимум для системы {s['ram_reserve_mb']} МБ). Контекст сохранён и сжат — отправьте сообщение, чтобы продолжить."))
            return
        if avail < s["ram_critical_mb"]:
            busy = [rt for rt in self.rt.values() if not rt.is_admin]
            busy.sort(key=lambda r: rt_rss(r), reverse=True)
            if busy:
                victim = busy[0]
                details["rss_mb"] = rt_rss(victim) // MB
                self.last_guard_action = now
                log.error("КРИТИЧНО: свободно %s МБ, экстренно завершаю %s", avail, victim.chat_id)
                await self.emergency_kill(victim.chat_id, kind="emergency", details=details, notice=(
                    f"Сессия ЭКСТРЕННО завершена, чтобы сервер не завис: свободной памяти осталось {avail} МБ "
                    f"(критический порог {s['ram_critical_mb']} МБ), процесс занимал ~{details['rss_mb']} МБ. "
                    "Текущий ответ прерван, история и контекст сохранены — можно продолжить."))


def incident_public(r) -> dict:
    return {"id": r["id"], "ts": r["ts"].isoformat(), "kind": r["kind"], "chat_id": str(r["chat_id"]) if r["chat_id"] else None,
            "chat_title": r["chat_title"], "message": r["message"], "details": r["details"], "seen": r["seen"],
            "user_id": r["user_id"]}


manager = ChatManager()
