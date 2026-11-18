"""Сбор метрик сервера и короткая история для графиков."""
import json
import os
import time
from collections import deque

import psutil

from . import config

MB = 1024 * 1024
history: deque = deque(maxlen=600)  # 600 точек * 3 c = 30 минут
_last_net = None
psutil.cpu_percent(None)

# Последние лимиты аккаунта Claude (rate_limit_info.unifiedWindows — те же цифры, что в /usage).
# Аккаунт общий для всех чатов, поэтому храним одно значение и переживаем перезапуск через файл.
_LIMITS_FILE = config.DATA_DIR / "limits.json"
_limits: dict | None = None


def note_limits(info: dict, ts: float):
    global _limits
    if not isinstance(info, dict) or not isinstance(info.get("unifiedWindows"), dict):
        return
    if _limits and _limits["ts"] > ts:
        return
    _limits = {"ts": ts, "status": info.get("status"), "windows": info["unifiedWindows"]}
    try:
        tmp = _LIMITS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_limits))
        tmp.chmod(0o600)
        tmp.replace(_LIMITS_FILE)
    except OSError:
        pass


def get_limits() -> dict | None:
    global _limits
    if _limits is None:
        try:
            _limits = json.loads(_LIMITS_FILE.read_text())
        except (OSError, ValueError):
            _limits = _scan_limits()
    return _limits


def _scan_limits() -> dict | None:
    """Первый запуск: ищем последнее rate_limit_event в недавно изменённых журналах чатов."""
    try:
        files = sorted(config.CHATS_DIR.glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    except OSError:
        return None
    best = None
    for p in files:
        try:
            with open(p, "rb") as f:
                f.seek(max(0, p.stat().st_size - 4 * MB))
                for line in f:
                    if b'"rate_limit_event"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    info = (ev.get("d") or {}).get("rate_limit_info") or {}
                    if isinstance(info.get("unifiedWindows"), dict) and (not best or ev.get("ts", 0) > best["ts"]):
                        best = {"ts": ev.get("ts", 0), "status": info.get("status"), "windows": info["unifiedWindows"]}
        except OSError:
            continue
    return best


def sample():
    global _last_net
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    now = time.time()
    rx = tx = 0.0
    if _last_net:
        dt = max(now - _last_net[0], 0.001)
        rx = (net.bytes_recv - _last_net[1]) / dt
        tx = (net.bytes_sent - _last_net[2]) / dt
    _last_net = (now, net.bytes_recv, net.bytes_sent)
    history.append({
        "ts": now, "cpu": psutil.cpu_percent(None), "mem_avail_mb": vm.available // MB,
        "mem_used_mb": (vm.total - vm.available) // MB, "rx": rx, "tx": tx,
    })


def snapshot(full: bool) -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    du = psutil.disk_usage("/")
    data = {
        "ts": time.time(),
        "uptime_s": time.time() - psutil.boot_time(),
        "load": os.getloadavg(),
        "cpu_count": psutil.cpu_count(),
        "cpu_percent": history[-1]["cpu"] if history else psutil.cpu_percent(None),
        "cpu_per_core": psutil.cpu_percent(None, percpu=True),
        "mem": {"total_mb": vm.total // MB, "available_mb": vm.available // MB,
                "used_mb": (vm.total - vm.available) // MB, "cached_mb": getattr(vm, "cached", 0) // MB},
        "swap": {"total_mb": sw.total // MB, "used_mb": sw.used // MB},
        "disk": {"total_gb": round(du.total / 1024 ** 3, 1), "used_gb": round(du.used / 1024 ** 3, 1),
                 "free_gb": round(du.free / 1024 ** 3, 1), "percent": du.percent},
        "net": {"rx_bps": history[-1]["rx"] if history else 0, "tx_bps": history[-1]["tx"] if history else 0},
        "history": list(history)[-200:],
        "limits": get_limits(),
    }
    if full:
        procs = []
        for p in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent", "cmdline"]):
            try:
                rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
                procs.append({"pid": p.info["pid"], "name": p.info["name"], "user": p.info["username"],
                              "rss_mb": rss // MB, "cpu": p.info["cpu_percent"] or 0,
                              "cmd": " ".join(p.info["cmdline"] or [])[:160]})
            except psutil.Error:
                pass
        procs.sort(key=lambda x: x["rss_mb"], reverse=True)
        data["top_processes"] = procs[:15]
        try:
            from .chats import dir_size
            data["data_dir_mb"] = dir_size(config.DATA_DIR) // MB
        except Exception:
            data["data_dir_mb"] = None
    return data
