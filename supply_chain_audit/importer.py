"""Bounded concurrent snapshot import and real wall-clock budgets."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .codes import E_HASH_BUDGET, E_PARSE_JSON, E_QUEUE_FULL, E_TIMEOUT
from .digests import compute_payload_digest
from .parsing import SnapshotParseError, load_snapshot


class BudgetExceeded(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TimeoutExceeded(BudgetExceeded):
    def __init__(self, elapsed_seconds: float, timeout_seconds: float):
        super().__init__(E_TIMEOUT, f"batch exceeded {timeout_seconds:.6f}s wall-clock budget")
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds


@dataclass
class ImportBudget:
    concurrency: int = 1
    timeout_seconds: float | None = None
    queue_limit: int | None = None
    max_hash_operations: int | None = None
    hmac_key: bytes | None = b"supply-chain-audit-fixed-local-hmac-key"

    def normalized_workers(self) -> int:
        return max(1, int(self.concurrency or 1))


class BatchExecution:
    """Per-batch mutable meter and cooperative cancellation boundary."""

    def __init__(self, budget: ImportBudget):
        self.budget = budget
        self.started_at = time.monotonic()
        self.deadline = (
            self.started_at + budget.timeout_seconds
            if budget.timeout_seconds is not None
            else None
        )
        self.hash_operations = 0
        self.records_processed = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def check_timeout(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutExceeded(self.elapsed(), self.budget.timeout_seconds or 0.0)

    def tick(self, records: int = 1) -> None:
        self.records_processed += records
        self.check_timeout()

    def compute_payload_digest(self, payload: bytes, algorithm: str) -> str:
        if (
            self.budget.max_hash_operations is not None
            and self.hash_operations >= self.budget.max_hash_operations
        ):
            raise BudgetExceeded(
                E_HASH_BUDGET,
                f"batch reached maximum of {self.budget.max_hash_operations} digest computations",
            )
        self.check_timeout()
        digest = compute_payload_digest(payload, algorithm, self.budget.hmac_key)
        self.hash_operations += 1
        self.check_timeout()
        return digest


def verify_claim_contents(
    parse_result: Any,
    base_dir: str | Path | None,
    execution: BatchExecution,
) -> list[dict[str, Any]]:
    """Recompute optional deterministic payload digests for parsed claims."""

    errors = []
    base = Path(base_dir) if base_dir else Path.cwd()
    for claim in parse_result.claims:
        execution.check_timeout()
        content_path = claim.get("content_path")
        if not content_path:
            execution.tick()
            continue
        path = Path(content_path)
        if not path.is_absolute():
            candidates = [base / path, Path.cwd() / path]
            if parse_result.path:
                candidates.append(Path(parse_result.path).parent / path)
            path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        payload = path.read_bytes()
        actual = execution.compute_payload_digest(payload, claim["algorithm_normalized"])
        execution.tick()
        if actual != claim["digest_normalized"]:
            errors.append(
                {
                    "code": "E_CONTENT_DIGEST_MISMATCH",
                    "artifact_key": claim["artifact_key"],
                    "expected_prefix": claim["digest_normalized"][:12],
                    "actual_prefix": actual[:12],
                }
            )
    return errors


def process_snapshot_batch(
    batch: str | Path | dict[str, Any],
    clock: dict[str, Any] | None,
    budget: ImportBudget | None = None,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    budget = budget or ImportBudget()
    execution = BatchExecution(budget)
    if isinstance(batch, (str, Path)):
        request = {"batch_id": Path(batch).stem, "path": str(batch)}
    else:
        request = dict(batch)

    batch_id = request.get("batch_id", "<unnamed-batch>")
    try:
        execution.check_timeout()
        snapshot_path = request.get("path")
        payload = request.get("payload")
        if snapshot_path is not None:
            parse_result = load_snapshot(snapshot_path, clock)
        elif payload is not None:
            # Payload batches are primarily useful in tests and library use.
            from .parsing import parse_v1, parse_v2_manifest

            if payload.get("schema") == "supply-chain-audit/v1":
                parse_result = parse_v1(payload, payload.get("snapshot_id", batch_id), clock or {})
            else:
                raise SnapshotParseError(E_PARSE_JSON, "payload v2 batches require a manifest path")
        else:
            raise SnapshotParseError(E_PARSE_JSON, "batch has neither path nor payload")

        execution.check_timeout()
        content_errors = verify_claim_contents(
            parse_result, request.get("base_dir", base_dir), execution
        )
        elapsed = execution.elapsed()
        if parse_result.fatal_error:
            return {
                "batch_id": batch_id,
                "status": "failed",
                "code": parse_result.fatal_error.get("code", E_PARSE_JSON),
                "message": parse_result.fatal_error.get("message"),
                "elapsed_seconds": elapsed,
                "hash_operations": execution.hash_operations,
                "records_processed": execution.records_processed,
                "parse_result": parse_result,
                "content_errors": content_errors,
            }
        return {
            "batch_id": batch_id,
            "status": "parsed",
            "elapsed_seconds": elapsed,
            "hash_operations": execution.hash_operations,
            "records_processed": execution.records_processed,
            "parse_result": parse_result,
            "content_errors": content_errors,
        }
    except TimeoutExceeded as exc:
        return {
            "batch_id": batch_id,
            "status": "failed",
            "code": E_TIMEOUT,
            "message": str(exc),
            "elapsed_seconds": execution.elapsed(),
            "hash_operations": execution.hash_operations,
            "records_processed": execution.records_processed,
            "terminated_early": True,
        }
    except BudgetExceeded as exc:
        return {
            "batch_id": batch_id,
            "status": "failed",
            "code": exc.code,
            "message": str(exc),
            "elapsed_seconds": execution.elapsed(),
            "hash_operations": execution.hash_operations,
            "records_processed": execution.records_processed,
            "terminated_early": True,
        }
    except SnapshotParseError as exc:
        return {
            "batch_id": batch_id,
            "status": "failed",
            "code": exc.code,
            "message": exc.message,
            "elapsed_seconds": execution.elapsed(),
            "hash_operations": execution.hash_operations,
            "records_processed": execution.records_processed,
        }
    except (OSError, ValueError) as exc:
        return {
            "batch_id": batch_id,
            "status": "failed",
            "code": E_PARSE_JSON,
            "message": str(exc),
            "elapsed_seconds": execution.elapsed(),
            "hash_operations": execution.hash_operations,
            "records_processed": execution.records_processed,
        }
    except Exception as exc:  # noqa: BLE001 - one malformed batch must not break peers
        return {
            "batch_id": batch_id,
            "status": "failed",
            "code": E_PARSE_JSON,
            "message": f"unexpected snapshot processing failure: {exc}",
            "elapsed_seconds": execution.elapsed(),
            "hash_operations": execution.hash_operations,
            "records_processed": execution.records_processed,
        }


class BatchImporter:
    """Import batches concurrently while preserving parse failures."""

    def __init__(self, clock: dict[str, Any] | None = None, budget: ImportBudget | None = None, engine: Any = None):
        self.clock = clock
        self.budget = budget or ImportBudget()
        self.engine = engine

    def import_batches(self, batches: list[str | Path | dict[str, Any]]) -> dict[str, Any]:
        accepted: dict[Any, dict[str, Any]] = {}
        outcomes_by_index: dict[int, dict[str, Any]] = {}
        queue_rejections: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=self.budget.normalized_workers()) as executor:
            for index, batch in enumerate(batches):
                batch_id = batch.get("batch_id") if isinstance(batch, dict) else Path(str(batch)).stem
                if self.budget.queue_limit is not None and len(accepted) >= self.budget.queue_limit:
                    rejection = {
                        "batch_id": batch_id,
                        "status": "rejected",
                        "code": E_QUEUE_FULL,
                        "message": f"pending batch queue limit {self.budget.queue_limit} exceeded",
                    }
                    queue_rejections.append(rejection)
                    outcomes_by_index[index] = rejection
                    continue
                future = executor.submit(process_snapshot_batch, batch, self.clock, self.budget)
                accepted[future] = {"batch_id": batch_id, "batch": batch, "index": index}

            for future, metadata in accepted.items():
                processed = future.result()
                if self.engine is not None and processed.get("status") == "parsed":
                    applied = self.engine.apply_parse_result(
                        processed["parse_result"], processed["batch_id"]
                    )
                    processed["apply"] = applied
                outcomes_by_index[metadata["index"]] = _public_outcome(processed)

        outcomes = [outcomes_by_index[index] for index in range(len(batches))]
        accepted_count = len(accepted)
        failed_count = sum(outcome.get("status") == "failed" for outcome in outcomes)
        rejected_count = sum(outcome.get("code") == E_QUEUE_FULL for outcome in outcomes)
        timeout_count = sum(outcome.get("code") == E_TIMEOUT for outcome in outcomes)
        return {
            "schema": "supply-chain-audit/import-report/v1",
            "concurrency": self.budget.normalized_workers(),
            "queue_limit": self.budget.queue_limit,
            "timeout_seconds": self.budget.timeout_seconds,
            "max_hash_operations": self.budget.max_hash_operations,
            "accepted_count": accepted_count,
            "outcome_count": len(outcomes),
            "failed_count": failed_count,
            "rejected_count": rejected_count,
            "timeout_count": timeout_count,
            "outcomes": outcomes,
            "queue_rejections": queue_rejections,
        }


def _public_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in outcome.items() if key != "parse_result"}


def run_wallclock_step_probe(step: Callable[[int], None], steps: int, budget: ImportBudget) -> dict[str, Any]:
    """Test/helper entry point proving timeout uses monotonic wall time."""

    execution = BatchExecution(budget)
    completed = 0
    try:
        for index in range(steps):
            execution.check_timeout()
            step(index)
            completed += 1
        return {"status": "completed", "completed": completed, "elapsed_seconds": execution.elapsed()}
    except TimeoutExceeded:
        return {
            "status": "failed",
            "code": E_TIMEOUT,
            "completed": completed,
            "elapsed_seconds": execution.elapsed(),
        }
