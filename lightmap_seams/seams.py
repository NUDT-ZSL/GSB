"""Loading contract: one seam per row, four endpoint UV values.

Required information: the UV pair of each endpoint.  Common column spellings
are accepted (``u1/v1/u2/v2``, ``u_a/v_a/u_b/v_b``, ``x1/y1/x2/y2`` ...); see
``COLUMN_ALIASES``.  A ``seam_id`` column is optional and only used for
labelling.

Duplicate endpoint pairs (in either endpoint order) are collapsed to one
seam and reported in ``duplicate_count``.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from pathlib import Path

CANONICAL = ("u1", "v1", "u2", "v2")

COLUMN_ALIASES = {
    "u1": ("u1", "u_a", "ua", "u_start", "ustart", "start_u", "x1", "x_a", "xa"),
    "v1": ("v1", "v_a", "va", "v_start", "vstart", "start_v", "y1", "y_a", "ya"),
    "u2": ("u2", "u_b", "ub", "u_end", "uend", "end_u", "x2", "x_b", "xb"),
    "v2": ("v2", "v_b", "vb", "v_end", "vend", "end_v", "y2", "y_b", "yb"),
}
ID_COLUMNS = ("seam_id", "id", "name", "edge_id")
DELIMITERS = ",;\t"


class SeamFormatError(Exception):
    """The seam CSV exists but does not follow the required format."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Seam:
    seam_id: str
    line: int
    u1: float
    v1: float
    u2: float
    v2: float

    @property
    def endpoints(self) -> tuple[float, float, float, float]:
        return (self.u1, self.v1, self.u2, self.v2)

    def as_dict(self) -> dict:
        return {
            "seam_id": self.seam_id,
            "line": self.line,
            "u1": self.u1,
            "v1": self.v1,
            "u2": self.u2,
            "v2": self.v2,
        }


@dataclass
class ParsedSeams:
    seams: list[Seam]
    duplicate_count: int
    duplicates: list[dict]
    row_count: int = 0


def _duplicate_key(seam: Seam) -> tuple:
    a = (round(seam.u1, 9), round(seam.v1, 9))
    b = (round(seam.u2, 9), round(seam.v2, 9))
    return tuple(sorted((a, b)))


def _detect_delimiter(text: str) -> str:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITERS).delimiter
    except csv.Error:
        # Sniffing fails on very small uniform files; fall back to comma.
        return ","


def _read_rows(path: Path) -> list[tuple[int, list[str]]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SeamFormatError(
            f"接缝清单无法读取：文件不是 UTF-8 文本（{exc}）"
        ) from exc
    except OSError as exc:
        raise SeamFormatError(f"接缝清单无法读取：{exc}") from exc

    delimiter = _detect_delimiter(text)
    # StringIO keeps quoted multi-line fields intact; each reader row still
    # corresponds to one CSV record, which is what the line numbers in
    # error messages refer to.
    raw_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows: list[tuple[int, list[str]]] = []
    for line_no, row in enumerate(raw_rows, start=1):
        if not row or all(not cell.strip() for cell in row):
            continue
        rows.append((line_no, row))
    return rows


def _resolve_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in header:
                mapping[canonical] = header.index(alias)
                break
    return mapping


def parse_seams(path: Path) -> ParsedSeams:
    """Parse and de-duplicate a seam CSV.

    Raises:
        FileNotFoundError: the CSV is absent.
        SeamFormatError: missing endpoint columns, malformed rows, or
            non-numeric coordinates.
    """

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    rows = _read_rows(path)
    if not rows:
        raise SeamFormatError("接缝清单为空：缺少表头行")

    header_line, raw_header = rows[0]
    header = [cell.strip().lower() for cell in raw_header]
    mapping = _resolve_columns(header)
    missing = [name for name in CANONICAL if name not in mapping]
    if missing:
        raise SeamFormatError(
            "接缝清单列缺失：缺少必要列 "
            + "、".join(missing)
            + f"（需要端点 UV 四列 u1、v1、u2、v2；表头位于第 {header_line} 行）"
        )

    id_index = None
    for candidate in ID_COLUMNS:
        if candidate in header:
            id_index = header.index(candidate)
            break

    seams: list[Seam] = []
    duplicates: list[dict] = []
    seen: dict[tuple, Seam] = {}

    for ordinal, (line_no, row) in enumerate(rows[1:], start=1):
        if len(row) < len(header):
            raise SeamFormatError(
                f"接缝清单第 {line_no} 行字段数不足：表头有 {len(header)} 列，"
                f"该行只有 {len(row)} 列"
            )
        # Extra trailing cells are ignored: only a short row is a format
        # error under the contract.

        values: dict[str, float] = {}
        for canonical in CANONICAL:
            raw_value = row[mapping[canonical]].strip()
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise SeamFormatError(
                    f"接缝清单第 {line_no} 行的 {canonical} 不是数值：{raw_value!r}"
                ) from exc
            if not math.isfinite(value):
                raise SeamFormatError(
                    f"接缝清单第 {line_no} 行的 {canonical} 不是有限数值："
                    f"{raw_value!r}"
                )
            values[canonical] = value

        seam_id = ""
        if id_index is not None:
            seam_id = row[id_index].strip()
        if not seam_id:
            seam_id = f"seam_{ordinal:03d}"

        seam = Seam(
            seam_id=seam_id,
            line=line_no,
            u1=values["u1"],
            v1=values["v1"],
            u2=values["u2"],
            v2=values["v2"],
        )

        key = _duplicate_key(seam)
        original = seen.get(key)
        if original is not None:
            duplicates.append(
                {
                    "seam_id": seam.seam_id,
                    "line": seam.line,
                    "duplicate_of": original.seam_id,
                }
            )
            continue
        seen[key] = seam
        seams.append(seam)

    return ParsedSeams(
        seams=seams,
        duplicate_count=len(duplicates),
        duplicates=duplicates,
        row_count=len(rows) - 1,
    )
