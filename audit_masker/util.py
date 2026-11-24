from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_text(canonical_json(value))


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    validator: Optional[Callable[[Path], None]] = None,
) -> str:
    """Write bytes via a same-directory temp file and atomic replace."""
    ensure_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if validator is not None:
            validator(temp_path)
        os.replace(str(temp_path), str(path))
        fsync_directory(path.parent)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return sha256_bytes(payload)


def atomic_write_json(path: Path, value: Any, *, compact: bool = False) -> str:
    text = canonical_json(value) if compact else pretty_json(value) + "\n"
    payload = text.encode("utf-8")

    def validate_json_file(candidate: Path) -> None:
        with candidate.open("r", encoding="utf-8") as handle:
            json.load(handle)

    return atomic_write_bytes(path, payload, validator=validate_json_file)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def digest_ledger_rows(rows: Iterable[Dict[str, Any]]) -> str:
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        normalized.append(
            {
                "event_id": row["event_id"],
                "seq": int(row["seq"]),
                "field_path": row["field_path"],
                "decision": row["decision"],
                "masked_value": row["masked_value"],
                "winning_rule_name": row["winning_rule_name"],
                "winning_generation": int(row["winning_generation"]),
                "winning_threshold": row.get("winning_threshold"),
                "basis": row["basis"],
                "matches": row.get("matches", []),
                "suppressed": row.get("suppressed", []),
                "business_codes": row.get("business_codes", []),
                "layout_fingerprint": row.get("layout_fingerprint", ""),
            }
        )
    normalized.sort(key=lambda item: (item["event_id"], item["seq"], item["field_path"]))
    return canonical_digest(normalized)


def stable_event_list(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = list(events)
    result.sort(key=lambda item: (item["event_id"], int(item["seq"])))
    return result


def bounded_raw(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def parse_int_text(value: str) -> Optional[int]:
    text = value.strip()
    if not text:
        return None
    if text.startswith("+"):
        text = text[1:]
    if text.startswith("-"):
        digits = text[1:]
    else:
        digits = text
    if not digits.isdigit():
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None
