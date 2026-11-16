"""v1 exchange format: fixed-width plain-text lines.

Layout (48 chars per line):
  inst_id      8 chars, left-justified
  category    12 chars, left-justified
  x0,y0,x1,y1  5 chars each, zero-padded integers
  confidence   4 chars, zero-padded permille integer (0..1000)
"""

from __future__ import annotations

from .errors import E_PARSE_INVALID, WorkbenchError

FIELD_WIDTHS = [("inst_id", 8), ("category", 12), ("x0", 5), ("y0", 5),
                ("x1", 5), ("y1", 5), ("conf", 4)]
V1_LINE_WIDTH = sum(w for _n, w in FIELD_WIDTHS)  # 48


def format_v1_record(rec: dict) -> str:
    x0, y0, x1, y1 = rec["bbox"]
    return (f"{rec['inst_id']:<8.8}{rec['category']:<12.12}"
            f"{x0:05d}{y0:05d}{x1:05d}{y1:05d}{rec['conf_permille']:04d}")


def serialize_v1(records: list[dict]) -> str:
    return "\n".join(format_v1_record(r) for r in records) + ("\n" if records else "")


def parse_v1_line(line: str, lineno: int) -> dict:
    if len(line) < V1_LINE_WIDTH:
        raise WorkbenchError(
            E_PARSE_INVALID,
            f"line {lineno}: length {len(line)} < fixed width {V1_LINE_WIDTH} "
            "(missing field)", location=lineno)
    pos, fields = 0, {}
    for name, width in FIELD_WIDTHS:
        fields[name] = line[pos:pos + width]
        pos += width
    try:
        bbox = [int(fields[k]) for k in ("x0", "y0", "x1", "y1")]
        conf = int(fields["conf"])
    except ValueError as exc:
        raise WorkbenchError(E_PARSE_INVALID,
                             f"line {lineno}: non-integer field ({exc})",
                             location=lineno) from exc
    inst_id = fields["inst_id"].strip()
    category = fields["category"].strip()
    if not inst_id or not category:
        raise WorkbenchError(E_PARSE_INVALID,
                             f"line {lineno}: blank inst_id or category",
                             location=lineno)
    return {"inst_id": inst_id, "category": category, "bbox": bbox,
            "conf_permille": conf}


def parse_v1(text: str) -> tuple[list[dict], list[dict]]:
    """Parse v1 text. Returns (records, errors); errors carry line numbers."""
    records, errors = [], []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(parse_v1_line(line.rstrip("\r\n"), lineno))
        except WorkbenchError as exc:
            errors.append({"line": lineno, "code": exc.code, "detail": exc.detail})
    return records, errors
