from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ic_ledger.errors import BusinessError, ErrorCode
from ic_ledger.validation import load_document, parse_document

from conftest import SAMPLES, remove_db, run_cli, run_cli_json


def _sample() -> dict:
    return json.loads((SAMPLES / "sample.json").read_text(encoding="utf-8"))


def test_valid_sample_parse(sample_path: Path) -> None:
    document = load_document(sample_path)
    assert len(document.queues) == 3
    assert len(document.flows) == 75
    assert document.config.fixed_scale == 1000


@pytest.mark.parametrize(
    ("filename", "code"),
    [
        ("invalid_duplicate_queue.json", ErrorCode.QUEUE_DUPLICATE),
        ("invalid_weight.json", ErrorCode.QUEUE_WEIGHT),
        ("invalid_max_irq_rate.json", ErrorCode.MAX_IRQ_RATE),
        ("invalid_rx_usecs.json", ErrorCode.RX_USECS),
        ("invalid_rx_frames.json", ErrorCode.RX_FRAMES),
        ("invalid_record_seq.json", ErrorCode.RECORD_SEQ),
        ("invalid_arrival_frames.json", ErrorCode.ARRIVAL_FRAMES),
        ("invalid_frame_bytes.json", ErrorCode.FRAME_BYTES),
        ("invalid_unknown_queue.json", ErrorCode.RECORD_QUEUE),
    ],
)
def test_invalid_samples_have_business_codes(
    filename: str, code: str, tmp_path: Path
) -> None:
    db = tmp_path / "invalid.sqlite"
    result = run_cli(
        ["load", str(SAMPLES / filename), "--db", str(db), "--compact"]
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 2
    assert payload["error"]["code"] == code
    assert not db.exists()


def test_invalid_load_does_not_modify_existing_database(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    remove_db(db)
    code, _ = run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    assert code == 0
    _, before = run_cli_json(["status", "--db", str(db)])
    code, payload = run_cli_json(
        ["load", str(SAMPLES / "invalid_weight.json"), "--db", str(db)]
    )
    assert code == 2
    assert payload["error"]["code"] == ErrorCode.QUEUE_WEIGHT
    _, after = run_cli_json(["status", "--db", str(db)])
    assert before == after


def test_external_and_nested_parameters_must_agree() -> None:
    raw = _sample()
    queue = raw["interfaces"][0]["queues"][0]
    raw["coalescing"] = [
        {
            "iface": "eth0",
            "queue": 0,
            **copy.deepcopy(queue["coalesce"]),
        }
    ]
    document = parse_document(raw)
    assert document.queues[0].params.rx_frames == 7
    raw["coalescing"][0]["rx_usecs"] += 1
    with pytest.raises(BusinessError) as caught:
        parse_document(raw)
    assert caught.value.code == ErrorCode.PARAM_DUPLICATE_CONFLICT


def test_clock_cannot_move_backwards_per_queue() -> None:
    raw = _sample()
    raw["traffic"][0]["logical_clock_us"] = 10**9
    with pytest.raises(BusinessError) as caught:
        parse_document(raw)
    assert caught.value.code == ErrorCode.RECORD_CLOCK


def test_boolean_is_not_an_integer() -> None:
    raw = _sample()
    raw["interfaces"][0]["queues"][0]["weight"] = True
    with pytest.raises(BusinessError) as caught:
        parse_document(raw)
    assert caught.value.code == ErrorCode.QUEUE_WEIGHT


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda raw: raw["interfaces"][0]["queues"][0]["coalesce"].__setitem__(
                "tx_usecs", -1
            ),
            ErrorCode.TX_USECS,
        ),
        (
            lambda raw: raw["interfaces"][0]["queues"][0]["coalesce"].__setitem__(
                "tx_frames", 1.0
            ),
            ErrorCode.TX_FRAMES,
        ),
        (
            lambda raw: raw["interfaces"][0]["queues"][0].__setitem__(
                "max_irq_rate", 1.5
            ),
            ErrorCode.MAX_IRQ_RATE,
        ),
        (
            lambda raw: raw["config"].__setitem__("fixed_scale", 0),
            ErrorCode.CONFIG_FIXED_SCALE,
        ),
        (
            lambda raw: raw["config"].__setitem__("avg_frame_interval_us", 0),
            ErrorCode.CONFIG_FRAME_INTERVAL,
        ),
        (
            lambda raw: raw["config"].__setitem__("float_tolerance", -1),
            ErrorCode.CONFIG_TOLERANCE,
        ),
        (
            lambda raw: raw["config"].__setitem__(
                "irq_budget_per_second", {"eth0:0": 0}
            ),
            ErrorCode.CONFIG_IRQ_BUDGET,
        ),
        (
            lambda raw: raw["config"].__setitem__("clock_step_us", 0),
            ErrorCode.CONFIG_CLOCK_STEP,
        ),
        (
            lambda raw: raw["traffic"][0].__setitem__("direction", "sideways"),
            ErrorCode.RECORD_DIRECTION,
        ),
    ],
)
def test_additional_field_error_codes(mutate, code: str) -> None:
    raw = _sample()
    mutate(raw)
    with pytest.raises(BusinessError) as caught:
        parse_document(raw)
    assert caught.value.code == code


def test_duplicate_interface_is_rejected() -> None:
    raw = _sample()
    raw["interfaces"].append(copy.deepcopy(raw["interfaces"][0]))
    with pytest.raises(BusinessError) as caught:
        parse_document(raw)
    assert caught.value.code == ErrorCode.IFACE_DUPLICATE

