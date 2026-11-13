"""PDF preflight scanner for an incoming attachment directory.

The scanner walks an input directory recursively, inspects every ``*.pdf``
file (case-insensitive extension), detects three independent risk features and
writes a deterministic JSON report with per-file risk levels and handling
advice.

Features
--------
javascript
    A PDF JavaScript name tree entry (``/JavaScript``/``/JS``) or a JavaScript
    action (``/S /JavaScript``) is present.
auto_action
    A document-level ``/OpenAction`` or an additional-action dictionary
    (``/AA``) is present.
embedded_executable
    A file specification (``/F``, ``/UF``, ...) names an embedded file whose
    name ends with ``.exe`` or ``.dll`` (case-insensitive).

Risk levels are derived only from the number of *distinct* feature kinds:
0 -> low, 1 -> medium, 2+ -> high.  Files that cannot be parsed (damaged,
encrypted, not a PDF) are ``unknown`` with an empty feature list and an
explicit reason; they are never treated as clean.

Only the Python standard library is used.  The scanner never executes or
extracts embedded content: it only reads bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

__version__ = "1.0.0"
TOOL_NAME = "pdf-preflight"

FEATURE_JAVASCRIPT = "javascript"
FEATURE_AUTO_ACTION = "auto_action"
FEATURE_EMBEDDED_EXECUTABLE = "embedded_executable"

# Stable order in every report, independent of where the bytes were found.
FEATURE_ORDER: tuple[str, ...] = (
    FEATURE_JAVASCRIPT,
    FEATURE_AUTO_ACTION,
    FEATURE_EMBEDDED_EXECUTABLE,
)

LEVEL_LOW = "low"
LEVEL_MEDIUM = "medium"
LEVEL_HIGH = "high"
LEVEL_UNKNOWN = "unknown"

LEVEL_ORDER = {LEVEL_HIGH: 0, LEVEL_MEDIUM: 1, LEVEL_LOW: 2, LEVEL_UNKNOWN: 3}

ADVICE = {
    LEVEL_HIGH: "建议隔离，待人工确认后再处理",
    LEVEL_MEDIUM: "建议人工复核后放行或隔离",
    LEVEL_LOW: "未发现风险特征，可放行",
    LEVEL_UNKNOWN: "无法解析，请人工检查后再决定",
}

ACTION = {
    LEVEL_HIGH: "quarantine",
    LEVEL_MEDIUM: "review",
    LEVEL_LOW: "allow",
    LEVEL_UNKNOWN: "manual_review",
}

# A PDF name ends at whitespace, a delimiter or end of file.
_NAME_END = rb"(?=[\s/<>\[\]\(\){}%]|$)"

# /JavaScript as a name-tree key, /JS as the value key, or /S /JavaScript.
_JS_RE = re.compile(rb"/(?:JavaScript|JS)" + _NAME_END, re.IGNORECASE)
_JS_ACTION_RE = re.compile(rb"/S\s*/JavaScript" + _NAME_END, re.IGNORECASE)
_AUTO_RE = re.compile(
    rb"/OpenAction" + _NAME_END + rb"|/AA\s*(?:<<|\d+\s+\d+\s+R)",
    re.IGNORECASE,
)

# File specification keys that can carry the embedded file's name.  The key
# must be followed by a literal string, a hex string or whitespace.
_FILESPEC_KEY_RE = re.compile(
    rb"/(?:F|UF|DOS|Mac|Unix)(?=[\s(<])", re.IGNORECASE
)
_HEX_ONLY_RE = re.compile(rb"^[0-9A-Fa-f\s]*$")
_EXECUTABLE_SUFFIX_RE = re.compile(r"\.(?:exe|dll)$", re.IGNORECASE)
_NAME_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")
_NAMES_ARRAY_RE = re.compile(rb"/Names\s*\[")

# Object streams (PDF 1.5+) hold compressed objects; cap decoded output.
_OBJSTM_RE = re.compile(rb"/Type\s*/ObjStm", re.IGNORECASE)
_MAX_DECOMPRESSED = 8 * 1024 * 1024
_MAX_XREF_STREAM_DICT = 64 * 1024
_MAX_FILE_BYTES = 200 * 1024 * 1024

_ERROR_MESSAGES = {
    "empty": "文件为空",
    "not_pdf": "未找到 PDF 文件头",
    "truncated": "文件缺少 %%EOF 结束标记",
    "broken_xref": "交叉引用表或 startxref 偏移损坏",
    "no_trailer": "缺少 trailer",
    "no_root": "trailer 中缺少 /Root 目录对象",
    "encrypted": "文件已加密，未解析内容",
    "unreadable": "文件无法读取",
    "too_large": "文件超过扫描大小上限，未解析",
}


@dataclass(frozen=True)
class ParseResult:
    """Outcome of structural validation; encrypted files are also unparsed."""

    ok: bool
    reason: str | None = None
    encrypted: bool = False


def _skip_ws(data: bytes, pos: int) -> int:
    while pos < len(data) and data[pos] in b"\x00\t\n\f\r ":
        pos += 1
    return pos


def _read_literal_string(data: bytes, pos: int) -> tuple[bytes, int] | None:
    """Read a PDF literal string starting at ``pos`` (which must be '(')."""

    if pos >= len(data) or data[pos] != 0x28:  # '('
        return None
    depth = 1
    pos += 1
    out = bytearray()
    while pos < len(data):
        byte = data[pos]
        if byte == 0x5C:  # backslash
            pos += 1
            if pos >= len(data):
                return None
            escaped = data[pos]
            escapes = {
                ord("n"): b"\n",
                ord("r"): b"\r",
                ord("t"): b"\t",
                ord("b"): b"\b",
                ord("f"): b"\f",
                ord("("): b"(",
                ord(")"): b")",
                ord("\\"): b"\\",
            }
            if escaped in escapes:
                out += escapes[escaped]
                pos += 1
            elif 0x30 <= escaped <= 0x37:  # octal, up to 3 digits
                digits = 0
                value = 0
                while (
                    digits < 3
                    and pos < len(data)
                    and 0x30 <= data[pos] <= 0x37
                ):
                    value = value * 8 + (data[pos] - 0x30)
                    pos += 1
                    digits += 1
                out.append(value & 0xFF)
            elif escaped in (0x0A, 0x0D):  # line continuation
                if (
                    escaped == 0x0D
                    and pos + 1 < len(data)
                    and data[pos + 1] == 0x0A
                ):
                    pos += 2
                else:
                    pos += 1
            else:
                out.append(escaped)
                pos += 1
            continue
        if byte == 0x28:
            depth += 1
            out.append(byte)
            pos += 1
            continue
        if byte == 0x29:
            depth -= 1
            pos += 1
            if depth == 0:
                return bytes(out), pos
            out.append(byte)
            continue
        out.append(byte)
        pos += 1
    return None


def _read_hex_string(data: bytes, pos: int) -> tuple[bytes, int] | None:
    if data[pos : pos + 2] == b"<<":
        return None  # dictionary, not a hex string
    end = data.find(b">", pos + 1)
    if end == -1:
        return None
    body = data[pos + 1 : end]
    if not _HEX_ONLY_RE.match(body):
        return None
    digits = re.sub(rb"\s+", b"", body)
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii")), end + 1
    except ValueError:
        return None


def _decode_pdf_text(raw: bytes) -> str:
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    return raw.decode("latin-1")


def _read_name_value(data: bytes, pos: int) -> tuple[str, int] | None:
    """Read a PDF string token (literal or hex) at ``pos``.

    A bare PDF name (``/A.EXE``) is also accepted, since a file
    specification may store its name that way.
    """

    pos = _skip_ws(data, pos)
    if pos >= len(data):
        return None
    if data[pos] == 0x28:
        parsed = _read_literal_string(data, pos)
    elif data[pos] == 0x3C:
        parsed = _read_hex_string(data, pos)
    elif data[pos] == 0x2F:  # '/'
        match = re.match(rb"/([^\s/<>\[\](){}%]*)" + _NAME_END, data[pos:])
        if match is None:
            return None
        raw_name = _NAME_ESCAPE_RE.sub(
            lambda m: bytes([int(m.group(1), 16)]), match.group(1)
        )
        return _decode_pdf_text(raw_name), pos + match.end()
    else:
        return None
    if parsed is None:
        return None
    return _decode_pdf_text(parsed[0]), parsed[1]


def _looks_like_filespec(body: bytes) -> bool:
    return (
        re.search(rb"/Type\s*/Filespec", body, re.IGNORECASE) is not None
        or re.search(rb"/EF\s*<<", body, re.IGNORECASE) is not None
    )


def _iter_filespec_bodies(data: bytes) -> Iterable[bytes]:
    """Yield dictionary bodies that are file specifications.

    Covers ordinary indirect objects and, because decoded object-stream
    payloads have no ``obj``/``endobj`` wrappers, also slices that start at a
    ``/Type /Filespec`` or ``/EF`` marker.
    """

    for match in re.finditer(rb"\d+\s+\d+\s+obj\b", data):
        end = data.find(b"endobj", match.end())
        body = data[match.end() : end if end != -1 else match.end() + 8192]
        if _looks_like_filespec(body):
            yield body

    for match in re.finditer(rb"/Type\s*/Filespec|/EF\s*<<", data, re.IGNORECASE):
        end = data.find(b"endobj", match.end())
        if end == -1:
            end = min(len(data), match.end() + 4096)
        yield data[match.start() : end]


def _iter_embedded_name_tree_strings(data: bytes) -> Iterable[str]:
    """Yield attachment names listed in an ``/EmbeddedFiles`` name tree.

    Name trees alternate name/value pairs inside ``/Names [...]``; only the
    first such array after ``/EmbeddedFiles`` belongs to that tree, so later
    name trees (for instance JavaScript destinations) are not consulted.
    """

    for tree in re.finditer(rb"/EmbeddedFiles", data, re.IGNORECASE):
        window = data[tree.end() : tree.end() + 16384]
        names = _NAMES_ARRAY_RE.search(window)
        if names is None:
            continue
        pos = names.end()
        depth = 1
        while pos < len(window) and depth > 0:
            byte = window[pos]
            if byte == 0x5B:  # '['
                depth += 1
            elif byte == 0x5D:  # ']'
                depth -= 1
                pos += 1
                continue
            elif byte in (0x28, 0x3C, 0x2F):
                parsed = _read_name_value(window, pos)
                if parsed is not None:
                    yield parsed[0]
                    pos = parsed[1]
                    continue
            pos += 1


def _iter_filespec_strings(data: bytes) -> Iterable[str]:
    """Yield filename values of file-specification dictionaries.

    Only dictionaries declaring ``/Type /Filespec`` or carrying an ``/EF``
    embedded-file dictionary are read, so unrelated keys such as a font
    resource named ``/F`` cannot produce false positives.
    """

    for body in _iter_filespec_bodies(data):
        if not _looks_like_filespec(body):
            continue
        for match in _FILESPEC_KEY_RE.finditer(body):
            parsed = _read_name_value(body, match.end())
            if parsed is not None:
                yield parsed[0]


def _iter_object_stream_contents(data: bytes) -> Iterable[bytes]:
    """Yield decompressed payloads of PDF 1.5 object streams (/ObjStm).

    Objects inside an object stream are compressed and therefore invisible to
    a plain byte scan; decoding them lets the feature scan see name trees and
    action dictionaries stored there.  Only FlateDecode is decoded, and the
    output is bounded to avoid decompression bombs.
    """

    for match in re.finditer(rb"stream\r?\n", data):
        object_start = data.rfind(b"obj", 0, match.start())
        if object_start == -1:
            continue
        dictionary = data[object_start + 3 : match.start()]
        if not _OBJSTM_RE.search(dictionary):
            continue
        if not re.search(
            rb"/Filter\s*(?:/FlateDecode|\[\s*/FlateDecode\s*\])",
            dictionary,
        ):
            continue
        payload_start = match.end()
        payload_end = data.find(b"endstream", payload_start)
        if payload_end == -1:
            continue
        decompressor = zlib.decompressobj()
        try:
            decoded = decompressor.decompress(
                data[payload_start:payload_end],
                _MAX_DECOMPRESSED,
            )
        except zlib.error:
            continue
        if decoded:
            yield decoded


def detect_features(data: bytes) -> list[str]:
    """Return the sorted, de-duplicated feature kinds found in ``data``.

    The scan is a structural byte scan of the PDF syntax (equivalent to the
    well-known ``pdfid`` approach).  It is applied only to files that passed
    structural validation and are not encrypted.  Payloads of object streams
    are decompressed and scanned as well.
    """

    found: set[str] = set()
    regions = [data, *_iter_object_stream_contents(data)]

    for region in regions:
        if _JS_RE.search(region) or _JS_ACTION_RE.search(region):
            found.add(FEATURE_JAVASCRIPT)

        if _AUTO_RE.search(region):
            found.add(FEATURE_AUTO_ACTION)

        if FEATURE_EMBEDDED_EXECUTABLE in found:
            continue
        names = list(_iter_filespec_strings(region))
        names.extend(_iter_embedded_name_tree_strings(region))
        if any(_EXECUTABLE_SUFFIX_RE.search(name.strip()) for name in names):
            found.add(FEATURE_EMBEDDED_EXECUTABLE)

    return [feature for feature in FEATURE_ORDER if feature in found]


def _last_startxref(data: bytes) -> int | None:
    """Find the final startxref offset (it must sit near the end of file)."""

    tail = data[-2048:]
    last: int | None = None
    for match in re.finditer(rb"startxref\s+(\d+)", tail):
        last = int(match.group(1))
    return last


def _validate_classic_xref(data: bytes, offset: int) -> bool:
    """Validate a classic xref table, which may hold several subsections."""

    at = data[offset:]
    cursor = re.match(rb"xref\s*", at)
    if cursor is None:
        return False
    pos = cursor.end()
    in_use = 0
    entries_seen = 0

    while True:
        header = re.match(rb"(\d+)\s+(\d+)\s*", at[pos:])
        if header is None:
            break
        start_number = int(header.group(1))
        count = int(header.group(2))
        if count == 0 and start_number == 0:
            break
        pos += header.end()
        for index in range(count):
            entry = at[pos + index * 20 : pos + (index + 1) * 20]
            entry_match = re.match(rb"(\d{10})\s(\d{5})\s([nf])\s?", entry)
            if entry_match is None:
                return False
            entries_seen += 1
            if entry_match.group(3) == b"n":
                entry_offset = int(entry_match.group(1))
                probe = data[entry_offset : entry_offset + 32]
                if re.match(rb"\s*\d+\s+\d+\s+obj", probe):
                    in_use += 1
        pos += count * 20
        if at[pos : pos + 7] in (b"trailer", b"startxref"):
            break

    return entries_seen > 0 and in_use > 0


def _trailer_dictionaries(data: bytes) -> Iterable[bytes]:
    """Yield the byte range of every trailer dict and xref stream dict."""

    for match in re.finditer(rb"trailer", data):
        start = data.find(b"<<", match.end())
        if start == -1:
            continue
        end = data.find(b"startxref", start)
        yield data[start : end if end != -1 else start + 4096]

    for match in re.finditer(rb"/Type\s*/XRef", data, re.IGNORECASE):
        start = data.rfind(b"<<", 0, match.start())
        if start == -1:
            continue
        end = data.find(b"stream", match.end())
        yield data[start : end if end != -1 else start + _MAX_XREF_STREAM_DICT]


def inspect_structure(data: bytes) -> ParseResult:
    """Validate header, startxref, xref and trailer without reading content."""

    if not data:
        return ParseResult(ok=False, reason="empty")

    header_index = data.find(b"%PDF-", 0, 1024)
    if header_index == -1:
        return ParseResult(ok=False, reason="not_pdf")

    if b"%%EOF" not in data[-2048:]:
        return ParseResult(ok=False, reason="truncated")

    xref_offset = _last_startxref(data)
    if xref_offset is None or xref_offset >= len(data):
        return ParseResult(ok=False, reason="broken_xref")

    at = data[xref_offset:]
    is_classic = at.startswith(b"xref")
    is_stream_xref = False
    if not is_classic:
        obj_match = re.match(rb"\s*\d+\s+\d+\s+obj", at)
        if obj_match is not None:
            stream_pos = at.find(b"stream", obj_match.end())
            header = at[obj_match.end() : stream_pos if stream_pos != -1 else 4096]
            # An xref stream is a dictionary with /Type /XRef, /W widths and a
            # /Root (or /Encrypt) entry; require /W so a forged object cannot
            # be mistaken for a usable cross-reference table.
            is_stream_xref = (
                re.search(rb"/Type\s*/XRef", header, re.IGNORECASE) is not None
                and re.search(rb"/W\s*\[", header) is not None
                and stream_pos != -1
            )
    if not is_classic and not is_stream_xref:
        return ParseResult(ok=False, reason="broken_xref")

    if is_classic:
        if not _validate_classic_xref(data, xref_offset):
            return ParseResult(ok=False, reason="broken_xref")
        trailer_index = data.rfind(b"trailer", xref_offset)
        if trailer_index == -1:
            return ParseResult(ok=False, reason="no_trailer")
        trailer_end = data.find(b"startxref", trailer_index)
        if trailer_end == -1:
            trailer_end = len(data)
        trailer = data[trailer_index:trailer_end]
    else:
        trailer = at[: at.find(b"stream")]
        if len(trailer) > _MAX_XREF_STREAM_DICT:
            return ParseResult(ok=False, reason="broken_xref")

    # /Root and /Encrypt can live in an earlier trailer when incremental
    # updates have appended new revisions, so every trailer dictionary and
    # xref stream dictionary is consulted rather than only the last one.
    trailers = [trailer, *_trailer_dictionaries(data)]
    if not any(re.search(rb"/Root\s+\d+\s+\d+\s+R", item) for item in trailers):
        return ParseResult(ok=False, reason="no_root")

    if any(
        re.search(rb"/Encrypt\s*(?:\d+\s+\d+\s+R|<<)", item)
        for item in trailers
    ):
        return ParseResult(ok=False, reason="encrypted", encrypted=True)

    return ParseResult(ok=True)


def classify(features: Sequence[str]) -> str:
    kinds = len(set(features))
    if kinds == 0:
        return LEVEL_LOW
    if kinds == 1:
        return LEVEL_MEDIUM
    return LEVEL_HIGH


def scan_file(path: Path, relative_path: str) -> dict[str, object]:
    """Inspect one file and return its report entry."""

    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    if size > _MAX_FILE_BYTES:
        return _unparsed_entry(relative_path, "too_large", size)

    try:
        data = path.read_bytes()
    except OSError:
        return _unparsed_entry(relative_path, "unreadable", size)

    result = inspect_structure(data)
    if not result.ok:
        return _unparsed_entry(relative_path, result.reason or "unreadable", size)

    features = detect_features(data)
    level = classify(features)
    return {
        "path": relative_path,
        "level": level,
        "features": features,
        "status": "parsed",
        "action": ACTION[level],
        "advice": ADVICE[level],
        "size": size,
    }


def _unparsed_entry(relative_path: str, reason: str, size: int) -> dict[str, object]:
    return {
        "path": relative_path,
        "level": LEVEL_UNKNOWN,
        "features": [],
        "status": "unparsed",
        "reason": reason,
        "detail": _ERROR_MESSAGES.get(reason, "无法解析"),
        "action": ACTION[LEVEL_UNKNOWN],
        "advice": ADVICE[LEVEL_UNKNOWN],
        "size": size,
    }


def discover_pdfs(input_dir: Path) -> list[tuple[Path, str]]:
    """Return (absolute path, POSIX relative path) for every PDF, recursively."""

    input_dir = Path(os.path.abspath(input_dir))
    discovered: list[tuple[Path, str]] = []
    for root, dirnames, filenames in os.walk(input_dir):
        dirnames.sort()
        for filename in sorted(filenames):
            if not filename.lower().endswith(".pdf"):
                continue
            absolute = Path(root) / filename
            if not absolute.is_file():
                continue
            relative = os.path.relpath(absolute, input_dir).replace(os.sep, "/")
            discovered.append((absolute, relative))
    return discovered


def sort_entries(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda entry: (
            LEVEL_ORDER[str(entry["level"])],
            str(entry["path"]),
        ),
    )


def build_report(input_dir: Path) -> dict[str, object]:
    entries = [
        scan_file(absolute, relative)
        for absolute, relative in discover_pdfs(input_dir)
    ]
    entries = sort_entries(entries)

    summary = {
        "total": len(entries),
        LEVEL_LOW: 0,
        LEVEL_MEDIUM: 0,
        LEVEL_HIGH: 0,
        LEVEL_UNKNOWN: 0,
    }
    for entry in entries:
        summary[str(entry["level"])] += 1

    return {
        "scanner": {"name": TOOL_NAME, "version": __version__},
        "input_dir": str(Path(os.path.abspath(input_dir))),
        "files": entries,
        "summary": summary,
    }


def write_report(report: dict[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output_path)


def _emit(text: str) -> None:
    """Print a status line, tolerating a closed/redirected stdout pipe."""

    try:
        print(text)
    except (BrokenPipeError, OSError):
        pass


def _print_summary(report: dict[str, object], output_path: Path) -> None:
    summary = report["summary"]
    files = report["files"]
    _emit(
        f"扫描完成: 共 {summary['total']} 个 PDF "
        f"(high={summary['high']}, medium={summary['medium']}, "
        f"low={summary['low']}, unknown={summary['unknown']})"
    )
    for entry in files:
        if entry["level"] in (LEVEL_HIGH, LEVEL_UNKNOWN):
            features = ",".join(entry["features"]) or (
                entry.get("reason", "") or "-"
            )
            _emit(f"  [{entry['level']}] {entry['path']} ({features})")
    _emit(f"报告已写入: {output_path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf-preflight",
        description="递归扫描 PDF 附件，识别脚本/自动动作/可执行嵌入并生成 JSON 报告。",
    )
    parser.add_argument("input_dir", help="待扫描的收件目录")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="输出 JSON 报告路径",
    )
    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"错误: 输入目录不存在或不是目录: {input_dir}", file=sys.stderr)
        return 2

    report = build_report(input_dir)
    output_path = Path(args.output)
    try:
        write_report(report, output_path)
    except OSError as exc:
        print(f"错误: 无法写入报告: {exc}", file=sys.stderr)
        return 1

    _print_summary(report, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
