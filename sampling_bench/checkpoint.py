"""请求边界检查点与强制终止恢复。

检查点只在请求（批次）边界原子提交，记录：

* 已完成并提交的请求集合及其采样产物；
* 已消费预算账本事件（恢复后不重算、不重复计入）；
* 下一个待执行请求的标识。

未提交的中间采样状态不属于检查点，恢复时直接丢弃并重建。
"""

from __future__ import annotations
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import LedgerEvent, SampleResult
from .snapshot import checksum_of

CHECKPOINT_FILENAME = "checkpoint.json"


@dataclass
class Checkpoint:
    committed_ids: list[str]
    next_request_id: str | None
    results: list[SampleResult]
    ledger_events: list[LedgerEvent]
    consumed_steps: int
    consumed_cache: int
    consumed_slots: int
    checksum: str

    def to_envelope(self) -> dict[str, Any]:
        payload = {
            "committed_ids": list(self.committed_ids),
            "next_request_id": self.next_request_id,
            "results": [result.to_dict() for result in self.results],
            "ledger_events": [event.to_dict() for event in self.ledger_events],
            "consumed_steps": self.consumed_steps,
            "consumed_cache": self.consumed_cache,
            "consumed_slots": self.consumed_slots,
        }
        return {"checksum": checksum_of(payload), "payload": payload}

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> "Checkpoint":
        payload = envelope["payload"]
        stored = str(envelope["checksum"])
        actual = checksum_of(payload)
        if stored != actual:
            raise ValueError("checkpoint checksum mismatch")
        return cls(
            committed_ids=list(payload["committed_ids"]),
            next_request_id=payload["next_request_id"],
            results=[SampleResult.from_dict(item) for item in payload["results"]],
            ledger_events=[LedgerEvent.from_dict(e) for e in payload["ledger_events"]],
            consumed_steps=int(payload["consumed_steps"]),
            consumed_cache=int(payload["consumed_cache"]),
            consumed_slots=int(payload["consumed_slots"]),
            checksum=stored,
        )


class CheckpointStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.directory / CHECKPOINT_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    def write(self, checkpoint: Checkpoint) -> None:
        envelope = checkpoint.to_envelope()
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{CHECKPOINT_FILENAME}.", suffix=".tmp", dir=str(self.directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def read(self) -> Checkpoint | None:
        if not self.exists():
            return None
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        return Checkpoint.from_envelope(envelope)

    def clear(self) -> None:
        if self.exists():
            self.path.unlink()


@dataclass
class RecoveryReport:
    resumed: bool
    committed_ids: list[str] = field(default_factory=list)
    reexecuted_ids: list[str] = field(default_factory=list)
    disjoint: bool = True
    next_request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resumed": self.resumed,
            "committed_requests": list(self.committed_ids),
            "reexecuted_requests": list(self.reexecuted_ids),
            "sets_disjoint": self.disjoint,
            "next_request_id": self.next_request_id,
        }
