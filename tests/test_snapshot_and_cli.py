from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coref_pipeline.canonical import pretty_json
from coref_pipeline.cli import main
from coref_pipeline.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_TYPE_INVALID,
    PipelineError,
)
from coref_pipeline.snapshot import SnapshotStore

from .conftest import write_events


def test_snapshot_version_rollback_and_corruption(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.commit(
        kind="evidence_weights",
        payload={"version": 1, "entity": 0.4},
        version=1,
    )
    second = store.commit(
        kind="evidence_weights",
        payload={"version": 2, "entity": 0.5},
        version=2,
    )
    assert first["file"] != second["file"]
    assert store.load_latest("evidence_weights")["version"] == 2

    with pytest.raises(PipelineError) as exc:
        store.commit(
            kind="evidence_weights",
            payload={"version": 1, "entity": 0.4},
            version=1,
        )
    assert exc.value.code == E_SNAPSHOT_ROLLBACK

    with pytest.raises(PipelineError) as exc:
        store.commit(
            kind="evidence_weights",
            payload={"version": 2, "entity": 0.55},
            version=2,
        )
    assert exc.value.code == E_SNAPSHOT_CORRUPT

    latest = store.load_latest("evidence_weights")
    path = store.root / latest["file"]
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    wrapper["payload"]["entity"] = 0.99
    path.write_text(pretty_json(wrapper), encoding="utf-8")
    with pytest.raises(PipelineError) as exc:
        store.load_latest("evidence_weights")
    assert exc.value.code == E_SNAPSHOT_CORRUPT


def test_stage_busy_is_immediate(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    with store.stage_lock("holder"):
        with pytest.raises(PipelineError) as exc:
            store.commit(
                kind="time_equivalence",
                payload={"version": 1, "groups": []},
                version=1,
            )
    assert exc.value.code == E_STAGE_BUSY


def test_pipeline_snapshot_integration(sample_dir, tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    first = main(
        [
            "run",
            "--sample-dir",
            str(sample_dir),
            "--output",
            str(tmp_path / "first.json"),
            "--snapshot-dir",
            str(snapshot_dir),
        ]
    )
    assert first == 0
    report = json.loads((tmp_path / "first.json").read_text(encoding="utf-8"))
    assert report["snapshots"]["committed"]["chain_state"]["version"] == 1
    assert report["snapshots"]["committed"]["evidence_weights"]["version"] == 1
    assert report["snapshots"]["committed"]["time_equivalence"]["version"] == 1

    second = main(
        [
            "run",
            "--sample-dir",
            str(sample_dir),
            "--output",
            str(tmp_path / "second.json"),
            "--snapshot-dir",
            str(snapshot_dir),
        ]
    )
    assert second == 0
    second_report = json.loads(
        (tmp_path / "second.json").read_text(encoding="utf-8")
    )
    assert second_report["snapshots"]["committed"]["chain_state"]["version"] == 1
    assert (
        (tmp_path / "first.json").read_bytes()
        == (tmp_path / "second.json").read_bytes()
    )


def test_chain_snapshot_whole_replacement_on_change(
    sample_dir, sample_events, tmp_path
):
    snapshot_dir = tmp_path / "snapshots"
    assert (
        main(
            [
                "run",
                "--sample-dir",
                str(sample_dir),
                "--output",
                str(tmp_path / "before.json"),
                "--snapshot-dir",
                str(snapshot_dir),
            ]
        )
        == 0
    )
    events = copy.deepcopy(sample_events)
    for event in events:
        if event["event_id"] == "ev-0003":
            event["source_trust"] = 0.51
    write_events(sample_dir / "events.jsonl", events)
    assert (
        main(
            [
                "run",
                "--sample-dir",
                str(sample_dir),
                "--output",
                str(tmp_path / "after.json"),
                "--snapshot-dir",
                str(snapshot_dir),
            ]
        )
        == 0
    )
    store = SnapshotStore(snapshot_dir)
    latest = store.load_latest("chain_state")
    assert latest["version"] == 2
    files = sorted((snapshot_dir / "chain_state").glob("v*.json"))
    assert len(files) == 2
    assert files[0].name != files[1].name
    assert files[0].read_bytes() != files[1].read_bytes()


def test_cli_writes_attributed_error_report(
    sample_dir, sample_events, tmp_path
):
    events = copy.deepcopy(sample_events)
    events[0]["event_type"] = "BAD"
    write_events(sample_dir / "events.jsonl", events)
    output = tmp_path / "error.json"
    result = main(
        ["run", "--sample-dir", str(sample_dir), "--output", str(output)]
    )
    assert result == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    error = report["errors"][0]
    assert error["code"] == E_TYPE_INVALID
    assert error["event_id"] == events[0]["event_id"]
    assert error["field"] == "event_type"
    assert output.with_name("error.json.sha256").is_file()


def test_cli_verify_command(sample_dir, tmp_path, capsys):
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "run",
                "--sample-dir",
                str(sample_dir),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    verification = tmp_path / "verification.json"
    assert (
        main(
            [
                "verify",
                "--report",
                str(output),
                "--config",
                str(sample_dir / "coref_rules.json"),
                "--output",
                str(verification),
            ]
        )
        == 0
    )
    payload = json.loads(verification.read_text(encoding="utf-8"))
    assert payload["ok"]
