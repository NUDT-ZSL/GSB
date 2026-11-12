"""Behavioural feature extraction from normalised VBA statements.

Features are matched against the *masked* statement view (string literal
contents blanked) so inert keywords inside strings do not trigger them.
String-concatenation obfuscation is detected separately by constant-folding
concatenations of string literals / Chr() calls in the unmasked text.
"""

import re

FEATURE_PATTERNS = {
    "auto_exec": [
        r"\bSub\s+(?:AutoOpen|Auto_Open|AutoExec|AutoClose|Auto_Close|"
        r"Document_Open|Document_Close|Workbook_Open|Workbook_Activate)\b",
    ],
    "process_create": [
        r"\bShell\s*\(",
        r"\bShell\s+[\w]",
        r"\bWScript\.Shell\b",
        r"\.\s*Run\b",
        r"\.\s*Exec\s*\(",
        r"\bWin32_Process\b",
    ],
    "file_write": [
        r"\bOpen\b.*\bFor\s+(?:Output|Append|Binary)\b",
        r"\bCreateTextFile\b",
        r"\bSaveToFile\b",
        r"\bPrint\s*#",
        r"\bWrite\s*#",
        r"\bFileSystemObject\b",
        r"\bADODB\.Stream\b",
    ],
    "network_download": [
        r"\bXMLHTTP\b",
        r"\bWinHttp",
        r"\bURLDownloadToFile\b",
        r"\bWebClient\b",
        r"\bDownloadFile\b",
        r"\bInternetExplorer\.Application\b",
    ],
}

_COMPILED = {
    name: [re.compile(p, re.IGNORECASE) for p in pats]
    for name, pats in FEATURE_PATTERNS.items()
}

# A constant string operand: a quoted literal or Chr()/ChrW() call.
_OPERAND = r'(?:"(?:[^"]|"")*"|ChrW?\s*\(\s*\d+\s*\))'
_CONCAT_RE = re.compile(
    _OPERAND + r"(?:\s*[&+]\s*" + _OPERAND + r")+")
_OPERAND_RE = re.compile(_OPERAND)
_CHR_RE = re.compile(r"ChrW?\s*\(\s*(\d+)\s*\)", re.IGNORECASE)

MAX_SNIPPET = 120


def _eval_operand(token: str) -> str:
    token = token.strip()
    if token.startswith('"'):
        return token[1:-1].replace('""', '"')
    m = _CHR_RE.match(token)
    if not m:
        raise ValueError("not a constant operand")
    value = int(m.group(1))
    if not 0 <= value <= 0xFFFF:
        raise ValueError("Chr value out of range")
    return chr(value)


def fold_concat_expressions(text: str):
    """Yield (expression, folded_value) for constant string concatenations."""
    for m in _CONCAT_RE.finditer(text):
        expr = m.group(0)
        operands = _OPERAND_RE.findall(expr)
        if len(operands) < 2:
            continue
        try:
            value = "".join(_eval_operand(op) for op in operands)
        except ValueError:
            continue
        yield expr, value


def _evidence(module_name, stmt):
    snippet = stmt["text"]
    if len(snippet) > MAX_SNIPPET:
        snippet = snippet[:MAX_SNIPPET] + "..."
    return {
        "module": module_name,
        "statement": stmt["index"],
        "snippet": snippet,
    }


def extract_features(module_name: str, statements):
    """Return the feature list for one module's normalised statements."""
    features = []
    for stmt in statements:
        masked = stmt["masked"]
        for ftype, patterns in _COMPILED.items():
            if any(p.search(masked) for p in patterns):
                features.append({
                    "type": ftype,
                    "evidence": _evidence(module_name, stmt),
                })
        # constant folding of string concatenations (unmasked text)
        for expr, value in fold_concat_expressions(stmt["text"]):
            features.append({
                "type": "obfuscation_string_concat",
                "evidence": _evidence(module_name, stmt),
                "detail": "constant-folds to %r" % value,
            })
    return features
