"""Item pictures and attachments, stored as files under `settings.media_dir`.

The database only holds the relative path. Files go back out only through authenticated endpoints —
never a public static folder — because an attachment can be a supplier contract or a licence scan.

Both kinds are checked by content, not by the name the browser sent: a program renamed to .pdf is
refused, and so is anything that looks like an executable whatever it is called.
"""
import re
import secrets
from pathlib import Path

from app.core.config import settings

PICTURE_MAX_BYTES = 3 * 1024 * 1024
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024

_PICTURES: list[tuple[bytes, int, str, str]] = [
    (b"\xff\xd8\xff", 0, "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, "png", "image/png"),
    (b"WEBP", 8, "webp", "image/webp"),
]
_PICTURE_TYPES = {ext: ctype for _, _, ext, ctype in _PICTURES}

# extension -> (content type, what the file must start with — None means plain text)
_ATTACHMENTS: dict[str, tuple[str, tuple[bytes, ...] | None]] = {
    "pdf": ("application/pdf", (b"%PDF",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "webp": ("image/webp", (b"RIFF",)),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", (b"PK\x03\x04",)),
    "doc": ("application/msword", (b"\xd0\xcf\x11\xe0",)),
    "xls": ("application/vnd.ms-excel", (b"\xd0\xcf\x11\xe0",)),
    "csv": ("text/csv", None),
    "txt": ("text/plain", None),
}
ATTACHMENT_KINDS = "PDF, JPEG, PNG, WebP, GIF, Word, Excel, CSV or text"
_EXECUTABLE_STARTS = (b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")


class MediaError(Exception):
    def __init__(self, message: str):
        self.message = message


def _root() -> Path:
    return Path(settings.media_dir).resolve()


def _safe(part: str) -> str:
    """An Item code can hold any character; a folder name can't."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", part)[:60] or "item"


def _picture_kind(content: bytes) -> str | None:
    for signature, offset, ext, _ in _PICTURES:
        if content[offset:offset + len(signature)] == signature:
            if ext == "webp" and content[:4] != b"RIFF":
                continue
            return ext
    return None


def _write(relative: str, content: bytes) -> None:
    target = _root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def save_picture(folder: str, owner_id: str, content: bytes, replacing: str | None = None) -> str:
    if not content:
        raise MediaError("The picture file is empty.")
    if len(content) > PICTURE_MAX_BYTES:
        raise MediaError(f"That picture is {len(content) / 1024 / 1024:.1f} MB. Use one under 3 MB.")
    ext = _picture_kind(content)
    if not ext:
        raise MediaError("That isn't a JPEG, PNG or WebP picture.")
    relative = f"{folder}/{_safe(owner_id)}-{secrets.token_hex(4)}.{ext}"
    _write(relative, content)
    if replacing:
        remove_file(replacing)
    return relative


def save_attachment(owner_id: str, file_name: str, content: bytes) -> tuple[str, str]:
    """Store an attachment. Returns (relative path, content type)."""
    if not content:
        raise MediaError("The file is empty.")
    if len(content) > ATTACHMENT_MAX_BYTES:
        raise MediaError(f"That file is {len(content) / 1024 / 1024:.1f} MB. Attachments must be under 10 MB.")
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    kind = _ATTACHMENTS.get(ext)
    if not kind:
        raise MediaError(f"{file_name} can't be attached. Use a {ATTACHMENT_KINDS} file.")
    if content.startswith(_EXECUTABLE_STARTS):
        raise MediaError(f"{file_name} is a program, not a document, and can't be attached.")
    content_type, starts = kind
    if starts is None:
        if b"\x00" in content[:8192]:
            raise MediaError(f"{file_name} isn't a plain text file.")
    elif not content.startswith(starts):
        raise MediaError(f"{file_name} isn't really a .{ext} file. Open it and save it again, then attach that.")
    relative = f"attachments/{_safe(owner_id)}/{secrets.token_hex(8)}.{ext}"
    _write(relative, content)
    return relative, content_type


def remove_file(relative: str | None) -> None:
    if not relative:
        return
    path = (_root() / relative).resolve()
    if _root() in path.parents and path.exists():
        path.unlink()


def stored_file(relative: str | None) -> Path | None:
    if not relative:
        return None
    path = (_root() / relative).resolve()
    if _root() not in path.parents or not path.exists():
        return None
    return path


def picture_file(relative: str | None) -> tuple[Path, str] | None:
    path = stored_file(relative)
    if not path:
        return None
    return path, _PICTURE_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
