"""Deterministic edit ledger serialization and replay."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode, fail
from .model import V2Sample, sample_checksum


@dataclass
class AuditLedger:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, entry: dict[str, Any]) -> None:
        self.entries.append(copy.deepcopy(entry))

    def applied(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if entry["status"] == "applied"]

    def to_dict(self) -> dict[str, Any]:
        return {"entries": copy.deepcopy(self.entries)}

    def to_jsonl(self) -> str:
        return "".join(
            json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
            for entry in self.entries
        )

    @classmethod
    def from_jsonl(cls, text: str) -> "AuditLedger":
        ledger = cls()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                fail(
                    ErrorCode.FORMAT_INVALID,
                    f"invalid audit JSONL at line {line_no}",
                    path=f"line:{line_no}",
                    stage="audit",
                    line=line_no,
                    details={"column": exc.colno},
                )
            ledger.append(entry)
        return ledger


def replay_edit_log(
    initial: V2Sample,
    ledger: AuditLedger,
    *,
    ontology: Any,
    limits: Any | None = None,
) -> V2Sample:
    """Replay every successful edit and reject broken checksum chains."""

    from .editing import Editor

    replay_editor = Editor(
        initial,
        ontology,
        limits=limits,
        record_failures=False,
    )
    expected_before = sample_checksum(initial)
    for index, entry in enumerate(ledger.entries):
        seq = entry.get("seq")
        if seq != index + 1:
            fail(
                ErrorCode.INCONSISTENT,
                "audit sequence is not contiguous",
                path=f"$.audit[{index}].seq",
                stage="audit_replay",
                details={"expected": index + 1, "actual": seq},
            )
        if entry.get("before_checksum") != expected_before:
            fail(
                ErrorCode.INCONSISTENT,
                "audit before_checksum does not match the replayed state",
                path=f"$.audit[{index}].before_checksum",
                stage="audit_replay",
                details={
                    "expected": expected_before,
                    "actual": entry.get("before_checksum"),
                },
            )
        status = entry.get("status")
        if status == "applied":
            replay_editor.edit(
                entry["operation"],
                entry["payload"],
                record=False,
            )
        elif status != "rejected":
            fail(
                ErrorCode.INCONSISTENT,
                "audit entry has an unknown status",
                path=f"$.audit[{index}].status",
                stage="audit_replay",
                details={"status": status},
            )
        expected_after = sample_checksum(replay_editor.sample)
        if entry.get("after_checksum") != expected_after:
            fail(
                ErrorCode.INCONSISTENT,
                "audit after_checksum does not match the replayed state",
                path=f"$.audit[{index}].after_checksum",
                stage="audit_replay",
                details={
                    "expected": expected_after,
                    "actual": entry.get("after_checksum"),
                },
            )
        expected_before = expected_after
    return replay_editor.sample

