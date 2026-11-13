"""Document discovery, decoding and de-obfuscation views.

Detection never renders Markdown: the *raw* decoded text is inspected, so
HTML comments, front matter and fenced code blocks are visible to the rules
exactly as they appear in the file.
"""

from __future__ import annotations

import codecs
import html
import os
import sys
import unicodedata
from pathlib import Path

MARKDOWN_SUFFIX = ".md"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover_markdown(root: Path) -> list[Path]:
    """Return every regular ``*.md`` file under *root*, recursively.

    * only the *file name* extension is considered and it is compared
      case-insensitively, so ``.md`` and ``.MD`` are scanned while
      ``a.txt``, ``a.md.txt`` and ``a.markdown`` are not;
    * symlinks are never followed - neither a symlinked directory (which
      could escape the tree) nor a symlinked file (which could point at a
      device or a file outside the corpus);
    * the result is sorted by relative path (POSIX separators, code-point
      order), which is what makes ``B.md`` sort before ``a.md``.
    """
    found: list[Path] = []

    def on_error(exc: OSError) -> None:
        # An unreadable sub-directory must not abort the scan; it is reported
        # on stderr, never silently swallowed.
        print(f"warning: cannot list directory: {exc}", file=sys.stderr)

    for current_dir, dir_names, file_names in os.walk(root, onerror=on_error, followlinks=False):
        dir_names.sort()
        for name in file_names:
            if not name.lower().endswith(MARKDOWN_SUFFIX):
                continue
            path = Path(current_dir) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                # stat failed (permissions, race): still report the candidate
                # so the failure is visible instead of silently dropped.
                pass
            found.append(path)

    found.sort(key=lambda p: p.relative_to(root).as_posix())
    return found


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def decode_document(raw: bytes) -> tuple[str, str | None]:
    """Decode file bytes and return ``(text, error)``.

    BOM-sniffed UTF-32/UTF-16 and plain UTF-8 are accepted.  Without a BOM a
    GB18030 attempt follows UTF-8 so that local Chinese Markdown is not
    reported as unreadable (GB18030 is a superset of GBK/GB2312).  Anything
    else yields an error message and empty text.
    """
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        candidates = ("utf-32",)
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates = ("utf-16",)
    elif raw.startswith(codecs.BOM_UTF8):
        candidates = ("utf-8-sig",)
    else:
        candidates = ("utf-8", "gb18030")

    last_error: UnicodeDecodeError | None = None
    for encoding in candidates:
        try:
            return raw.decode(encoding), None
        except UnicodeDecodeError as exc:
            last_error = exc

    assert last_error is not None
    bad = raw[last_error.start : last_error.start + 1].hex() or "??"
    return "", (
        f"cannot decode file as {' or '.join(candidates)}: "
        f"invalid byte 0x{bad} at offset {last_error.start}"
    )


def read_document(path: Path) -> tuple[str, str | None]:
    """Read and decode one file, never raising for filesystem errors."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", "file disappeared before it could be read (deleted or broken link)"
    except PermissionError as exc:
        return "", f"permission denied: {exc}"
    except IsADirectoryError:
        return "", "path is a directory, not a file"
    except OSError as exc:
        return "", f"read failed: {exc}"
    return decode_document(raw)


# --------------------------------------------------------------------------- #
# De-obfuscation views
# --------------------------------------------------------------------------- #
_TAG_BASE = 0xE0000
_TAG_MAX = _TAG_BASE + 0x7F

# Hangul fillers are category Lo yet render blank; U+2800 is a blank braille
# pattern.  Everything else is category Cf and found on first use.
_EXTRA_INVISIBLE = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800})
_invisible_cache: frozenset[int] | None = None


def _invisible_set() -> frozenset[int]:
    """Code points that render invisible but stay in the text stream.

    Unicode ``Cf`` (format) covers zero-width space / non-joiner / joiner,
    word joiner, BOM, soft hyphen, bidi and language controls, Mongolian free
    variation selectors, Arabic letter mark, Unicode tag characters and
    musical symbol controls.  The set is computed once, lazily, because the
    full scan of the code space takes ~0.2 s.
    """
    global _invisible_cache
    if _invisible_cache is None:
        _invisible_cache = frozenset(
            {code for code in range(sys.maxunicode + 1) if unicodedata.category(chr(code)) == "Cf"}
            | _EXTRA_INVISIBLE
        )
    return _invisible_cache


def _is_invisible_char(ch: str) -> bool:
    code = ord(ch)
    if _TAG_BASE <= code <= _TAG_MAX:
        return True
    return code in _invisible_set()


def strip_invisible(text: str) -> str:
    """Remove invisible characters; decode Unicode tag characters to ASCII.

    ``U+E0041``-style tag characters are the standard way to smuggle text
    past a renderer: they are invisible and map onto ASCII by subtracting
    ``0xE0000``.
    """
    if not any(_is_invisible_char(ch) for ch in text):
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if _TAG_BASE <= code <= _TAG_MAX:      # tag char -> ASCII
            if code - _TAG_BASE >= 0x20:
                out.append(chr(code - _TAG_BASE))
            continue
        if _is_invisible_char(ch):
            continue
        out.append(ch)
    return "".join(out)


def deobfuscated_views(text: str) -> list[str]:
    """Return fallback matching views derived from the raw text.

    The raw text is always matched first; these views only exist so a payload
    cannot hide *between* raw characters.  A keyword visible after
    normalisation is a real hit - the downstream consumer sees the normalised
    form, not the bytes.  Views identical to the raw text are dropped so no
    document is scanned more than necessary.
    """
    views: list[str] = []

    # Removing Cf characters / decoding tag characters also reassembles a
    # keyword split as "ev<ZWSP>al".
    stripped = strip_invisible(text)
    if stripped != text:
        views.append(stripped)

    # NFKC folds full-width / compatibility forms ("ｅｖａｌ" -> "eval").
    folded = unicodedata.normalize("NFKC", stripped)
    if folded not in views:
        views.append(folded)

    # HTML entities are never rendered by a plain-text pipeline but are a
    # classic hiding place ("&#101;val" / "&commat;").
    unescaped = html.unescape(stripped)
    if unescaped not in views:
        views.append(unescaped)

    return [view for view in views if view != text]


def matching_views(text: str) -> list[str]:
    """All views a rule is applied to: the raw text first, then normalised ones."""
    return [text, *deobfuscated_views(text)]
