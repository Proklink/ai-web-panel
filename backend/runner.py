#!/usr/bin/env python3
"""Раннер чата. Запускается внутри tmux-сессии и живёт независимо от веб-сервера.

- держит процесс `claude -p --input-format stream-json --output-format stream-json`;
- пишет все события в <chat_dir>/events.jsonl (источник истины для истории);
- принимает команды от веб-сервера через unix-сокет <chat_dir>/runner.sock;
- печатает читаемый лог в окно tmux (можно посмотреть: tmux -L webpanel attach -t wp-...).

Только стандартная библиотека.
"""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

chat_dir = Path(sys.argv[1]).resolve()
cfg = json.loads((chat_dir / "config.json").read_text())
events_path = chat_dir / "events.jsonl"
sock_path = chat_dir / "runner.sock"

seq = 0
if events_path.exists():
    with events_path.open("rb") as f:
        for line in f:
            try:
                seq = max(seq, json.loads(line).get("seq", 0))
            except Exception:
                pass

busy = False
clients: set[asyncio.StreamWriter] = set()
proc: asyncio.subprocess.Process | None = None
events_file = events_path.open("a", encoding="utf-8")


def log(msg: str):
    print(time.strftime("%H:%M:%S"), msg[:500].replace("\n", " ⏎ "), flush=True)


def broadcast(obj: dict):
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
    for w in list(clients):
        try:
            w.write(data)
        except Exception:
            clients.discard(w)


def persist(kind: str, d) -> dict:
    global seq
    seq += 1
    ev = {"seq": seq, "ts": time.time(), "k": kind, "d": d}
    events_file.write(json.dumps(ev, ensure_ascii=False) + "\n")
    events_file.flush()
    broadcast({"ev": ev})
    return ev


def set_busy(v: bool):
    global busy
    if busy != v:
        busy = v
        broadcast({"state": {"busy": busy}})


def describe(d: dict) -> str:
    t = d.get("type")
    if t == "assistant":
        parts = []
        for b in d.get("message", {}).get("content", []):
            if b.get("type") == "text":
                parts.append(b["text"])
            elif b.get("type") == "tool_use":
                parts.append(f"[{b.get('name')}] {json.dumps(b.get('input'), ensure_ascii=False)[:200]}")
        return "claude: " + " ".join(parts)
    if t == "result":
        return f"--- ход завершён ({d.get('subtype')}, ${d.get('total_cost_usd', 0):.4f})"
    if t == "system":
        return f"system: {d.get('subtype')}"
    return t or "?"


async def read_stdout():
    assert proc and proc.stdout
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        try:
            d = json.loads(line)
        except Exception:
            log("не JSON: " + line.decode(errors="replace"))
            continue
        t = d.get("type")
        if t == "stream_event":
            broadcast({"live": d.get("event")})
            continue
        if t in ("control_response", "control_request", "keep_alive"):
            continue
        persist("claude", d)
        log(describe(d))
        if t == "result":
            set_busy(False)


async def write_stdin(obj: dict):
    assert proc and proc.stdin
    proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
    await proc.stdin.drain()


async def shutdown():
    if proc and proc.returncode is None:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 8)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    clients.add(writer)
    writer.write((json.dumps({"hello": {"busy": busy, "seq": seq, "pid": proc.pid if proc else None}}) + "\n").encode())
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                cmd = json.loads(line)
            except Exception:
                continue
            op = cmd.get("op")
            if op == "send":
                text = str(cmd.get("text", ""))
                persist("user", {"text": text})
                log("user: " + text)
                set_busy(True)
                await write_stdin({"type": "user", "message": {"role": "user", "content": text}})
            elif op == "interrupt":
                log("прерывание")
                await write_stdin({"type": "control_request", "request_id": f"int-{time.time()}",
                                   "request": {"subtype": "interrupt"}})
            elif op == "note":
                persist("sys", cmd.get("d") or {})
            elif op == "shutdown":
                log("остановка по команде сервера")
                await shutdown()
                break
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        clients.discard(writer)
        try:
            writer.close()
        except Exception:
            pass


async def main():
    global proc
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("CLAUDECODE") or k in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k)
    env.update(cfg.get("env", {}))
    stderr_f = (chat_dir / "claude.stderr").open("ab")
    sock_path.unlink(missing_ok=True)
    old_umask = os.umask(0o077)
    server = await asyncio.start_unix_server(handle_client, path=str(sock_path), limit=64 * 1024 * 1024)
    os.umask(old_umask)
    os.chmod(sock_path, 0o600)

    log("запуск: " + " ".join(cfg["argv"][:3]) + " ... cwd=" + cfg["cwd"])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cfg["argv"], cwd=cfg["cwd"], env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=stderr_f, start_new_session=True, limit=64 * 1024 * 1024,
        )
    except Exception as e:
        persist("sys", {"event": "exited", "code": -1, "error": f"Не удалось запустить claude: {e}"})
        return 1
    persist("sys", {"event": "started", "pid": proc.pid, "resume": cfg.get("resume", False)})

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(shutdown()))

    await read_stdout()
    code = await proc.wait()
    set_busy(False)
    tail = ""
    try:
        stderr_f.flush()
        with (chat_dir / "claude.stderr").open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2000))
            tail = f.read().decode(errors="replace")
    except Exception:
        pass
    persist("sys", {"event": "exited", "code": code, "stderr": tail[-1500:]})
    log(f"claude завершился с кодом {code}")
    await asyncio.sleep(0.3)  # дать серверу дочитать сокет
    server.close()
    sock_path.unlink(missing_ok=True)
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
