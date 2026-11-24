from __future__ import annotations

import json
from dataclasses import replace

from formbench.cli import main
from formbench.ledger import Ledger
from formbench.outputs import atomic_write_versioned_json
from formbench.persistence import BlockStore
from formbench.runner import RunOptions, Workbench


def _spec(task, image_id):
    return next(spec for spec in task.images if spec.image_id == image_id)


def test_interrupt_at_block_boundary_and_resume(task, generated_root, tmp_path):
    task_path = generated_root / "samples" / "task.json"
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    common = [
        "--task",
        str(task_path),
        "--state-dir",
        str(state_dir),
        "--output-dir",
        str(output_dir),
    ]

    first_code = main(["run", *common, "--concurrency", "1", "--stop-after-blocks", "3"])
    assert first_code == 130
    marker = state_dir / "INTERRUPTED.json"
    assert marker.exists()
    marker_data = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_data["exit_code"] == 130
    assert marker_data["completed_blocks"] == [0, 1, 2]

    block_json = list((state_dir / "images" / "form-001" / "blocks").glob("block-*.json"))
    assert len(block_json) == 3
    with Ledger(state_dir / "ledger.sqlite3") as ledger:
        assert ledger.count_blocks("form-001", marker_data.get("version_id", "")) in {0, 3}
        rows = ledger.list_blocks("form-001")
        assert len(rows) == 3

    second_code = main(["run", *common])
    assert second_code == 1
    assert not marker.exists()

    result = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    assert result["form_count"] == 3
    form = json.loads((output_dir / "forms" / "form-001.json").read_text(encoding="utf-8"))
    assert form["block_count"] == 11
    assert form["adjudication"]["passed"] is True

    diagnostics = json.loads((output_dir / "diagnostics.json").read_text(encoding="utf-8"))
    form_diag = next(item for item in diagnostics["diagnostics"] if item["image_id"] == "form-001")
    assert form_diag["recovery_mode"] == "version_match_reuse"
    assert form_diag["pending_block_count"] == 8


def test_same_version_rerun_has_no_duplicate_ledger_rows(task, tmp_path):
    spec = _spec(task, "form-003")
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    options = RunOptions(state_dir=state_dir, output_dir=output_dir, concurrency=2)

    with Workbench(task, options=options) as bench:
        bench.process_image(spec)
    with Workbench(task, options=options) as bench:
        outcome = bench.process_image(spec)
        count = bench.ledger.count_blocks(spec.image_id, outcome["result"]["version_id"])

    assert count == 11
    indexes = [block["block_index"] for block in outcome["result"]["blocks"]]
    assert len(indexes) == len(set(indexes)) == 11
    assert outcome["diagnostic"]["pending_block_count"] == 0


def test_version_mismatch_redoes_whole_image(task, tmp_path):
    spec = _spec(task, "form-001")
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    options = RunOptions(state_dir=state_dir, output_dir=output_dir)

    with Workbench(task, options=options) as bench:
        first = bench.process_image(spec)
        first_version = first["result"]["version_id"]

    changed_task = replace(task, detection_threshold=0.21)
    with Workbench(changed_task, options=options) as bench:
        second = bench.process_image(spec)
        rows = bench.ledger.list_blocks(spec.image_id)

    assert second["diagnostic"]["recovery_mode"] == "version_mismatch_redone"
    assert second["result"]["version_id"] != first_version
    assert len(rows) == 11
    assert all(row["version_id"] == second["result"]["version_id"] for row in rows)


def test_tampered_persisted_block_is_recomputed(task, tmp_path):
    spec = _spec(task, "form-002")
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    options = RunOptions(state_dir=state_dir, output_dir=output_dir)

    with Workbench(task, options=options) as bench:
        first = bench.process_image(spec)
        version_id = first["result"]["version_id"]

    block_path = state_dir / "images" / spec.image_id / "blocks" / "block-0004.json"
    record = json.loads(block_path.read_text(encoding="utf-8"))
    record["text"] = "TAMPERED"
    block_path.write_text(json.dumps(record), encoding="utf-8")

    with Workbench(task, options=options) as bench:
        outcome = bench.process_image(spec)

    assert outcome["diagnostic"]["recovery_mode"] == "version_match_reuse"
    assert outcome["diagnostic"]["pending_block_count"] == 1
    block_four = next(block for block in outcome["result"]["blocks"] if block["block_index"] == 4)
    assert block_four["text"] != "TAMPERED"
    assert outcome["result"]["adjudication"]["passed"] is True
    assert version_id == outcome["result"]["version_id"]


def test_temporary_block_files_are_not_completed(task, tmp_path):
    store = BlockStore(tmp_path / "state", "form-x")
    store.blocks_dir.mkdir(parents=True)
    (store.blocks_dir / "block-0001.json.tmp").write_text("partial", encoding="utf-8")
    (store.blocks_dir / "block-0001.normalized.bin.tmp").write_bytes(b"partial")
    assert store.completed_indices("v1") == set()
    store.cleanup_temporary_files()
    assert not list(store.blocks_dir.glob("*.tmp"))


def test_versioned_output_archives_only_different_version(tmp_path):
    path = tmp_path / "results.json"
    first = atomic_write_versioned_json(path, {"value": 1}, version_id="v1")
    same = atomic_write_versioned_json(path, {"value": 2}, version_id="v1")
    second = atomic_write_versioned_json(path, {"value": 3}, version_id="v2")

    assert first["status"] == "replaced"
    assert same["status"] == "unchanged"
    assert second["status"] == "replaced"
    assert json.loads(path.read_text())["value"] == 3
    archives = list((tmp_path / "archive").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["version_id"] == "v1"
