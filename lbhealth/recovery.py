"""Recovery after a forced termination or clean restart."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .engine import HealthEngine
from .errors import LedgerError
from .ledger import Ledger
from .models import RuntimeState


@dataclass
class RecoveryReport:
    status: str
    recovered_round: int
    next_round: int
    complete_rounds_recovered: int
    unknown_rounds: list[dict[str, Any]]
    requires_rerun: bool
    in_flight_results_recovered: bool = False
    note: str = (
        "Only committed complete rounds were restored. Incomplete attempts are marked "
        "unknown and must be rerun; no in-flight probe result is claimed as recovered."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "recovered_round": self.recovered_round,
            "next_round": self.next_round,
            "complete_rounds_recovered": self.complete_rounds_recovered,
            "unknown_rounds": self.unknown_rounds,
            "requires_rerun": self.requires_rerun,
            "in_flight_results_recovered": self.in_flight_results_recovered,
            "note": self.note,
        }


@dataclass
class RecoveredSession:
    engine: HealthEngine
    report: RecoveryReport


def recover_session(config: Config, ledger: Ledger) -> RecoveredSession:
    ledger.recover_incomplete()
    unknown = ledger.unresolved_unknowns()
    latest = ledger.latest_complete()
    state: RuntimeState | None = None
    if latest is not None:
        if (
            latest["config_version"] != config.config_version
            or latest["member_digest"] != config.member_digest
            or latest["config_digest"] != config.config_digest
        ):
            raise LedgerError(
                "E_LEDGER_CONFIG",
                "ledger was written by a different configuration; restore a snapshot or use a new ledger",
                {
                    "ledger_config_version": latest["config_version"],
                    "ledger_member_digest": latest["member_digest"],
                    "ledger_config_digest": latest["config_digest"],
                    "current_config_version": config.config_version,
                    "current_member_digest": config.member_digest,
                    "current_config_digest": config.config_digest,
                },
            )
        state = RuntimeState.from_dict(_loads(latest["state_json"]))
    engine = HealthEngine(config, state)
    complete_count = len(ledger.complete_rounds_upto(engine.current_round))
    report = RecoveryReport(
        status="ok",
        recovered_round=engine.current_round,
        next_round=engine.current_round + 1,
        complete_rounds_recovered=complete_count,
        unknown_rounds=[item.to_dict() for item in unknown],
        requires_rerun=bool(unknown),
    )
    return RecoveredSession(engine=engine, report=report)


def _loads(value: str) -> dict[str, Any]:
    import json

    return json.loads(value)
