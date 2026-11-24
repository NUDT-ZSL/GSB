#!/usr/bin/env python3
"""本地验收：正常流程、并发一致性、中断续跑、摘要回退和 E_REPLAY 定位。"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multiscreen.canon import canonical  # noqa: E402
from multiscreen.generator import generate_sample  # noqa: E402
from multiscreen.pipeline import prepare_data, run_pipeline  # noqa: E402
from multiscreen.replay import run_replay  # noqa: E402
from multiscreen.validate import load_json  # noqa: E402
from multiscreen.verify import build_manifest_initial_states, verify_replay  # noqa: E402


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def run_test_suite() -> dict[str, Any]:
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stderr).run(suite)
    return {
        "run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "successful": result.wasSuccessful(),
    }


def make_replay_mismatch_sample(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    shard_path = target / "snapshots" / "v2" / "shard_000.json"
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    shard["records"][0]["adoption_cursor_seq"] = 999
    shard_path.write_text(json.dumps(shard, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    db_path = target / "snapshots" / "v2" / "meta.sqlite3"
    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE receiver_metadata SET adoption_cursor_seq=999 WHERE canonical_receiver_id=?",
            ("rcv-side",),
        )
        connection.commit()
    finally:
        connection.close()

    digest_path = target / "snapshots" / "digests.json"
    digest_manifest = json.loads(digest_path.read_text(encoding="utf-8"))
    from multiscreen.canon import sha256_file

    for set_entry in digest_manifest["snapshot_sets"]:
        if set_entry["set_id"] != "v2-current":
            continue
        for file_entry in set_entry["files"]:
            file_entry["digest"] = sha256_file(target / "snapshots" / file_entry["path"])
    digest_path.write_text(json.dumps(digest_manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def main() -> int:
    sample_dir = ROOT / "task"
    state_dir = ROOT / "state"
    interrupt_dir = ROOT / "state-interrupt"
    fixture_root = ROOT / "fixtures"
    tampered_dir = fixture_root / "samples-tampered"
    mismatch_dir = fixture_root / "samples-replay-mismatch"
    invalid_dir = fixture_root / "samples-invalid"
    output_dir = ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    tampered_state_dir = ROOT / "state-tampered"
    for directory in (state_dir, interrupt_dir, tampered_state_dir, fixture_root):
        if directory.exists():
            shutil.rmtree(directory)
    fixture_root.mkdir(parents=True, exist_ok=True)

    generation = generate_sample(sample_dir)
    normal = run_pipeline(sample_dir, state_dir, workers=4, chunk_count=4)
    write_json(output_dir / "report.json", normal)

    prep = prepare_data(sample_dir, workers=4)
    initial = build_manifest_initial_states(prep["manifest"])
    serial = run_replay(prep["events"], initial, workers=1, auto_switch_on_breach=True)
    concurrent = run_replay(prep["events"], initial, workers=4, budget_bytes=8 * 1024 * 1024,
                            auto_switch_on_breach=True)
    concurrency_same = canonical(serial["results"]) == canonical(concurrent["results"])

    interrupted = run_pipeline(
        sample_dir, interrupt_dir, workers=2, chunk_count=4, interrupt_after_chunks=2
    )
    resumed = run_pipeline(sample_dir, interrupt_dir, workers=2, chunk_count=4)
    write_json(output_dir / "report_interrupt_resume.json", {
        "interrupted": {k: v for k, v in interrupted.items() if k != "recovery"},
        "resumed_ok": resumed["ok"],
        "persistence": resumed["persistence"],
    })

    if tampered_dir.exists():
        shutil.rmtree(tampered_dir)
    shutil.copytree(sample_dir, tampered_dir)
    shard = tampered_dir / "snapshots" / "v2" / "shard_000.json"
    shard.write_bytes(shard.read_bytes() + b" ")
    digest_fallback = run_pipeline(tampered_dir, tampered_state_dir, workers=2, chunk_count=3)
    write_json(output_dir / "report_digest_fallback.json", {
        "ok": digest_fallback["ok"],
        "fallback": digest_fallback["merge"]["fallback"],
        "rejected_snapshots": digest_fallback["merge"]["rejected_snapshots"],
        "summary": digest_fallback["verification"]["summary"],
    })

    make_replay_mismatch_sample(sample_dir, mismatch_dir)
    mismatch_prep = prepare_data(mismatch_dir)
    mismatch_report = verify_replay(
        mismatch_prep["view"], mismatch_prep["events"], mismatch_prep["manifest"], mismatch_prep["records"]
    )
    write_json(output_dir / "report_replay_mismatch.json", mismatch_report)

    invalid_payload = None
    bad_data = invalid_dir
    shutil.copytree(sample_dir, bad_data)
    manifest_path = bad_data / "sessions.json"
    bad_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad_manifest["sessions"][0]["geometry"]["scale"] = 0
    manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
    try:
        prepare_data(bad_data)
    except Exception as exc:  # noqa: BLE001
        invalid_payload = getattr(exc, "to_dict", lambda: {"error": {"message": str(exc)}})()
    write_json(output_dir / "error_invalid_input.json", invalid_payload)

    tests = run_test_suite()
    acceptance = {
        "ok": (
            normal["ok"]
            and concurrency_same
            and interrupted["interrupted"]
            and resumed["ok"]
            and digest_fallback["ok"]
            and not mismatch_report["ok"]
            and mismatch_report["code"] == "E_REPLAY"
            and invalid_payload["error"]["code"] == "E_GEOMETRY"
            and tests["successful"]
        ),
        "seed_generation": generation,
        "normal": {
            "ok": normal["ok"],
            "summary": normal["verification"]["summary"],
            "stats": normal["verification"]["stats"],
            "conflicts": len(normal["merge"]["conflicts"]),
            "chunks": normal["persistence"]["chunks_recovered"],
        },
        "concurrency": {"workers_1_vs_4_identical": concurrency_same},
        "interrupt_resume": {
            "interrupt_exit_code": interrupted["exit_code"],
            "interrupted_after": interrupted["chunks_written"],
            "resumed_ok": resumed["ok"],
            "resumed_from_chunk": resumed["persistence"]["resumed_from_chunk"],
            "chunks_recovered": resumed["persistence"]["chunks_recovered"],
        },
        "digest_fallback": {
            "ok": digest_fallback["ok"],
            "fallback": digest_fallback["merge"]["fallback"],
            "rejected_count": len(digest_fallback["merge"]["rejected_snapshots"]),
        },
        "replay_mismatch": {
            "ok": mismatch_report["ok"],
            "code": mismatch_report["code"],
            "first_divergence": mismatch_report["first_divergence"],
        },
        "invalid_input": invalid_payload,
        "tests": tests,
    }
    write_json(output_dir / "acceptance_report.json", acceptance)
    print(json.dumps(acceptance, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if acceptance["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
