from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import Rules
from .errors import (
    E_DUPLICATE_SESSION,
    E_FORMAT_CORRUPT,
    fail,
)
from .models import Candidate
from .validation import (
    require_mapping,
    validate_confidence,
    validate_json_value,
    validate_session_id,
    validate_slot_key,
    validate_turn_no,
)


TURN_FIELDS = {
    "session_id",
    "turn_no",
    "user_text",
    "assistant_text",
    "slot_candidates",
}


@dataclass(frozen=True)
class ParsedTurn:
    session_id: str
    turn_no: int
    user_text: str
    assistant_text: str
    candidates: tuple[Candidate, ...]
    clarification_requests: tuple[str, ...]

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_no": self.turn_no,
            "user_text": self.user_text,
            "assistant_text": self.assistant_text,
            "slot_candidates": [candidate.to_dict() for candidate in self.candidates],
            "clarification_requests": list(self.clarification_requests),
        }

    def raw_row(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_no": self.turn_no,
            "user_text": self.user_text,
            "assistant_text": self.assistant_text,
            "slot_candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def _candidate_from_record(
    record: dict[str, Any],
    *,
    field: str,
    turn_no: int,
    rules: Rules,
) -> Candidate:
    if "slot_key" not in record:
        fail(E_FORMAT_CORRUPT, "slot candidate is missing slot_key", field=f"{field}.slot_key")
    slot_key = validate_slot_key(record["slot_key"], rules, f"{field}.slot_key")
    if "value" not in record:
        fail(E_FORMAT_CORRUPT, "slot candidate is missing value", field=f"{field}.value")
    value = validate_json_value(record["value"], f"{field}.value")
    if "confidence" not in record:
        fail(
            E_FORMAT_CORRUPT,
            "slot candidate is missing confidence",
            field=f"{field}.confidence",
        )
    confidence = validate_confidence(record["confidence"], f"{field}.confidence")
    supplied_key = record.get("candidate_key")
    if supplied_key is not None and not isinstance(supplied_key, str):
        fail(
            E_FORMAT_CORRUPT,
            "candidate_key must be a string",
            field=f"{field}.candidate_key",
        )
    try:
        return Candidate.build(slot_key, value, confidence, turn_no, supplied_key)
    except ValueError as exc:
        fail(
            E_FORMAT_CORRUPT,
            str(exc),
            field=f"{field}.candidate_key",
        )


def parse_slot_candidates(
    raw: Any,
    *,
    turn_no: int,
    field: str,
    rules: Rules,
) -> tuple[Candidate, ...]:
    candidates: list[Candidate] = []
    if isinstance(raw, dict):
        for index, slot_key in enumerate(raw):
            item_field = f"{field}.{slot_key}"
            validate_slot_key(slot_key, rules, item_field)
            record = require_mapping(raw[slot_key], item_field)
            merged = dict(record)
            if "slot_key" in merged and merged["slot_key"] != slot_key:
                fail(
                    E_FORMAT_CORRUPT,
                    "mapping key and record slot_key disagree",
                    field=f"{item_field}.slot_key",
                )
            merged["slot_key"] = slot_key
            candidates.append(
                _candidate_from_record(
                    merged,
                    field=item_field,
                    turn_no=turn_no,
                    rules=rules,
                )
            )
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            item_field = f"{field}[{index}]"
            record = require_mapping(item, item_field)
            candidates.append(
                _candidate_from_record(
                    record,
                    field=item_field,
                    turn_no=turn_no,
                    rules=rules,
                )
            )
    else:
        fail(
            E_FORMAT_CORRUPT,
            "slot_candidates must be an object or an array",
            field=field,
        )

    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.slot_key,
                candidate.value_key,
                candidate.confidence,
                candidate.candidate_key,
            ),
        )
    )


def extract_clarification_requests(
    assistant_text: str,
    *,
    field: str,
    rules: Rules,
) -> tuple[str, ...]:
    found: set[str] = set()
    for match in rules.clarification_regex.findall(assistant_text):
        parts = [part.strip() for part in match.split(",")]
        for part in parts:
            if not part:
                fail(
                    E_FORMAT_CORRUPT,
                    "clarification request contains an empty slot key",
                    field=field,
                )
            found.add(validate_slot_key(part, rules, field))
    return tuple(sorted(found))


def parse_turn_row(
    row: Any,
    *,
    line_no: int,
    rules: Rules,
    source: str,
) -> ParsedTurn:
    if not isinstance(row, dict):
        fail(
            E_FORMAT_CORRUPT,
            "JSONL line must be a JSON object",
            field=f"line:{line_no}",
            details={"source": source},
        )
    actual_fields = set(row)
    if actual_fields != TURN_FIELDS:
        missing = sorted(TURN_FIELDS - actual_fields)
        extra = sorted(actual_fields - TURN_FIELDS)
        fail(
            E_FORMAT_CORRUPT,
            "turn line field set does not match the schema",
            field=f"line:{line_no}.fields",
            details={
                "expected_count": len(TURN_FIELDS),
                "actual_count": len(actual_fields),
                "missing": missing,
                "extra": extra,
            },
        )
    session_id = validate_session_id(row["session_id"], f"line:{line_no}.session_id")
    turn_no = validate_turn_no(row["turn_no"], f"line:{line_no}.turn_no")
    user_text = row["user_text"]
    assistant_text = row["assistant_text"]
    if not isinstance(user_text, str):
        fail(E_FORMAT_CORRUPT, "user_text must be a string", field=f"line:{line_no}.user_text")
    if not isinstance(assistant_text, str):
        fail(
            E_FORMAT_CORRUPT,
            "assistant_text must be a string",
            field=f"line:{line_no}.assistant_text",
        )
    candidates = parse_slot_candidates(
        row["slot_candidates"],
        turn_no=turn_no,
        field=f"line:{line_no}.slot_candidates",
        rules=rules,
    )
    requests = extract_clarification_requests(
        assistant_text,
        field=f"line:{line_no}.assistant_text",
        rules=rules,
    )
    return ParsedTurn(
        session_id=session_id,
        turn_no=turn_no,
        user_text=user_text,
        assistant_text=assistant_text,
        candidates=candidates,
        clarification_requests=requests,
    )


def parse_turn_rows(
    rows: Iterable[dict[str, Any]],
    *,
    rules: Rules,
    source: str = "<memory>",
) -> tuple[ParsedTurn, ...]:
    parsed: list[ParsedTurn] = []
    seen: dict[tuple[str, int], int] = {}
    for line_no, row in enumerate(rows, start=1):
        turn = parse_turn_row(row, line_no=line_no, rules=rules, source=source)
        business_key = (turn.session_id, turn.turn_no)
        if business_key in seen:
            fail(
                E_DUPLICATE_SESSION,
                "duplicate (session_id, turn_no) business key",
                field="session_id",
                details={
                    "session_id": turn.session_id,
                    "turn_no": turn.turn_no,
                    "first_line": seen[business_key],
                    "duplicate_line": line_no,
                },
            )
        seen[business_key] = line_no
        parsed.append(turn)
    return tuple(
        sorted(parsed, key=lambda turn: (turn.session_id, turn.turn_no))
    )


def load_turns(path: str | Path, rules: Rules) -> tuple[ParsedTurn, ...]:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    fail(
                        E_FORMAT_CORRUPT,
                        f"invalid JSONL: {exc.msg}",
                        field=f"line:{line_no}",
                        details={"source": str(source), "column": exc.colno},
                    )
                if not isinstance(row, dict):
                    fail(
                        E_FORMAT_CORRUPT,
                        "JSONL line must be an object",
                        field=f"line:{line_no}",
                    )
                rows.append(row)
    except FileNotFoundError:
        fail(E_FORMAT_CORRUPT, "turns file does not exist", field=str(source))
    except OSError as exc:
        fail(E_FORMAT_CORRUPT, f"turns file cannot be read: {exc}", field=str(source))
    return parse_turn_rows(rows, rules=rules, source=str(source))


def group_turns_by_session(
    turns: Iterable[ParsedTurn],
) -> dict[str, tuple[ParsedTurn, ...]]:
    grouped: dict[str, list[ParsedTurn]] = {}
    for turn in turns:
        grouped.setdefault(turn.session_id, []).append(turn)
    return {
        session_id: tuple(sorted(items, key=lambda item: item.turn_no))
        for session_id, items in sorted(grouped.items())
    }

