"""Генерация конфигурации Caddy и «окно» входа без клиентского сертификата.

Режимы:
  strict — без сертификата устройства TLS-соединение отклоняется (боты не видят даже страницу входа);
  window — временно: сертификат проверяется, если предъявлен, но не обязателен. Веб-сервер в это время
           пускает без сертификата только администратора.

Состояние окна — файл /etc/webpanel/nocert-until (unix-время окончания). Параметры адреса —
/etc/webpanel/caddy.json (пишет ./webpanel.sh install).

Запуск из консоли:  python -m app.caddyconf sync | open МИНУТ | close | status
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ETC = Path(os.environ.get("WEBPANEL_ETC", "/etc/webpanel"))
PARAMS = ETC / "caddy.json"
WINDOW = ETC / "nocert-until"
APPLIED = ETC / "caddy-mode"
CADDYFILE = Path("/etc/caddy/Caddyfile")
MAX_WINDOW_MIN = 240


def window_until() -> float:
    try:
        return float(WINDOW.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def window_open() -> bool:
    return window_until() > time.time()


def set_window(minutes: int) -> float:
    minutes = max(0, min(int(minutes), MAX_WINDOW_MIN))
    until = time.time() + minutes * 60 if minutes else 0
    ETC.mkdir(parents=True, exist_ok=True)
    WINDOW.write_text(f"{until}\n")
    os.chmod(WINDOW, 0o600)
    return until


def render(mode: str) -> str:
    p = json.loads(PARAMS.read_text())
    address = p["address"]
    from .certs import CADDY_CA
    client_auth = (f"\t\tclient_auth {{\n"
                   f"\t\t\tmode {'require_and_verify' if mode == 'strict' else 'verify_if_given'}\n"
                   f"\t\t\ttrusted_ca_cert_file {CADDY_CA}\n"
                   f"\t\t}}\n")
    tls = f"\ttls {'internal ' if p.get('tls_internal') else ''}{{\n{client_auth}\t}}\n"
    glob = ["\tadmin localhost:2019"]
    if p.get("tls_internal"):
        glob.append("\tauto_https disable_redirects")
    if p.get("default_sni"):
        glob.append(f"\tdefault_sni {p['default_sni']}")
        # По IP браузер не шлёт SNI, а при client_auth Caddy требует SNI == Host и отвечает 421.
        # Сайт один, обходить проверку сертификата через другой SNI некуда — проверку можно выключить.
        glob.append("\tservers {\n\t\tstrict_sni_host insecure_off\n\t}")
    return f"""# Сгенерировано webpanel (app/caddyconf.py), режим: {mode}. Ручные правки будут перезаписаны.
{{
{chr(10).join(glob)}
}}

{address} {{
{tls}	encode zstd gzip

	# служебный эндпоинт хука доступен только с localhost напрямую
	@internal path /internal/*
	respond @internal 404

	request_body {{
		max_size 2MB
	}}

	header {{
		Strict-Transport-Security "max-age=31536000"
		-Server
	}}

	reverse_proxy 127.0.0.1:8100 {{
		# отпечаток клиентского сертификата (SHA-256 DER); значение от клиента всегда перезаписывается
		header_up X-Client-Cert-Fp {{http.request.tls.client.fingerprint}}
		header_up X-Client-Cert-Subject {{http.request.tls.client.subject}}
	}}
}}
"""


def applied_mode() -> str | None:
    try:
        return APPLIED.read_text().strip()
    except OSError:
        return None


def apply(mode: str, force: bool = False) -> bool:
    """Записать Caddyfile для режима и перезагрузить Caddy. True, если что-то поменялось."""
    if not PARAMS.exists():
        return False
    from .certs import ensure_ca
    ensure_ca()
    text = render(mode)
    if not force and CADDYFILE.exists() and CADDYFILE.read_text() == text and applied_mode() == mode:
        return False
    tmp = CADDYFILE.with_suffix(".webpanel-tmp")
    tmp.write_text(text)
    chk = subprocess.run(["caddy", "validate", "--config", str(tmp), "--adapter", "caddyfile"],
                         capture_output=True, text=True)
    if chk.returncode:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Caddyfile не прошёл проверку: " + (chk.stderr or chk.stdout)[-500:])
    tmp.replace(CADDYFILE)
    os.chmod(CADDYFILE, 0o644)
    r = subprocess.run(["systemctl", "reload-or-restart", "caddy"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("Не удалось перезагрузить Caddy: " + r.stderr[-300:])
    APPLIED.write_text(mode + "\n")
    return True


def sync(force: bool = False) -> str:
    mode = "window" if window_open() else "strict"
    apply(mode, force)
    return mode


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "open":
        until = set_window(int(argv[2]) if len(argv) > 2 else 30)
        sync()
        print("Вход без сертификата (только для администратора) открыт до", time.strftime("%H:%M:%S", time.localtime(until)))
    elif cmd == "close":
        set_window(0)
        sync()
        print("Окно закрыто: без сертификата устройства вход невозможен")
    elif cmd == "sync":
        print("Режим Caddy:", sync(force="--force" in argv))
    elif cmd == "status":
        u = window_until()
        print("Режим Caddy:", applied_mode() or "?",
              "| окно без сертификата:", ("открыто до " + time.strftime("%H:%M:%S", time.localtime(u))) if u > time.time() else "закрыто")
    else:
        print("использование: python -m app.caddyconf sync|open МИНУТ|close|status")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
