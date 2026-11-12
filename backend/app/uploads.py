"""Вложения чата: фото и документы, загруженные через веб.

Файлы лежат в <chat_dir>/uploads/ и стираются вместе с диалогом. Рядом с каждым файлом — <id>.json с
метаданными. Для Claude:
- картинки (jpeg/png/gif/webp) и PDF уходят прямо в сообщение как content-блоки (видны без прав на файлы);
- небольшие текстовые файлы — как текстовый document-блок;
- остальное (docx, xlsx, архивы…) — только путь на сервере (нужны права на чтение/команды).
"""
import base64
import json
import mimetypes
import re
import secrets
import time
from pathlib import Path

MAX_PER_MESSAGE = 10
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
IMAGE_INLINE_MAX = 5 * 1024 * 1024        # лимит API на картинку
PDF_INLINE_MAX = 20 * 1024 * 1024
TEXT_INLINE_MAX = 256 * 1024
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".log", ".ini", ".toml", ".conf",
            ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".sql", ".html", ".css", ".c", ".h", ".cpp", ".go",
            ".rs", ".java", ".kt", ".rb", ".php", ".env.example"}
ID_RE = re.compile(r"^[a-f0-9]{16}$")


def uploads_dir(chat_dir: Path) -> Path:
    return chat_dir / "uploads"


def safe_name(name: str) -> str:
    name = (name or "file").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".") or "file"
    name = re.sub(r"[^\w.\- ()\[\]+,=@]", "_", name, flags=re.UNICODE)
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:100] + "." + ext[:15]) if dot and len(ext) <= 15 else name[:120]
    return name


def sniff_mime(head: bytes, name: str) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    guess = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if guess in IMAGE_TYPES or guess == "application/pdf":
        return "application/octet-stream"  # расширение врёт — содержимому не соответствует
    return guess


def is_text(meta: dict, path: Path) -> bool:
    ext = Path(meta["name"]).suffix.lower()
    if ext not in TEXT_EXT and not meta["mime"].startswith("text/"):
        return False
    try:
        path.read_bytes()[:TEXT_INLINE_MAX].decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def new_id() -> str:
    return secrets.token_hex(8)


def file_path(chat_dir: Path, meta: dict) -> Path:
    return uploads_dir(chat_dir) / meta["file"]


def write_meta(chat_dir: Path, meta: dict):
    p = uploads_dir(chat_dir) / f"{meta['id']}.json"
    p.write_text(json.dumps(meta, ensure_ascii=False))


def load_meta(chat_dir: Path, fid: str) -> dict | None:
    if not ID_RE.match(fid or ""):
        return None
    p = uploads_dir(chat_dir) / f"{fid}.json"
    try:
        meta = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return meta if file_path(chat_dir, meta).is_file() else None


def public(meta: dict) -> dict:
    return {k: meta[k] for k in ("id", "name", "size", "mime")} | {"image": meta["mime"] in IMAGE_TYPES}


def fmt_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} МБ" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} КБ"


def build_message(chat_dir: Path, text: str, metas: list[dict]) -> tuple[str, list]:
    """Возвращает (текст для истории/старых раннеров, content-блоки для Claude)."""
    lines, blocks = [], []
    for m in metas:
        p = file_path(chat_dir, m)
        how = "путь на сервере"
        if m["mime"] in IMAGE_TYPES and m["size"] <= IMAGE_INLINE_MAX:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": m["mime"],
                                                      "data": base64.b64encode(p.read_bytes()).decode()}})
            how = "приложено к сообщению"
        elif m["mime"] == "application/pdf" and m["size"] <= PDF_INLINE_MAX:
            blocks.append({"type": "document", "title": m["name"], "source": {
                "type": "base64", "media_type": "application/pdf", "data": base64.b64encode(p.read_bytes()).decode()}})
            how = "приложено к сообщению"
        elif m["size"] <= TEXT_INLINE_MAX and is_text(m, p):
            blocks.append({"type": "document", "title": m["name"], "source": {
                "type": "text", "media_type": "text/plain", "data": p.read_text(encoding="utf-8")}})
            how = "приложено к сообщению"
        lines.append(f"- {m['name']} ({m['mime']}, {fmt_size(m['size'])}; {how}): {p}")
    note = "Вложения пользователя (файлы сохранены на сервере):\n" + "\n".join(lines)
    full = (text.strip() + "\n\n" + note) if text.strip() else note
    return full, blocks + [{"type": "text", "text": full}]


def discard(chat_dir: Path, meta: dict):
    file_path(chat_dir, meta).unlink(missing_ok=True)
    (uploads_dir(chat_dir) / f"{meta['id']}.json").unlink(missing_ok=True)


def mark_sent(chat_dir: Path, metas: list[dict]):
    for m in metas:
        m["sent"] = time.time()
        write_meta(chat_dir, m)
