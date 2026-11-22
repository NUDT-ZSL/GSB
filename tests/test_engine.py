from __future__ import annotations

from fractions import Fraction

from ic_ledger.engine import initial_runtime, step_record
from ic_ledger.validation import load_document, parse_document

from conftest import SAMPLES


def _document(records: list[dict], *, rx_usecs: int, rx_frames: int, scale: int = 1000):
    raw = {
        "config": {
            "avg_frame_interval_us": "1.5",
            "clock_step_us": 10,
            "error_bound_budget_us": "100",
            "fixed_scale": scale,
            "float_tolerance": "1e-12",
            "irq_budget_per_second": {"eth0:0": 1000000},
        },
        "interfaces": [
            {
                "name": "eth0",
                "queues": [
                    {
                        "coalesce": {
                            "rx_frames": rx_frames,
                            "rx_usecs": rx_usecs,
                            "tx_frames": 0,
                            "tx_usecs": 0,
                        },
                        "max_irq_rate": 1000000,
                        "queue": 0,
                        "weight": 1,
                    }
                ],
            }
        ],
        "traffic": records,
    }
    return parse_document(raw)


def test_both_triggers_are_deduplicated() -> None:
    records = [
        {
            "arrival_frames": 1,
            "frame_bytes": 100,
            "iface": "eth0",
            "logical_clock_us": clock,
            "queue": 0,
            "record_seq": index + 1,
        }
        for index, clock in enumerate([0, 10, 20])
    ]
    document = _document(records, rx_usecs=20, rx_frames=3)
    spec = document.queues[0]
    runtime = initial_runtime("eth0", 0, "rx", 1)
    rows = []
    for record in document.flows:
        result = step_record(
            runtime,
            record,
            spec,
            document.config,
            param_version=1,
        )
        runtime = result.runtime
        rows.append(result.ledger)
    assert rows[-1].fixed_trigger == "both"
    assert rows[-1].fixed_irq_delta == 1
    assert runtime.fixed.interrupts == 1
    assert runtime.fixed.frames == 0
    assert rows[-1].fixed_irq_rate == Fraction(50_000)
    assert rows[-1].fixed_throughput_bps == Fraction(120_000_000)


def test_trigger_resets_both_paths_and_records_source() -> None:
    records = [
        {
            "arrival_frames": 2,
            "frame_bytes": 100,
            "iface": "eth0",
            "logical_clock_us": index * 5,
            "queue": 0,
            "record_seq": index + 1,
        }
        for index in range(4)
    ]
    document = _document(records, rx_usecs=100, rx_frames=3)
    spec = document.queues[0]
    runtime = initial_runtime("eth0", 0, "rx", 1)
    seen = []
    for record in document.flows:
        result = step_record(
            runtime, record, spec, document.config, param_version=1
        )
        runtime = result.runtime
        seen.append(result.ledger.fixed_trigger)
    assert seen == ["none", "frames", "none", "frames"]
    assert runtime.fixed.interrupts == runtime.float_state.interrupts == 2


def test_tx_direction_uses_tx_parameters() -> None:
    records = [
        {
            "arrival_frames": 1,
            "direction": "tx",
            "frame_bytes": 100,
            "iface": "eth0",
            "logical_clock_us": index,
            "queue": 0,
            "record_seq": index + 1,
        }
        for index in range(2)
    ]
    document = _document(records, rx_usecs=0, rx_frames=0)
    spec = document.queues[0]
    runtime = initial_runtime("eth0", 0, "tx", 1)
    trigger = None
    for record in document.flows:
        result = step_record(
            runtime, record, spec, document.config, param_version=1
        )
        runtime = result.runtime
        trigger = result.ledger.fixed_trigger
    assert trigger == "immediate"
    assert runtime.fixed.interrupts == 2


def test_sample_records_first_divergence_with_metric_and_residue() -> None:
    document = load_document(SAMPLES / "sample.json")
    spec = next(item for item in document.queues if item.key == "eth0:1")
    runtime = initial_runtime(spec.iface, spec.queue, "rx", 2)
    first = None
    for record in sorted(
        (
            item
            for item in document.flows
            if item.iface == spec.iface
            and item.queue == spec.queue
            and item.direction == "rx"
        ),
        key=lambda item: item.record_seq,
    ):
        result = step_record(
            runtime, record, spec, document.config, param_version=2
        )
        runtime = result.runtime
        if result.ledger.diverged and first is None:
            first = result.ledger
    assert first is not None
    assert first.first_divergence_record == 1060
    assert first.first_divergence_metric == "window_occupancy"
    assert first.residue_after != 0
    assert first.fixed_window_occupancy != 0

