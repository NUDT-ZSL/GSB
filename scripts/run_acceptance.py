from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from graphstore.checksums import canonical_json, sha256_object


def run_cli(
    *args: str,
    expect_success: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(ROOT)
    if env:
        process_env.update(env)
    process = subprocess.run(
        [sys.executable, "-m", "graphstore.cli", *args],
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if expect_success and process.returncode != 0:
        raise RuntimeError(process.stderr or process.stdout)
    payload = process.stdout.strip() or process.stderr.strip()
    if not payload:
        return process.returncode, {}
    return process.returncode, json.loads(payload)


def relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()

    run_dir = ROOT / "data" / "acceptance" / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    baseline_db = run_dir / "baseline.sqlite3"
    crash_db = run_dir / "crash.sqlite3"
    replace_db = run_dir / "replace-crash.sqlite3"
    common = ["--clock", "samples/clock.json"]
    try:
        _, loaded = run_cli(
            "--db",
            relative(baseline_db),
            *common,
            "load",
            "--package",
            "samples/packages/base/manifest.json",
        )
        assert loaded["result"]["classification"] == "initial_load"
        assert loaded["result"]["node_count"] == 9
        assert loaded["result"]["edge_count"] == 9
        assert loaded["result"]["tombstone_count"] == 2

        _, merge_preview = run_cli(
            "--db",
            relative(baseline_db),
            *common,
            "merge",
            "--package",
            "samples/packages/append/manifest.json",
        )
        assert (
            merge_preview["decision"]["classification"] == "incremental_append"
        )

        traverse_args = (
            "traverse",
            "--batch-id",
            "acceptance-two-hop",
            "--kind",
            "reachable",
            "--node-id",
            "n1",
            "--depth",
            "2",
        )
        _, baseline = run_cli(
            "--db", relative(baseline_db), *common, *traverse_args
        )
        assert baseline["stats"] == {
            "emitted": 3,
            "truncated": 2,
            "visited": 4,
        }
        assert [item["node_id"] for item in baseline["results"]] == [
            "n2",
            "n7",
            "n4",
        ]

        run_cli(
            "--db",
            relative(crash_db),
            *common,
            "load",
            "--package",
            "samples/packages/base/manifest.json",
        )
        crash_code, _ = run_cli(
            "--db",
            relative(crash_db),
            *common,
            *traverse_args,
            expect_success=False,
            env={
                "GRAPHSTORE_ENABLE_TEST_FAILPOINTS": "1",
                "GRAPHSTORE_CRASH_AT": "traverse_after_layer_1",
            },
        )
        assert crash_code != 0
        with sqlite3.connect(crash_db) as connection:
            status, next_depth = connection.execute(
                """
                SELECT status, next_depth FROM traverse_batches
                WHERE batch_id = 'acceptance-two-hop'
                """
            ).fetchone()
        assert (status, next_depth) == ("running", 1)
        _, resumed = run_cli(
            "--db",
            relative(crash_db),
            *common,
            "resume",
            "--batch-id",
            "acceptance-two-hop",
        )
        assert resumed == baseline
        _, crash_status = run_cli(
            "--db", relative(crash_db), *common, "status"
        )
        assert crash_status["committed_traversals"]["batch_count"] == 1
        _, crash_verify = run_cli(
            "--db",
            relative(crash_db),
            *common,
            "verify",
            "--batch-id",
            "acceptance-two-hop",
        )
        assert crash_verify["ok"] is True

        _, rollback_error = run_cli(
            "--db",
            relative(baseline_db),
            *common,
            "replace",
            "--package",
            "samples/packages/rollback/manifest.json",
            expect_success=False,
        )
        assert rollback_error["error"]["code"] == "GS_VERSION_ROLLBACK"
        _, hash_error = run_cli(
            "--db",
            relative(baseline_db),
            *common,
            "replace",
            "--package",
            "samples/packages/partial/manifest.json",
            expect_success=False,
        )
        assert hash_error["error"]["code"] == "GS_SHARD_HASH_MISMATCH"

        run_cli(
            "--db",
            relative(replace_db),
            *common,
            "load",
            "--package",
            "samples/packages/base/manifest.json",
        )
        replace_crash_code, _ = run_cli(
            "--db",
            relative(replace_db),
            *common,
            "replace",
            "--package",
            "samples/packages/append/manifest.json",
            expect_success=False,
            env={
                "GRAPHSTORE_ENABLE_TEST_FAILPOINTS": "1",
                "GRAPHSTORE_CRASH_AT": "replace_after_stage_nodes",
            },
        )
        assert replace_crash_code != 0
        _, replace_status = run_cli(
            "--db", relative(replace_db), *common, "status"
        )
        assert replace_status["current_index"]["index_version"] == 1
        assert len(replace_status["pending_builds"]) == 1
        _, replace_resumed = run_cli(
            "--db",
            relative(replace_db),
            *common,
            "resume",
            "--package",
            "samples/packages/append/manifest.json",
        )
        assert replace_resumed["result"]["node_count"] == 10
        _, replace_verify = run_cli(
            "--db", relative(replace_db), *common, "verify"
        )
        assert replace_verify["ok"] is True

        result = {
            "checks": {
                "append_classification": merge_preview["decision"][
                    "classification"
                ],
                "bad_shard_error": hash_error["error"]["code"],
                "baseline_result_digest": sha256_object(baseline),
                "crash_exit_code": crash_code,
                "replace_crash_exit_code": replace_crash_code,
                "replace_recovered_node_count": replace_resumed["result"][
                    "node_count"
                ],
                "resume_equal_to_baseline": resumed == baseline,
                "rollback_error": rollback_error["error"]["code"],
                "traversal_committed_batches": crash_status[
                    "committed_traversals"
                ]["batch_count"],
                "tombstone_truncations": len(baseline["truncations"]),
                "verify_ok": crash_verify["ok"] and replace_verify["ok"],
            },
            "ok": True,
            "sample_index": {
                "edge_count": loaded["result"]["edge_count"],
                "index_version": loaded["result"]["index_version"],
                "node_count": loaded["result"]["node_count"],
                "tombstone_count": loaded["result"]["tombstone_count"],
            },
        }
        text = canonical_json(result)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

