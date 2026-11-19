"""End-to-end scheduling, execution, ledger settlement and snapshot commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError
from .executor import SpawnExecutor
from .ledger import BudgetLedger
from .models import PipelineConfig, Task
from .scheduler import build_plan
from .snapshots import SnapshotStore
from .stage import ErrorJournal, StageGate


class Pipeline:
    def __init__(
        self,
        root: str | Path,
        *,
        snapshot_dir: str | Path | None = None,
        runtime_dir: str | Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.snapshot_store = SnapshotStore(
            snapshot_dir or self.root / "data" / "runtime" / "snapshots"
        )
        self.runtime_dir = Path(
            runtime_dir or self.root / "data" / "runtime"
        )
        self.gate = StageGate(self.runtime_dir)
        self.journal = ErrorJournal(self.runtime_dir)

    def run(
        self,
        tasks: Iterable[Task],
        config: PipelineConfig,
    ) -> dict[str, Any]:
        try:
            self.gate.acquire()
        except BusinessError as exc:
            self.journal.append_error(exc, source="stage")
            raise

        try:
            stage_version = self.snapshot_store.current_version() + 1
            plan = build_plan(tasks, config)
            plan["stage_version"] = stage_version
            plan_envelope = self.snapshot_store.commit(
                "plan",
                plan,
                stage_version,
            )

            ledger = BudgetLedger(config)
            ledger.reserve_plan(plan)
            execution = SpawnExecutor(config).run(plan)
            ledger.settle_results(execution["results"])
            ledger.assert_invariants()

            ledger_payload = ledger.snapshot()
            ledger_payload.update(
                {
                    "stage_version": stage_version,
                    "plan_checksum": plan_envelope.checksum,
                }
            )
            ledger_envelope = self.snapshot_store.commit(
                "ledger",
                ledger_payload,
                stage_version,
            )

            results_payload = {
                "schema_version": 1,
                "stage_version": stage_version,
                "plan_checksum": plan_envelope.checksum,
                "ledger_checksum": ledger_envelope.checksum,
                "execution_order": plan["execution_order"],
                "merge_order": execution["merge_order"],
                "execution_chunks": execution["execution_chunks"],
                "pool_mode": execution["pool_mode"],
                "worker_count": execution["worker_count"],
                "results": execution["results"],
            }
            results_envelope = self.snapshot_store.commit(
                "results",
                results_payload,
                stage_version,
            )

            for result in execution["results"]:
                if result["status"] == "error":
                    self.journal.append(
                        result["business_code"],
                        result["stage"],
                        result["message"],
                        {
                            "job_id": result["job_id"],
                            "details": result.get("details", {}),
                        },
                        source="worker_result",
                    )

            return {
                "stage_version": stage_version,
                "plan": plan,
                "ledger": ledger_payload,
                "results": execution["results"],
                "snapshots": {
                    "plan": {
                        "version": plan_envelope.version,
                        "checksum": plan_envelope.checksum,
                    },
                    "ledger": {
                        "version": ledger_envelope.version,
                        "checksum": ledger_envelope.checksum,
                    },
                    "results": {
                        "version": results_envelope.version,
                        "checksum": results_envelope.checksum,
                    },
                },
            }
        except BusinessError as exc:
            self.journal.append_error(exc)
            raise
        except Exception as exc:
            error = BusinessError(
                "E_PIPELINE_INTERNAL",
                f"{type(exc).__name__}: {exc}",
                stage="pipeline",
            )
            self.journal.append_error(error)
            raise error from exc
        finally:
            self.gate.release()
