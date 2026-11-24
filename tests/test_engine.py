from __future__ import annotations

import json
import os
import shutil
import signal
import threading
from pathlib import Path

import pytest

from haptic_engine.cli import _SignalState
from haptic_engine.engine import HapticEngine
from haptic_engine.errors import HapticError
from verify_results import verify

from .conftest import load_bundle, make_workspace, write_json


def _run(path: Path, on_block=None, stop_event=None):
    bundle = load_bundle(path)
    return HapticEngine(bundle, on_block=on_block).run(
        stop_event=stop_event
    )


def _commands_bytes(path: Path) -> bytes:
    return (path / "export" / "commands.json").read_bytes()


def _all_block_files(path: Path) -> list[Path]:
    return sorted((path / "state").glob("devices/*/block_*.json"))


def test_parallel_and_budgeted_runs_match_serial(tmp_path):
    serial = tmp_path / "serial"
    parallel = tmp_path / "parallel"
    make_workspace(serial, config_updates={"global_concurrency": 1, "inflight_command_limit": 3})
    make_workspace(parallel, config_updates={"global_concurrency": 4, "inflight_command_limit": 3})
    serial_result = _run(serial)
    parallel_result = _run(parallel)
    assert serial_result.exit_code == 0
    assert parallel_result.exit_code == 0
    assert _commands_bytes(serial) == _commands_bytes(parallel)
    assert serial_result.report["scheduling"]["peak"] <= 3
    assert parallel_result.report["scheduling"]["peak"] <= 3
    assert parallel_result.report["totals"]["commands"] > 0
    for path in (serial, parallel):
        for block_file in _all_block_files(path):
            block = json.loads(block_file.read_text(encoding="utf-8"))
            device = load_bundle(path).device_by_id[block["device_id"]]
            assert len(block["commands"]) <= device.max_commands_per_frame
            assert len(block["commands"]) == block["command_count"]


def test_reusing_state_does_not_change_commands(workspace):
    first = _run(workspace)
    first_bytes = _commands_bytes(workspace)
    second = _run(workspace)
    assert second.report["state"]["reused_blocks"] == first.report["totals"]["frame_blocks"]
    assert second.report["state"]["computed_blocks"] == 0
    assert _commands_bytes(workspace) == first_bytes


def test_graceful_interrupt_resumes_to_identical_stream(tmp_path):
    complete = tmp_path / "complete"
    interrupted = tmp_path / "interrupted"
    make_workspace(complete, config_updates={"global_concurrency": 3, "inflight_command_limit": 3})
    make_workspace(interrupted, config_updates={"global_concurrency": 3, "inflight_command_limit": 3})

    full = _run(complete)
    stop_event = threading.Event()
    count = {"blocks": 0}

    def on_block(_device_id, _block_index):
        count["blocks"] += 1
        if count["blocks"] >= 5:
            stop_event.set()

    partial = _run(interrupted, on_block=on_block, stop_event=stop_event)
    assert partial.status == "interrupted"
    assert partial.exit_code == 130
    assert partial.interrupt_path is not None and partial.interrupt_path.exists()
    assert not (interrupted / "export" / "commands.json").exists()
    assert len(_all_block_files(interrupted)) < full.report["totals"]["frame_blocks"]

    resume = _run(interrupted)
    assert resume.exit_code == 0
    assert resume.report["state"]["reused_blocks"] > 0
    assert resume.report["digest"]["missing_recomputed"] > 0
    assert "E_DIGEST" in resume.report["business_codes"]
    assert not (interrupted / "state" / "interrupted.json").exists()
    assert _commands_bytes(interrupted) == _commands_bytes(complete)


def test_partial_state_recovers_without_interrupt_marker(tmp_path):
    complete = tmp_path / "complete"
    interrupted = tmp_path / "interrupted"
    forced = tmp_path / "forced"
    make_workspace(complete)
    make_workspace(interrupted)
    make_workspace(forced)

    _run(complete)
    stop_event = threading.Event()

    def on_block(_device_id, _block_index):
        if len(_all_block_files(interrupted)) >= 4:
            stop_event.set()

    _run(interrupted, on_block=on_block, stop_event=stop_event)
    shutil.copytree(interrupted / "state", forced / "state")
    (forced / "state" / "interrupted.json").unlink(missing_ok=True)

    forced_result = _run(forced)
    assert forced_result.exit_code == 0
    assert forced_result.report["state"]["reused_blocks"] > 0
    assert _commands_bytes(forced) == _commands_bytes(complete)


def test_digest_corruption_is_repaired_and_reported(workspace):
    initial = _run(workspace)
    baseline = _commands_bytes(workspace)
    first_block = _all_block_files(workspace)[0]
    corrupted = json.loads(first_block.read_text(encoding="utf-8"))
    corrupted["command_count"] += 1
    write_json(first_block, corrupted)

    repaired = _run(workspace)
    assert repaired.exit_code == 0
    assert "E_DIGEST" in repaired.report["business_codes"]
    assert repaired.report["digest"]["issues"]
    assert _commands_bytes(workspace) == baseline
    assert repaired.report["totals"] == initial.report["totals"]


def test_missing_block_is_recomputed_and_reported(workspace):
    _run(workspace)
    baseline = _commands_bytes(workspace)
    deleted = _all_block_files(workspace)[0]
    deleted.unlink()
    result = _run(workspace)
    assert result.exit_code == 0
    assert result.report["digest"]["missing_recomputed"] == 1
    assert "E_DIGEST" in result.report["business_codes"]
    assert _commands_bytes(workspace) == baseline


def test_version_change_archives_state_and_export(tmp_path):
    target = tmp_path / "versioned"
    make_workspace(target)
    first = _run(target)
    old_version = first.report["version_id"]
    events_path = target / "events.json"
    events_doc = json.loads(events_path.read_text(encoding="utf-8"))
    events_doc["events"][0]["type"] = "impact_revision_2"
    write_json(events_path, events_doc)
    second = _run(target)
    assert second.report["version_id"] != old_version
    assert second.report["state"]["reused_blocks"] == 0
    assert second.report["state"]["archived_state"]
    assert second.report["state"]["archived_export"]
    assert Path(second.report["state"]["archived_state"]).exists()
    assert Path(second.report["state"]["archived_export"]).exists()


def test_tampered_version_metadata_archives_state(workspace):
    _run(workspace)
    version_path = workspace / "state" / "_version.json"
    version = json.loads(version_path.read_text(encoding="utf-8"))
    version["manifest_digest"] = "0" * 64
    write_json(version_path, version)
    result = _run(workspace)
    assert result.report["state"]["reused_state"] is False
    assert result.report["state"]["archived_state"]
    assert result.report["state"]["computed_blocks"] == result.report["totals"]["frame_blocks"]


def test_sync_tolerance_violation_returns_e_sync_without_export(tmp_path):
    target = tmp_path / "sync-error"

    def mutate(_devices, events, _config):
        for event in events["events"]:
            if event["target_device"] == "dev_glove_right" and event["anchor"] == "sync_0":
                event["start_ms"] += 5
                break

    make_workspace(target, mutate=mutate)
    with pytest.raises(HapticError) as raised:
        _run(target)
    assert raised.value.code == "E_SYNC"
    assert raised.value.details["anchor_id"] == "anchor_0"
    assert raised.value.details["device_id"] == "dev_glove_right"
    assert raised.value.details["delta_ms"] > raised.value.details["tolerance_ms"]
    assert not (target / "export" / "commands.json").exists()


def test_report_contains_required_accounting(workspace):
    result = _run(workspace)
    report = result.report
    assert report["business_codes"] == []
    assert {item["device_id"] for item in report["devices"]} == {
        "dev_glove_left",
        "dev_glove_right",
        "dev_vest_core",
        "dev_wrist_band",
    }
    assert len(report["anchor_diagnostics"]) == 12
    assert all(item["sample_delta"] == 0 for item in report["anchor_diagnostics"])
    assert all(item["delta_ms"] == 0 for item in report["anchor_diagnostics"])
    assert report["totals"]["splits"] > 0
    assert report["scheduling"]["peak"] <= report["scheduling"]["limit"]
    verified = verify(workspace)
    assert verified["ok"] is True
    assert verified["totals"]["frame_blocks"] == report["totals"]["frame_blocks"]
    assert verified["totals"]["commands"] == report["totals"]["commands"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal delivery test")
def test_os_signal_is_deferred_to_block_boundary_and_exits_130(tmp_path):
    target = tmp_path / "signal"
    make_workspace(target)
    bundle = load_bundle(target)
    fired = {"value": False}
    lock = threading.Lock()

    def on_block(_device_id, _block_index):
        with lock:
            should_fire = not fired["value"]
            fired["value"] = True
        if should_fire:
            os.kill(os.getpid(), signal.SIGINT)

    with _SignalState() as signal_state:
        result = HapticEngine(bundle, on_block=on_block).run(
            stop_event=signal_state.stop_event,
            signal_info=signal_state.info,
        )
    assert result.status == "interrupted"
    assert result.exit_code == 130
    marker = json.loads(result.interrupt_path.read_text(encoding="utf-8"))
    assert marker["signal"] == signal.SIGINT
    assert marker["completed_blocks"]
    assert not (target / "export" / "commands.json").exists()


def test_sigterm_maps_to_exit_143(tmp_path):
    target = tmp_path / "sigterm"
    make_workspace(target)
    stop_event = threading.Event()
    stop_event.set()
    result = HapticEngine(load_bundle(target)).run(
        stop_event=stop_event,
        signal_info={"signal": 15},
    )
    assert result.status == "interrupted"
    assert result.exit_code == 143
    marker = json.loads(result.interrupt_path.read_text(encoding="utf-8"))
    assert marker["signal"] == 15
    assert marker["exit_code"] == 143
