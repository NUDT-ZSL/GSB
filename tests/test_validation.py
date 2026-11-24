from __future__ import annotations

import copy

import pytest

from haptic_engine.cli import main as cli_main
from haptic_engine.errors import HapticError
from haptic_engine.models import parse_bundle

from .conftest import make_workspace


def _parse(tmp_path, mutate):
    documents = copy.deepcopy(make_workspace(tmp_path, mutate=mutate))
    devices = documents["devices"]
    events = documents["events"]
    config = documents["config"]
    return parse_bundle(tmp_path, devices, events, config)


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda devices, events, config: devices["devices"].append(
                copy.deepcopy(devices["devices"][0])
            ),
            "E_DEVICE",
        ),
        (
            lambda devices, events, config: devices["devices"][0].update(
                sample_rate_hz=0
            ),
            "E_RATE",
        ),
        (
            lambda devices, events, config: devices["devices"][0].update(
                frame_duration_ms=0
            ),
            "E_FRAME",
        ),
        (
            lambda devices, events, config: devices["devices"][0].update(
                max_commands_per_frame=0
            ),
            "E_MAXCMD",
        ),
        (
            lambda devices, events, config: devices["devices"][0].update(
                quantization_levels=1
            ),
            "E_QUANT",
        ),
        (
            lambda devices, events, config: devices["devices"][0].update(
                strength_min=5, strength_max=5
            ),
            "E_RANGE",
        ),
        (
            lambda devices, events, config: events["events"][0].update(start_ms=-1),
            "E_EVENT_START",
        ),
        (
            lambda devices, events, config: events["events"][0].update(duration_ms=0),
            "E_EVENT_DURATION",
        ),
        (
            lambda devices, events, config: events["events"][0]["envelope"][1].update(
                offset_ms=events["events"][0]["envelope"][0]["offset_ms"]
            ),
            "E_ENVELOPE_TIME",
        ),
        (
            lambda devices, events, config: events["events"][0]["envelope"][0].update(
                strength=-1
            ),
            "E_ENVELOPE_RANGE",
        ),
        (
            lambda devices, events, config: events["events"][0].update(
                target_device="missing"
            ),
            "E_TARGET",
        ),
        (
            lambda devices, events, config: events["anchors"][0].update(
                reference_device="missing"
            ),
            "E_ANCHOR",
        ),
    ],
)
def test_validation_business_codes(tmp_path, mutator, code):
    with pytest.raises(HapticError) as raised:
        _parse(tmp_path, mutator)
    assert raised.value.code == code
    assert code.startswith("E_")


def test_invalid_input_writes_no_command_stream(tmp_path, capsys):
    target = tmp_path / "invalid"
    make_workspace(
        target,
        mutate=lambda devices, events, config: events["events"][0].update(start_ms=-5),
    )
    exit_code = cli_main(["--workspace", str(target), "--compact"])
    captured = capsys.readouterr().out
    assert exit_code == 2
    assert "E_EVENT_START" in captured
    assert not (target / "export" / "commands.json").exists()
    assert not (target / "export" / "report.json").exists()


def test_invalid_json_is_reported_without_exports(tmp_path, capsys):
    target = tmp_path / "bad-json"
    make_workspace(target)
    (target / "events.json").write_text("{not json", encoding="utf-8")
    exit_code = cli_main(["--workspace", str(target), "--compact"])
    captured = capsys.readouterr().out
    assert exit_code == 2
    assert "E_JSON" in captured
    assert not (target / "export" / "commands.json").exists()


def test_budget_smaller_than_frame_cap_is_rejected(tmp_path):
    with pytest.raises(HapticError) as raised:
        _parse(
            tmp_path,
            lambda devices, events, config: config.update(
                inflight_command_limit=1
            ),
        )
    assert raised.value.code == "E_BUDGET"
