"""VBA source normalisation.

Produces a sequence of normalised statements per module:
- line continuations (`` _`` at end of line) are joined;
- comments (``' ...`` and ``Rem ...``) are stripped;
- each statement is paired with a *masked* view in which string literal
  contents are blanked, so keywords inside strings cannot produce false
  feature matches.
"""

import re

_CONTINUATION_RE = re.compile(r"\s+_\s*$")
_REM_RE = re.compile(r"^\s*Rem\s", re.IGNORECASE)


def _join_continuations(lines):
    logical = []
    pending = ""
    for ln in lines:
        if _CONTINUATION_RE.search(ln):
            pending += _CONTINUATION_RE.sub("", ln) + " "
            continue
        logical.append(pending + ln)
        pending = ""
    if pending:
        logical.append(pending)
    return logical


def _strip_and_mask(line):
    """Remove comments; return (text, masked) with string contents blanked
    in the masked view only."""
    out = []
    masked = []
    i = 0
    in_str = False
    n = len(line)
    while i < n:
        c = line[i]
        if in_str:
            if c == '"':
                if i + 1 < n and line[i + 1] == '"':   # escaped quote ""
                    out.append('""')
                    masked.append("  ")
                    i += 2
                    continue
                in_str = False
                out.append('"')
                masked.append('"')
                i += 1
                continue
            out.append(c)
            masked.append(" ")
            i += 1
        else:
            if c == '"':
                in_str = True
                out.append('"')
                masked.append('"')
                i += 1
            elif c == "'":
                break                                    # comment to EOL
            else:
                out.append(c)
                masked.append(c)
                i += 1
    return "".join(out).rstrip(), "".join(masked).rstrip()


def normalize_source(text: str):
    """Return a list of statements: {"index", "text", "masked"}.

    ``text`` keeps string literals (needed for constant folding); ``masked``
    has literal contents blanked (used for keyword/feature matching).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    statements = []
    for line in _join_continuations(text.split("\n")):
        if _REM_RE.match(line):
            continue
        stripped, masked = _strip_and_mask(line)
        if not stripped.strip():
            continue
        statements.append({
            "index": len(statements) + 1,
            "text": stripped.strip(),
            "masked": masked.strip(),
        })
    return statements
