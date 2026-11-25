"""Reader and writer for version 1 flat pipe-delimited transcripts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .codes import Code, Diagnostic
from .models import Utterance
from .results import LoadResult
from .validation import V1_FIELDS, build_validated_utterances


V1_VERSION = "1.0"


def _detect_version(lines: list[str]) -> str | None:
    pattern = re.compile(r"transcript[-_ ]?format\s*[:=]\s*v?([0-9]+(?:\.[0-9]+)?)", re.I)
    compact_pattern = re.compile(r"^#\s*transcript\s+v?([0-9]+(?:\.[0-9]+)?)\s*$", re.I)
    for line in lines[:10]:
        stripped = line.strip()
        match = pattern.search(stripped)
        if match:
            value = match.group(1)
            return value if "." in value else f"{value}.0"
        match = compact_pattern.match(stripped)
        if match:
            value = match.group(1)
            return value if "." in value else f"{value}.0"
    return None


def parse_v1_text(text: str, source: str = "<text>") -> LoadResult:
    lines = text.splitlines()
    version = _detect_version(lines)
    header_index: int | None = None
    field_names: list[str] | None = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = [part.strip() for part in stripped.split("|")]
        if candidate[:2] == ["utterance_id", "start_ms"] or stripped.startswith("utterance_id|"):
            header_index = index
            field_names = candidate
            break

    diagnostics = []
    if field_names is None:
        if version is not None and version.split(".")[0] == "1":
            field_names = list(V1_FIELDS)
            data_start = next(
                (index + 1 for index, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")),
                0,
            )
        else:
            return LoadResult(
                format_name="v1",
                format_version=version,
                source=source,
                diagnostics=[
                    Diagnostic(
                        code=Code.E_FORMAT_HEADER,
                        message="v1 transcript requires a version header or pipe-delimited field set",
                        source=source,
                    )
                ],
            )
    else:
        data_start = header_index + 1 if header_index is not None else 0
        missing_header = [name for name in ("utterance_id", "start_ms", "end_ms") if name not in field_names]
        if missing_header:
            diagnostics.append(
                Diagnostic(
                    code=Code.E_FIELD_MISSING,
                    message="v1 header field set is missing required columns",
                    source=source,
                    line=(header_index + 1 if header_index is not None else None),
                    details={"fields": missing_header},
                )
            )
            return LoadResult("v1", version, [], diagnostics, source)

    rows: list[dict[str, object]] = []
    assert field_names is not None
    for line_number_zero_based in range(data_start, len(lines)):
        raw_line = lines[line_number_zero_based]
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_number = line_number_zero_based + 1
        parts = stripped.split("|")
        if len(parts) != len(field_names):
            diagnostics.append(
                Diagnostic(
                    code=Code.E_FIELD_TYPE,
                    message="v1 row field count does not match header",
                    source=source,
                    line=line_number,
                    details={"expected": len(field_names), "actual": len(parts)},
                )
            )
            continue
        row: dict[str, object] = {
            field_name: value.strip() for field_name, value in zip(field_names, parts)
        }
        row["_line"] = line_number
        row["_source"] = source
        rows.append(row)

    utterances, row_diagnostics = build_validated_utterances(
        rows,
        format_name="v1",
        required_fields=V1_FIELDS,
        confidence_required=True,
        source=source,
    )
    diagnostics.extend(row_diagnostics)
    return LoadResult("v1", version or V1_VERSION, utterances, diagnostics, source)


def read_v1(path: str | Path) -> LoadResult:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadResult(
            "v1",
            source=str(path),
            diagnostics=[Diagnostic(Code.E_NOT_FOUND, f"cannot read v1 file: {exc}", source=str(path))],
        )
    return parse_v1_text(text, source=str(path))


def write_v1(utterances: Iterable[Utterance], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# transcript-format: v1",
        "# fields: utterance_id|start_ms|end_ms|text|confidence",
        "|".join(V1_FIELDS),
    ]
    for item in sorted(utterances, key=lambda u: (u.start_ms, u.utterance_id)):
        text = item.text.replace("\n", " ").replace("|", "/")
        confidence = f"{item.confidence:.3f}".rstrip("0").rstrip(".")
        lines.append(
            "|".join(
                [
                    item.utterance_id,
                    str(item.start_ms),
                    str(item.end_ms),
                    text,
                    confidence,
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
