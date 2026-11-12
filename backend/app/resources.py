"""Сбор метрик сервера и короткая история для графиков."""
import os
import time
from collections import deque

import psutil

from . import config

MB = 1024 * 1024
history: deque = deque(maxlen=600)  # 600 точек * 3 c = 30 минут
_last_net = None
psutil.cpu_percent(None)


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
