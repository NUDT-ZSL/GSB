#!/usr/bin/env python3
"""Run the complete local acceptance verification and write a JSON report."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from support_summary.canonical import atomic_write_json, sha256_bytes
from support_summary.errors import BusinessError
from support_summary.incremental import run_incremental_scenario
from support_summary.models import Turn
from support_summary.nodes import (
    build_merge_block,
    verify_one_shot_equivalence,
)
from support_summary.pipeline import PipelineRunner
from support_summary.recovery import run_recovery_demo
from support_summary.sample_data import write_dataset
from support_summary.selfcheck import run_self_check
from support_summary.snapshots import SnapshotManager
from support_summary.validation import (
    dataset_signature,
    load_config,
    load_sessions,
    load_vocabulary,
    validate_config,
    validate_sessions,
)


def capture_error(function: Callable[[], Any]) -> Dict[str, Any]:
    try:
        function()
    except BusinessError as exc:
        return exc.to_dict()
    return {"code": "NO_ERROR"}


def main() -> int:
    write_dataset(ROOT, force=False)
    paths = {
        "sessions": ROOT / "data" / "sessions.jsonl",
        "vocabulary": ROOT / "data" / "vocabulary_v1.json",
        "vocabulary_v2": ROOT / "data" / "vocabulary_v2.json",
        "config": ROOT / "data" / "params_v1.json",
    }
    sessions = load_sessions(paths["sessions"])
    vocabulary = load_vocabulary(paths["vocabulary"])
    config = load_config(paths["config"])

    with tempfile.TemporaryDirectory(prefix="summary-verify-") as temp_name:
        temp = Path(temp_name)
        full_cache = temp / "full-cache"
        full = PipelineRunner(
            sessions, vocabulary, config, cache_dir=full_cache
        ).run()
        reused = PipelineRunner(
            sessions, vocabulary, config, cache_dir=full_cache
        ).run()

        incrementals = {}
        for scenario, kwargs in (
            ("append", {"target_session_id": sessions[0].session_id}),
            ("strength", {"new_strength": 0.65}),
            (
                "vocab",
                {"vocabulary_path": paths["vocabulary_v2"]},
            ),
        ):
            report = run_incremental_scenario(
                sessions,
                vocabulary,
                config,
                cache_dir=temp / f"incremental-{scenario}",
                scenario=scenario,
                **kwargs,
            )
            incrementals[scenario] = {
                "ok": report["ok"],
                "affected_sessions": report["affected_sessions"],
                "affected_nodes": report["affected_nodes"],
                "recomputed_sessions": report["recomputed_sessions"],
                "reused_sessions": report["reused_sessions"],
                "recomputed_nodes": report["recomputed_nodes"],
                "reused_nodes": report["reused_nodes"],
                "stage_counts": report["stage_counts"],
                "incremental_digest": report["incremental_digest"],
                "full_digest": report["full_digest"],
                "byte_identical_to_full": report["byte_identical_to_full"],
                "node_partition": report["node_partition"],
                "self_check_ok": report["self_check"]["ok"],
            }

        limited = replace(config, max_sessions_per_stage=1)
        degraded = replace(
            config,
            max_sessions_per_stage=1,
            max_resident_frequency=1,
            max_intermediate_memory_bytes=1,
            resource_policy="degrade",
        )
        degraded_result = PipelineRunner(
            sessions,
            vocabulary,
            degraded,
            cache_dir=temp / "degraded",
        ).run()

        errors = {
            "precision": capture_error(
                lambda: validate_config(
                    replace(config, precision="fp8")
                )
            ),
            "block_size": capture_error(
                lambda: validate_config(replace(config, block_size=0))
            ),
            "strength": capture_error(
                lambda: validate_config(
                    replace(config, rewrite_strength=1.2)
                )
            ),
            "duplicate_session": capture_error(
                lambda: validate_sessions([sessions[0], sessions[0]])
            ),
            "weight": capture_error(
                lambda: validate_config(
                    replace(
                        config,
                        weights={**dict(config.weights), "coverage": 0.0},
                    )
                )
            ),
            "resource_limit": capture_error(
                lambda: PipelineRunner(
                    sessions,
                    vocabulary,
                    limited,
                    cache_dir=temp / "limit",
                ).run()
            ),
            "frequency_limit": capture_error(
                lambda: PipelineRunner(
                    sessions,
                    vocabulary,
                    replace(config, max_resident_frequency=1),
                    cache_dir=temp / "frequency-limit",
                ).run()
            ),
            "memory_limit": capture_error(
                lambda: PipelineRunner(
                    sessions,
                    vocabulary,
                    replace(config, max_intermediate_memory_bytes=1),
                    cache_dir=temp / "memory-limit",
                ).run()
            ),
        }

        bad_session = replace(
            sessions[0],
            turns=sessions[0].turns
            + (
                Turn(
                    turn_index=max(
                        turn.turn_index for turn in sessions[0].turns
                    )
                    + 1,
                    role="user",
                    text="ZZZ",
                ),
            ),
        )
        errors["vocab_missing"] = capture_error(
            lambda: PipelineRunner(
                [bad_session],
                vocabulary,
                config,
                cache_dir=temp / "vocab-missing",
            ).run()
        )

        def block_mismatch() -> None:
            session = sessions[0]
            chunk_artifacts = full.stage_results["chunk_tokenize"][
                session.session_id
            ]
            rewrite_blocks = full.stage_results["rewrite"][
                session.session_id
            ]
            blocks = []
            for chunk_artifact, rewrite_block in zip(
                chunk_artifacts, rewrite_blocks
            ):
                chunk = chunk_artifact["payload"]
                blocks.append(
                    build_merge_block(
                        session.session_id,
                        int(chunk["block_index"]),
                        [
                            artifact["payload"]
                            for artifact in rewrite_block["rewrites"]
                        ],
                        int(chunk["token_count"]),
                        [
                            row["word"]
                            for row in chunk["frequencies"]
                        ],
                        config.precision,
                    )
                )
            tampered = [dict(block) for block in blocks]
            tampered[0]["units"] = [
                dict(unit) for unit in tampered[0]["units"]
            ]
            tampered[0]["units"][0]["text"] += " 篡改"
            verify_one_shot_equivalence(
                session,
                tampered,
                [
                    {
                        "block_index": block["block_index"],
                        "rewrites": [
                            artifact["payload"]
                            for artifact in block["rewrites"]
                        ],
                    }
                    for block in rewrite_blocks
                ],
                config.precision,
            )

        errors["block_mismatch"] = capture_error(block_mismatch)

        snapshot_temp = temp / "snapshots"
        expected = ["S1"]
        manager = SnapshotManager(snapshot_temp / "rollback", expected, "sig")
        manager.commit("chunk_tokenize", {"value": 1}, expected)
        errors["snapshot_rollback"] = capture_error(
            lambda: manager.commit(
                "chunk_tokenize", {"value": 0}, expected, version=1
            )
        )

        corrupt = SnapshotManager(snapshot_temp / "corrupt", expected, "sig")
        corrupt.commit("chunk_tokenize", {"value": 1}, expected)
        corrupt_path = corrupt.root / "01_chunk_tokenize.json"
        corrupt_value = json.loads(corrupt_path.read_text(encoding="utf-8"))
        corrupt_value["checksum"] = "0" * 64
        corrupt_path.write_text(
            json.dumps(corrupt_value), encoding="utf-8"
        )
        errors["snapshot_corrupt"] = capture_error(corrupt.load)

        busy = SnapshotManager(snapshot_temp / "busy", expected, "sig")
        with busy.stage_lock("chunk_tokenize"):
            errors["stage_busy"] = capture_error(
                lambda: busy.commit(
                    "chunk_tokenize", {"value": 1}, expected
                )
            )

    recovery = run_recovery_demo(ROOT)
    report = {
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "hostname": socket.gethostname(),
        },
        "dataset": {
            name: {
                "path": str(path),
                "sha256": sha256_bytes(path.read_bytes()),
            }
            for name, path in paths.items()
        },
        "full_run": {
            "summary_digest": full.summary_digest,
            "cache_stats": full.cache_stats,
            "self_check": run_self_check(full),
        },
        "cache_reuse": {
            "summary_digest": reused.summary_digest,
            "cache_stats": reused.cache_stats,
            "byte_identical": full.summary_bytes == reused.summary_bytes,
        },
        "incremental": incrementals,
        "degradation": degraded_result.degradations,
        "business_errors": errors,
        "recovery_demo": recovery,
    }
    output = ROOT / "artifacts" / "verification.json"
    atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(
        (
            report["full_run"]["self_check"]["ok"],
            report["cache_reuse"]["byte_identical"],
            all(item["ok"] for item in report["incremental"].values()),
            report["recovery_demo"]["ok"],
        )
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
