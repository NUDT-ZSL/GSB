"""Rule evaluation and per-file reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import documents
from .config import Rule
from .documents import matching_views, read_document

# A rule regex is evaluated against at most this many characters per view.
# Python's ``re`` has no timeout; capping the window bounds the worst case
# without changing results for any realistic Markdown document.
MAX_REGEX_INPUT = 4 * 1024 * 1024


@dataclass
class FileResult:
    file: str
    suspicious: bool
    matched_rules: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "suspicious": self.suspicious,
            "matched_rules": self.matched_rules,
            "error": self.error,
        }


def evaluate_document(text: str, rules: Sequence[Rule]) -> list[str]:
    """Return the names of every rule that matches the document.

    Matching runs against the raw text and the de-obfuscated views; rules are
    evaluated independently so the report lists all reasons, in config order
    and exactly once per rule.

    Regexes are evaluated with ``search`` on each view; ``MAX_REGEX_INPUT``
    caps the scanned window (see README for the trade-off).
    """
    views = matching_views(text)
    folded_views = [view.casefold() for view in views]

    matched: list[str] = []
    for rule in rules:
        hit = False
        for keyword in rule.keywords:
            if any(keyword in folded for folded in folded_views):
                hit = True
                break
        if not hit:
            for pattern in rule.patterns:
                if any(pattern.search(view[:MAX_REGEX_INPUT]) for view in views):
                    hit = True
                    break
        if hit:
            matched.append(rule.name)
    return matched


def scan_file(path: Path, display_path: str, rules: Sequence[Rule]) -> FileResult:
    """Scan one file; a read/decode failure is recorded instead of raised.

    A file whose content could not be inspected is treated as *untrusted*
    (``suspicious = true``, no rule names, reason in ``error``): a document we
    cannot audit must never be reported as clean.
    """
    text, error = read_document(path)
    if error is not None:
        return FileResult(file=display_path, suspicious=True, error=error)
    matched = evaluate_document(text, rules)
    return FileResult(file=display_path, suspicious=bool(matched), matched_rules=matched)


def scan_directory(root: Path, rules: Sequence[Rule]) -> list[FileResult]:
    """Scan every Markdown file below *root*; rows are sorted by path."""
    results = []
    for path in documents.discover_markdown(root):
        results.append(scan_file(path, path.relative_to(root).as_posix(), rules))
    results.sort(key=lambda row: row.file)
    return results


def summarise(results: Sequence[FileResult]) -> dict[str, int]:
    suspicious = sum(1 for row in results if row.suspicious)
    return {
        "total_files": len(results),
        "suspicious_files": suspicious,
        "clean_files": sum(1 for row in results if not row.suspicious),
        "files_with_errors": sum(1 for row in results if row.error is not None),
    }
