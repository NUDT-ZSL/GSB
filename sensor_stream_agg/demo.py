from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .aggregator import Aggregator
from .config import AggregatorConfig
from .errors import AggregationError, ErrorCode
from .models import Event, WindowResult


DEMO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = DEMO_ROOT / "demo_output"
WORKER_EXIT_CODE = 91


class Lcg:
    """Small fixed local generator, independent of Python's random algorithm."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def unit(self) -> float:
        self.state = (
            6364136223846793005 * self.state + 1442695040888963407
        ) & 0xFFFFFFFFFFFFFFFF
        return float(self.state >> 11) / float(1 << 53)

    def log_uniform(self, low: float, high: float) -> float:
        return 10.0 ** (low + (high - low) * self.unit())

    def choice(self, values: list[Any]) -> Any:
        return values[int(self.unit() * len(values)) % len(values)]


@dataclass(frozen=True, slots=True)
class SampleSet:
    base: list[Event]
    late_open: Event
    late_closed: Event
    duplicate: Event


def build_samples() -> SampleSet:
    times = [
        1_012_000,
        1_031_000,
        1_066_000,
        1_089_000,
        1_111_000,
        1_137_000,
        1_176_000,
        1_198_000,
        1_211_000,
        1_244_000,
        1_273_000,
        1_292_000,
        1_312_000,
        1_338_000,
        1_367_000,
        1_396_000,
    ]
    generator = Lcg(0x5EED20261115)
    base: list[Event] = []
    for index, timestamp in enumerate(times):
        magnitude = generator.log_uniform(-4.0, 8.0)
        sign = -1.0 if index % 6 in (2, 5) else 1.0
        base.append(
            Event(
                event_time_us=timestamp,
                device_id=1 + index % 3,
                sequence=100 + index,
                observation=sign * magnitude,
                weight=generator.log_uniform(-1.0, 1.0),
            )
        )
    late_open = Event(
        event_time_us=1_360_000,
        device_id=9,
        sequence=900,
        observation=generator.log_uniform(-2.0, 7.0),
        weight=generator.log_uniform(-1.0, 1.0),
    )
    late_closed = Event(
        event_time_us=1_050_000,
        device_id=9,
        sequence=901,
        observation=generator.log_uniform(-2.0, 7.0),
        weight=generator.log_uniform(-1.0, 1.0),
    )
    return SampleSet(base, late_open, late_closed, base[-1])


def result_bits(window: WindowResult) -> tuple[str, ...]:
    return (
        window.sum_value.hex(),
        window.rms_value.hex(),
        window.weighted_mean.hex(),
        window.sum_total.hex(),
        window.sum_compensation.hex(),
        window.sum_absolute.hex(),
        window.weighted_sum_total.hex(),
        window.weighted_sum_compensation.hex(),
        window.weighted_sum_absolute.hex(),
        window.weight_total.hex(),
        window.weight_compensation.hex(),
        window.square_sum_total.hex(),
        window.square_sum_compensation.hex(),
        window.square_sum_absolute.hex(),
    )


def checkpoint_payload(aggregator: Aggregator) -> dict[str, Any]:
    return {
        "summary": aggregator.summary(),
        "closed": [
            {
                "window_id": window.window_id,
                "start_us": window.start_us,
                "end_us": window.end_us,
                "bits": list(result_bits(window)),
            }
            for window in aggregator.list_windows("closed")
        ],
    }


def worker(strategy: str, db_path: Path, checkpoint_path: Path) -> None:
    samples = build_samples()
    config = config_for(strategy)
    aggregator = Aggregator(db_path, config)
    for event in samples.base[:8]:
        result = aggregator.ingest(event)
        if result.code != ErrorCode.OK.value:
            raise RuntimeError(f"worker ingest failed: {result.code}")
    checkpoint_path.write_text(
        json.dumps(checkpoint_payload(aggregator), sort_keys=True),
        encoding="utf-8",
    )
    # Deliberately skip close()/atexit/rollback: simulate a hard process kill.
    os._exit(WORKER_EXIT_CODE)


def config_for(strategy: str) -> AggregatorConfig:
    if strategy == "fixed":
        return AggregatorConfig(
            strategy="fixed",
            window_length_us=100_000,
            allowed_lateness_us=25_000,
        )
    return AggregatorConfig(
        strategy="sliding",
        window_length_us=100_000,
        slide_us=50_000,
        allowed_lateness_us=25_000,
    )


def invoke_worker(strategy: str, db_path: Path, checkpoint_path: Path) -> None:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(DEMO_ROOT) if not existing else str(DEMO_ROOT) + os.pathsep + existing
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "sensor_stream_agg.demo",
            "--worker",
            strategy,
            "--db",
            str(db_path),
            "--checkpoint",
            str(checkpoint_path),
        ],
        cwd=str(DEMO_ROOT),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != WORKER_EXIT_CODE:
        raise RuntimeError(
            f"worker for {strategy} returned {process.returncode}: {process.stderr}"
        )


def fmt(value: float) -> str:
    if value == 0.0:
        return "0"
    return format(value, ".17g")


def print_restore_line(strategy: str, checkpoint: dict[str, Any], aggregator: Aggregator) -> None:
    before = checkpoint["summary"]
    restored = aggregator.summary()
    persisted = {
        window.window_id: result_bits(window)
        for window in aggregator.list_windows("closed")
    }
    unchanged = all(
        persisted.get(int(item["window_id"])) == tuple(item["bits"])
        for item in checkpoint["closed"]
    )
    print(
        f"[{strategy}] restore before_closed={before['windows_closed']} "
        f"after_closed={restored['windows_closed']} "
        f"before_open={before['windows_open']} after_open={restored['windows_open']} "
        f"closed_bits_unchanged={unchanged}"
    )
    if not unchanged:
        raise RuntimeError(f"{strategy}: restored closed result changed")


def print_step(strategy: str, label: str, result: Any) -> None:
    step_text = ",".join(
        f"w{step.window_id}:n={step.member_count},sum={fmt(step.sum_value)},"
        f"sum_bound={fmt(step.sum_error_bound)}"
        for step in result.steps
    )
    dropped = ",".join(f"[{start},{end})" for start, end in result.dropped_windows)
    print(
        f"[{strategy}] {label} code={result.code} status={result.status} "
        f"late={result.late} windows=[{step_text}] dropped=[{dropped}]"
    )


def print_windows(strategy: str, aggregator: Aggregator) -> None:
    closed = aggregator.list_windows("closed")
    for window in closed:
        report = aggregator.error_report(window.window_id)
        print(
            f"[{strategy}] W{window.window_id} [{window.start_us},{window.end_us}) "
            f"n={window.member_count} sum={fmt(window.sum_value)} "
            f"+/-{fmt(window.sum_error_bound)} rms={fmt(window.rms_value)} "
            f"+/-{fmt(window.rms_error_bound)} wmean={fmt(window.weighted_mean)} "
            f"+/-{fmt(window.weighted_mean_error_bound)} "
            f"dev=({fmt(report.sum_deviation)},{fmt(report.rms_deviation)},"
            f"{fmt(report.weighted_mean_deviation)}) code={report.code}"
        )
        if report.code != ErrorCode.OK.value:
            raise RuntimeError(f"error bound failure in window {window.window_id}")
    print(f"[{strategy}] closed_window_count={len(closed)}")


def run_strategy(strategy: str) -> None:
    samples = build_samples()
    db_path = OUTPUT_DIR / f"recovery_{strategy}.sqlite3"
    checkpoint_path = OUTPUT_DIR / f"recovery_{strategy}_checkpoint.json"
    invoke_worker(strategy, db_path, checkpoint_path)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))

    aggregator = Aggregator(db_path, config_for(strategy))
    try:
        print_restore_line(strategy, checkpoint, aggregator)
        for event in samples.base[8:]:
            result = aggregator.ingest(event)
            if result.code != ErrorCode.OK.value:
                raise RuntimeError(f"continuation ingest failed: {result.code}")

        late_open_result = aggregator.ingest(samples.late_open)
        print_step(strategy, "late_open", late_open_result)
        if late_open_result.code != ErrorCode.OK.value or not late_open_result.late:
            raise RuntimeError("late-open event was not merged into an open window")

        duplicate_result = aggregator.ingest(samples.duplicate)
        print_step(strategy, "duplicate", duplicate_result)
        if duplicate_result.code != ErrorCode.E_DUPLICATE.value:
            raise RuntimeError("duplicate key was not rejected")

        dropped_result = aggregator.ingest(samples.late_closed)
        print_step(strategy, "late_closed", dropped_result)
        if dropped_result.code != ErrorCode.E_LATE_DROP.value:
            raise RuntimeError("closed-window late event was not dropped")

        first_closed = aggregator.list_windows("closed")[0]
        transition = aggregator.transition_window(first_closed.window_id, "closed")
        print(
            f"[{strategy}] illegal_transition code={transition.code} "
            f"window=W{first_closed.window_id}"
        )
        if transition.code != ErrorCode.E_WINDOW_STATE.value:
            raise RuntimeError("illegal window transition was accepted")

        flush_result = aggregator.flush()
        print(
            f"[{strategy}] flush watermark={flush_result.watermark_us} "
            f"closed_now={len(flush_result.closed_window_ids)}"
        )
        print_windows(strategy, aggregator)
        summary = aggregator.summary()
        print(
            f"[{strategy}] final_summary events={summary['events_total']} "
            f"accepted={summary['events_accepted']} late={summary['events_late_accepted']} "
            f"partial_late={summary['events_partial_late']} "
            f"late_dropped={summary['events_late_dropped']} "
            f"drop_logs={summary['late_drop_log_entries']} "
            f"windows={summary['windows_total']} closed={summary['windows_closed']} "
            f"open={summary['windows_open']} errbound_failures={summary['error_bound_failures']}"
        )
    finally:
        aggregator.close()


def order_invariance_check() -> None:
    samples = build_samples()
    events = samples.base + [samples.late_open, samples.late_closed]
    orders = [
        list(events),
        list(reversed(events)),
        list(events),
    ]
    random.Random(7719).shuffle(orders[2])
    config = AggregatorConfig(
        strategy="fixed",
        window_length_us=100_000,
        allowed_lateness_us=10**12,
    )
    snapshots: list[dict[tuple[int, int], tuple[str, ...]]] = []
    for index, ordered in enumerate(orders):
        db_path = OUTPUT_DIR / f"order_{index}.sqlite3"
        with Aggregator(db_path, config) as aggregator:
            for event in ordered:
                result = aggregator.ingest(event)
                if result.code != ErrorCode.OK.value:
                    raise RuntimeError(f"order sample rejected: {result.code}")
            aggregator.flush()
            snapshots.append(
                {
                    (window.start_us, window.end_us): result_bits(window)
                    for window in aggregator.list_windows("closed")
                }
            )
    identical = snapshots[0] == snapshots[1] == snapshots[2]
    print(
        f"[order] permutations=3 events={len(events)} windows={len(snapshots[0])} "
        f"bitwise_identical={identical}"
    )
    if not identical:
        raise RuntimeError("arrival-order permutations produced different bits")


def anomaly_checks() -> None:
    try:
        AggregatorConfig(window_length_us=0)
    except AggregationError as exc:
        print(f"[anomaly] invalid_window code={exc.code.value}")

    with Aggregator(
        OUTPUT_DIR / "anomaly_regress.sqlite3",
        AggregatorConfig(
            window_length_us=100,
            allowed_lateness_us=10,
            strict_event_time_order=True,
        ),
    ) as aggregator:
        aggregator.ingest(Event(100, 1, 1, 1.0))
        regressed = aggregator.ingest(Event(99, 1, 2, 2.0))
        print(f"[anomaly] time_regress code={regressed.code}")

    with Aggregator(
        OUTPUT_DIR / "anomaly_conflict.sqlite3",
        AggregatorConfig(
            window_length_us=100,
            allowed_lateness_us=10,
            inject_db_conflict_once=True,
        ),
    ) as aggregator:
        conflict = aggregator.ingest(Event(100, 1, 1, 1.0))
        print(
            f"[anomaly] db_conflict code={conflict.code} "
            f"events_after_rollback={aggregator.summary()['events_total']}"
        )
        retried = aggregator.ingest(Event(100, 1, 1, 1.0))
        print(f"[anomaly] db_conflict_retry code={retried.code}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("fixed", "sliding"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.worker:
        if args.db is None or args.checkpoint is None:
            parser.error("--db and --checkpoint are required with --worker")
        worker(args.worker, args.db, args.checkpoint)
        return 0

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    print("sensor_stream_agg deterministic demo")
    print("strategy semantics: fixed=[start,start+L), sliding starts at multiples of step")
    for strategy in ("fixed", "sliding"):
        run_strategy(strategy)
    order_invariance_check()
    anomaly_checks()
    print("ALL_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

