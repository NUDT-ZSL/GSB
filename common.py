"""Common helpers shared by the vulnerable/fixed upload services and the detector.

Everything here uses the Python standard library only.  All sample payloads are
generated deterministically from fixed byte strings -- no external files and no
third-party packages are needed.
"""

from __future__ import annotations

import json
import os
import re
import uuid

# ---------------------------------------------------------------------------
# Deterministic sample payloads (the same bytes on every run, on every machine)
# ---------------------------------------------------------------------------

# A plain text file uploaded with a traversal-style name.
TRAVERSAL_FILENAME = "../escape.txt"
TRAVERSAL_CONTENT = b"deterministic-traversal-payload: project50\n"

# An "image" whose real content is a PHP-like script, hidden behind a double
# extension.  The vulnerable service only looks at the trailing extension.
DISGUISED_FILENAME = "shell.php.jpg"
DISGUISED_CONTENT = (
    b"<?php echo 'deterministic-pseudo-php-payload project50'; ?>\n"
)

# A real, minimal 1x1 PNG generated once and reused everywhere.
def _build_png_1x1() -> bytes:
    import struct
    import zlib

    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    raw_row = b"\x00\xff\x00\x00"  # filter byte + one RGB pixel (green)
    idat = zlib.compress(raw_row, 9)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


PNG_CONTENT = _build_png_1x1()
PNG_FILENAME = "pixel.png"

# ---------------------------------------------------------------------------
# Multipart helpers (standard library only)
# ---------------------------------------------------------------------------


def build_multipart(
    fields: list[tuple[str, bytes, str]],
    field_name: str = "file",
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` request body.

    ``fields`` is a list of ``(filename, content, content_type)`` tuples.
    Returns ``(body, content_type_header)``.
    """
    if boundary is None:
        # Fixed prefix plus a deterministic uuid-like suffix keeps bodies
        # reproducible while remaining a valid, collision-free boundary.
        boundary = "----project50boundary" + uuid.UUID(int=0x50524F4A3530).hex
    lines: list[bytes] = []
    for filename, content, content_type in fields:
        lines.append(("--" + boundary).encode("ascii"))
        lines.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"'
            ).encode("utf-8")
        )
        lines.append(("Content-Type: " + content_type).encode("ascii"))
        lines.append(b"")
        lines.append(content)
    lines.append(("--" + boundary + "--").encode("ascii"))
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, "multipart/form-data; boundary=" + boundary


def parse_multipart(body: bytes, content_type: str) -> list[dict]:
    """Parse a ``multipart/form-data`` body without any third-party library.

    Returns a list of dicts with keys: ``name``, ``filename``,
    ``content_type`` and ``data``.
    Raises ``ValueError`` on malformed requests.
    """
    if not content_type:
        raise ValueError("missing Content-Type")
    main, _, params = content_type.partition(";")
    if main.strip().lower() != "multipart/form-data":
        raise ValueError("expected multipart/form-data")
    boundary = None
    for param in params.split(";"):
        key, sep, value = param.strip().partition("=")
        if sep and key.lower() == "boundary":
            boundary = value.strip('"')
            break
    if not boundary:
        raise ValueError("missing multipart boundary")

    delim = b"--" + boundary.encode("ascii")
    if delim not in body:
        raise ValueError("boundary not found in body")

    parts: list[dict] = []
    # Split on the boundary; first chunk is preamble (ignored), the last one
    # is the epilogue after the closing "--boundary--".
    chunks = body.split(delim)
    for chunk in chunks[1:]:
        if chunk in (b"--\r\n", b"--", b"--\n"):
            break
        if not chunk.startswith(b"\r\n"):
            # Epilogue / malformed tail.
            continue
        block = chunk[2:]  # strip leading CRLF
        if block.endswith(b"\r\n"):
            block = block[:-2]
        header_blob, sep, data = block.partition(b"\r\n\r\n")
        if not sep:
            raise ValueError("malformed multipart part: no header/body separator")
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n"):
            name, colon, value = line.partition(b":")
            if colon:
                headers[name.decode("ascii", "replace").strip().lower()] = (
                    value.decode("ascii", "replace").strip()
                )
        disposition = headers.get("content-disposition", "")
        name = _disposition_param(disposition, "name")
        filename = _disposition_param(disposition, "filename")
        parts.append(
            {
                "name": name,
                "filename": filename,
                "content_type": headers.get("content-type", "application/octet-stream"),
                "data": data,
            }
        )
    if not parts:
        raise ValueError("multipart body contained no parts")
    return parts


def _disposition_param(disposition: str, param: str) -> str | None:
    match = re.search(
        r'{0}\s*=\s*"([^"]*)"'.format(re.escape(param)), disposition
    )
    if match:
        return match.group(1)
    match = re.search(
        r"{0}\s*=\s*([^;]+)".format(re.escape(param)), disposition
    )
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Response helper
# ---------------------------------------------------------------------------


def json_response(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# Matches Windows drive-letter paths (e.g. C:\..., d:/...) anywhere in text.
DRIVE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]")


def contains_absolute_path(text: str) -> bool:
    """True if ``text`` leaks a server-side absolute path."""
    if DRIVE_PATH_RE.search(text):
        return True
    # POSIX-style absolute paths (two consecutive slashes also count).
    for token in re.split(r"[\s\"'<>]+", text):
        if token.startswith("/") and len(token) > 1:
            return True
    return False


# Extensions that must never be accepted by the fixed service.
EXECUTABLE_EXTENSIONS = {
    "php", "phtml", "php3", "php4", "php5", "phar",
    "exe", "bat", "cmd", "com", "scr", "ps1", "vbs", "js", "jar",
    "sh", "py", "pyc", "dll", "msi", "jsp", "asp", "aspx", "cgi",
}

# Image extensions accepted by the fixed service (allow-list).
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}

# Magic bytes used for lightweight content sniffing.
IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",                 # JPEG
    b"\x89PNG\r\n\x1a\n",            # PNG
    b"GIF87a", b"GIF89a",            # GIF
    b"RIFF",                         # WEBP (prefix checked further below)
)


def looks_like_image_bytes(data: bytes) -> bool:
    for signature in IMAGE_SIGNATURES:
        if data.startswith(signature):
            if signature == b"RIFF":
                return len(data) >= 12 and data[8:12] == b"WEBP"
            return True
    return False


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
