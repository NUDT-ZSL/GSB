from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .engine import BattleEngine
from .errors import BudgetError
from .ledger import Ledger
from .model import BattleSpec, Model


class LogicalClock:
    def now_ms(self) -> float:
        raise NotImplementedError


class ManualLogicalClock(LogicalClock):
    def __init__(self, start_ms: float = 0.0) -> None:
        self._value = float(start_ms)
        self._lock = threading.Lock()

    def now_ms(self) -> float:
        with self._lock:
            return self._value

    def advance(self, milliseconds: float) -> None:
        with self._lock:
            self._value += max(0.0, float(milliseconds))


class WallLogicalClock(LogicalClock):
    def __init__(self) -> None:
        self._start = time.monotonic_ns()

    def now_ms(self) -> float:
        return (time.monotonic_ns() - self._start) / 1_000_000.0


def estimate_chunk_buffer_bytes(
    model: Model, battle: BattleSpec, chunk_index: int
) -> int:
    start_round, end_round = battle.chunk_bounds(chunk_index)
    action_count = sum(
        len(battle.rounds[round_number - 1].actions)
        for round_number in range(start_round, end_round + 1)
    )
    config = model.config
    return (
        int(config["state_buffer_overhead_bytes"])
        + len(battle.unit_ids) * int(config["state_buffer_per_unit_bytes"])
        + (end_round - start_round + 1)
        * int(config["state_buffer_per_round_bytes"])
        + action_count * 256
    )


def compute_chunk(
    model: Model,
    battle_id: str,
    chunk_index: int,
    start_state: dict[str, Any] | None,
    delay_ms: int = 0,
) -> dict[str, Any]:
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    engine = BattleEngine(model)
    battle = model.battles[battle_id]
    return engine.simulate_chunk(battle, chunk_index, start_state)


class BudgetScheduler:
    def __init__(
        self,
        model: Model,
        ledger: Ledger,
        *,
        clock: LogicalClock | None = None,
        stop_event: threading.Event | None = None,
        worker: Callable[..., dict[str, Any]] = compute_chunk,
    ) -> None:
        self.model = model
        self.ledger = ledger
        self.clock = clock or ManualLogicalClock()
        self.stop_event = stop_event or threading.Event()
        self.worker = worker
        self.parallelism = int(model.config["parallelism"])
        self.time_budget_ms = float(model.config["logic_time_budget_ms"])
        self.memory_budget_bytes = int(model.config["memory_budget_bytes"])
        self.delay_ms = int(model.config.get("chunk_delay_ms", 0))
        self.admission_sequence: list[dict[str, Any]] = []
        self.round_reports: list[dict[str, Any]] = []
        self._admission_counter = 0

    def _expected_counts(self) -> dict[str, int]:
        return {
            battle.battle_id: battle.chunk_count
            for battle in self.model.battle_list()
        }

    def _ready_coordinates(self) -> list[tuple[str, int]]:
        ready: list[tuple[str, int]] = []
        for battle in self.model.battle_list():
            committed = self.ledger.committed_indices(battle.battle_id)
            for chunk_index in range(battle.chunk_count):
                if chunk_index in committed:
                    continue
                if chunk_index > 0 and chunk_index - 1 not in committed:
                    continue
                ready.append((battle.battle_id, chunk_index))
        return ready

    def _plan_batch(
        self, coordinates: list[tuple[str, int]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        batch: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        used_memory = 0
        for battle_id, chunk_index in coordinates:
            battle = self.model.battles[battle_id]
            buffer_bytes = estimate_chunk_buffer_bytes(model=self.model, battle=battle, chunk_index=chunk_index)
            if buffer_bytes > self.memory_budget_bytes:
                raise BudgetError(
                    f"chunk {battle_id}#{chunk_index} needs {buffer_bytes} bytes, "
                    f"exceeding budget {self.memory_budget_bytes}",
                    code="E_MEMORY_CHUNK_EXCEEDS_BUDGET",
                    details={
                        "battle_id": battle_id,
                        "chunk_index": chunk_index,
                        "required_bytes": buffer_bytes,
                        "budget_bytes": self.memory_budget_bytes,
                    },
                )
            if len(batch) >= self.parallelism:
                deferred.append(
                    {
                        "battle_id": battle_id,
                        "chunk_index": chunk_index,
                        "reason": "parallelism_slots_full",
                    }
                )
                continue
            if used_memory + buffer_bytes > self.memory_budget_bytes:
                deferred.append(
                    {
                        "battle_id": battle_id,
                        "chunk_index": chunk_index,
                        "reason": "memory_budget",
                        "required_bytes": buffer_bytes,
                        "used_bytes": used_memory,
                    }
                )
                continue
            used_memory += buffer_bytes
            predecessor_state = None
            if chunk_index > 0:
                predecessor_state = self.ledger.get_end_state(
                    battle_id, chunk_index - 1
                )
                if predecessor_state is None:
                    deferred.append(
                        {
                            "battle_id": battle_id,
                            "chunk_index": chunk_index,
                            "reason": "predecessor_state_missing",
                        }
                    )
                    continue
            batch.append(
                {
                    "battle_id": battle_id,
                    "chunk_index": chunk_index,
                    "buffer_bytes": buffer_bytes,
                    "start_state": predecessor_state,
                }
            )
        return batch, deferred

    def run_round(self, round_index: int, executor: ThreadPoolExecutor) -> dict[str, Any]:
        round_start_clock = self.clock.now_ms()
        admitted: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        yielded_for_budget = False

        while not self.stop_event.is_set():
            if self.clock.now_ms() - round_start_clock >= self.time_budget_ms:
                yielded_for_budget = True
                break
            coordinates = self._ready_coordinates()
            if not coordinates:
                break
            batch, batch_deferred = self._plan_batch(coordinates)
            deferred.extend(batch_deferred)
            if not batch:
                break

            futures = {}
            for item in batch:
                self._admission_counter += 1
                admission = {
                    "scheduler_round": round_index,
                    "admission_order": self._admission_counter,
                    "battle_id": item["battle_id"],
                    "chunk_index": item["chunk_index"],
                    "buffer_bytes": item["buffer_bytes"],
                    "clock_ms": self.clock.now_ms(),
                }
                self.admission_sequence.append(admission)
                admitted.append(admission)
                future = executor.submit(
                    self.worker,
                    self.model,
                    item["battle_id"],
                    item["chunk_index"],
                    item["start_state"],
                    self.delay_ms,
                )
                futures[(item["battle_id"], item["chunk_index"])] = future

            batch_logic_cost = 0
            for coordinate in sorted(futures):
                result = futures[coordinate].result()
                self.ledger.commit_chunk(result, self.model.input_version_id)
                batch_logic_cost = max(
                    batch_logic_cost, int(result["metrics"]["logic_cost_ms"])
                )

            if isinstance(self.clock, ManualLogicalClock):
                self.clock.advance(batch_logic_cost)

            if self.clock.now_ms() - round_start_clock >= self.time_budget_ms:
                yielded_for_budget = True
                break

        report = {
            "round_index": round_index,
            "started_clock_ms": round_start_clock,
            "elapsed_clock_ms": self.clock.now_ms() - round_start_clock,
            "admitted": [
                {
                    "admission_order": item["admission_order"],
                    "battle_id": item["battle_id"],
                    "chunk_index": item["chunk_index"],
                    "buffer_bytes": item["buffer_bytes"],
                }
                for item in admitted
            ],
            "deferred_count": len(deferred),
            "yielded_for_budget": yielded_for_budget,
            "stop_requested": self.stop_event.is_set(),
        }
        self.round_reports.append(report)
        return report

    def run_until_complete(self) -> dict[str, Any]:
        self.ledger.prune_invalid_chunks()
        expected = self._expected_counts()
        round_index = len(self.round_reports)
        with ThreadPoolExecutor(
            max_workers=self.parallelism, thread_name_prefix="chunk-worker"
        ) as executor:
            while not self.stop_event.is_set():
                overview = self.ledger.completion_overview(expected)
                if overview["all_complete"]:
                    break
                self.run_round(round_index, executor)
                round_index += 1
                if not self.round_reports[-1]["admitted"]:
                    if self.stop_event.is_set():
                        break
                    break

        overview = self.ledger.completion_overview(expected)
        status = "completed" if overview["all_complete"] else "interrupted"
        return {
            "status": status,
            "overview": overview,
            "rounds": self.round_reports,
            "admission_sequence": self.admission_sequence,
            "parallelism": self.parallelism,
            "time_budget_ms": self.time_budget_ms,
            "memory_budget_bytes": self.memory_budget_bytes,
        }
