#!/usr/bin/env python3
"""Run the acceptance-oriented validation suite and persist a JSON report."""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from index_diag.compression import (
    CompressionParams,
    decode_integer_stream,
    encode_integer_stream,
)
from index_diag.config import (
    downgrade_v2_to_v1,
    load_config,
    upgrade_v1_to_v2,
)
from index_diag.engine import IndexEngine, InjectedFault
from index_diag.errors import DiagnosticError
from index_diag.ledger import VersionedSnapshotStore
from index_diag.postings import (
    postings_digest,
    serialize_postings,
)
from index_diag.samples import generate_samples
from index_diag.util import canonical_json


def _check(
    checks: list[dict[str, Any]],
    name: str,
    function: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    try:
        details = function()
        check = {"details": details, "name": name, "ok": True}
    except Exception as exc:  # noqa: BLE001 - report every acceptance failure
        details = {
            "error": str(exc),
            "type": type(exc).__name__,
        }
        if isinstance(exc, DiagnosticError):
            details["code"] = exc.code
            details["details"] = exc.details
        check = {"details": details, "name": name, "ok": False}
    checks.append(check)
    return check


def _expect_code(code: str, function: Callable[[], Any]) -> dict[str, Any]:
    try:
        function()
    except DiagnosticError as exc:
        if exc.code != code:
            raise AssertionError(f"expected {code}, got {exc.code}: {exc.message}")
        return {"expected": code, "observed": exc.code, "details": exc.details}
    raise AssertionError(f"expected {code}, operation succeeded")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def run_unit_tests(report_dir: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    (report_dir / "unittest.txt").write_text(output, encoding="utf-8")
    match = re.search(r"Ran (\d+) tests?", output)
    count = int(match.group(1)) if match else 0
    return {
        "command": f"{sys.executable} -m unittest discover -v",
        "failed": output.count("FAILED"),
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "tests": count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", default="artifacts")
    parser.add_argument("--seed", type=int, default=20261127)
    args = parser.parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "checks": [],
        "environment": {
            "numpy": np.__version__,
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "seed": args.seed,
    }
    checks = report["checks"]

    with tempfile.TemporaryDirectory(prefix="index-diag-validation-") as temporary:
        root = Path(temporary)
        samples = root / "samples"
        samples_again = root / "samples-again"

        def deterministic_samples() -> dict[str, Any]:
            first = generate_samples(samples, seed=args.seed)
            second = generate_samples(samples_again, seed=args.seed)
            if first["batch_hashes"] != second["batch_hashes"]:
                raise AssertionError("batch hashes differ for the same seed")
            for relative in first["batches"]:
                if (samples / relative).read_bytes() != (
                    samples_again / relative
                ).read_bytes():
                    raise AssertionError(f"{relative} is not byte deterministic")
            for relative in (
                "config/v2/manifest.json",
                "config/v2/metadata.sqlite",
                "config/v2/shards/shard_000.json",
            ):
                if (samples / relative).read_bytes() != (
                    samples_again / relative
                ).read_bytes():
                    raise AssertionError(
                        f"{relative} is not byte deterministic"
                    )
            if first["replacement_hash"] != second["replacement_hash"]:
                raise AssertionError("replacement fixture differs for the same seed")
            return {
                "batch_hashes": first["batch_hashes"],
                "manifest_digest": first["digest"],
            }

        _check(checks, "fixed_seed_byte_determinism", deterministic_samples)
        manifest = generate_samples(samples, seed=args.seed)
        config_v1 = samples / manifest["config_v1"]
        config_v2 = samples / manifest["config_v2"]
        config_v1_gen2 = samples / manifest["config_v1_generation2"]
        batches = [samples / relative for relative in manifest["batches"]]
        replacement = samples / manifest["replacement"]
        fault = samples / "faults" / "fail_before_compress_checkpoint.json"
        recovery = samples / "recovery" / "resume.json"

        def config_migration() -> dict[str, Any]:
            upgraded = upgrade_v1_to_v2(
                config_v1, root / "migration-v2", shard_count=3
            )
            downgraded = downgrade_v2_to_v1(
                upgraded.target_path, root / "migration-v1.json"
            )
            original = load_config(config_v1)
            up = load_config(upgraded.target_path)
            down = load_config(downgraded.target_path)
            for name in (
                "generation",
                "logical_clock",
                "chunk_size",
                "compression",
                "delta_step",
                "vocabulary",
                "resource_limits",
            ):
                if getattr(original, name) != getattr(up, name):
                    raise AssertionError(f"upgrade changed {name}")
                if getattr(original, name) != getattr(down, name):
                    raise AssertionError(f"downgrade changed {name}")
            if not up.migration_history:
                raise AssertionError("upgrade did not record migration history")
            return {
                "completed_default_fields": upgraded.report[
                    "completed_default_fields"
                ],
                "dropped_fields": downgraded.report["dropped_fields"],
                "flattened_fields": downgraded.report["flattened_fields"],
                "v1_hash": original.config_hash,
                "v2_hash": up.config_hash,
            }

        _check(checks, "config_v1_v2_migration", config_migration)

        def codec_round_trip() -> dict[str, Any]:
            values = [
                0,
                1,
                9,
                3,
                -7,
                100,
                105,
                -(2**63),
                2**63 - 1,
            ]
            results = {}
            for mode in (
                "raw",
                "zlib",
                "gzip",
                "delta",
                "delta_zlib",
                "delta_gzip",
            ):
                params = CompressionParams(4, mode, 2)
                payload = encode_integer_stream(values, params)
                decoded = decode_integer_stream(
                    payload, params, expected_length=len(values)
                )
                if decoded != values:
                    raise AssertionError(f"{mode} did not round trip")
                results[mode] = len(payload)
            return {"encoded_sizes": results, "values": values}

        _check(checks, "compression_round_trip", codec_round_trip)

        full_engine = IndexEngine(root / "full-v2", config_path=config_v2)

        def full_build() -> dict[str, Any]:
            result = full_engine.run(batches)
            check = full_engine.self_check()
            if result["checkpoint"]["phase"] != "ledger":
                raise AssertionError("pipeline did not finish at ledger")
            return {
                "checkpoint_version": result["checkpoint"]["version"],
                "config_hash": result["config"]["config_hash"],
                "documents": result["postings"]["documents"],
                "ledger": result["ledger"],
                "postings_digest": postings_digest(full_engine.postings),
                "selfcheck": check["status"],
                "terms": result["postings"]["terms"],
                "total_postings": result["postings"]["total_postings"],
            }

        _check(checks, "full_build_and_selfcheck", full_build)

        def v1_v2_consistency() -> dict[str, Any]:
            first = IndexEngine(root / "full-v1", config_path=config_v1)
            first.run(batches)
            first.self_check()
            if postings_digest(first.postings) != postings_digest(
                full_engine.postings
            ):
                raise AssertionError("v1 and v2 builds produced different postings")
            if serialize_postings(first.postings) != serialize_postings(
                full_engine.postings
            ):
                raise AssertionError("v1 and v2 serialized postings differ")
            return {
                "v1_postings_digest": postings_digest(first.postings),
                "v2_postings_digest": postings_digest(full_engine.postings),
            }

        _check(checks, "v1_v2_index_consistency", v1_v2_consistency)

        def incremental_consistency() -> dict[str, Any]:
            engine = IndexEngine(root / "incremental", config_path=config_v2)
            engine.run([batches[0]])
            second = engine.run([batches[1], batches[2]])
            engine.self_check()
            if postings_digest(engine.postings) != postings_digest(
                full_engine.postings
            ):
                raise AssertionError("incremental postings differ from full build")
            return {
                "invalidation_reason": second["invalidation"]["reason"],
                "postings_digest": postings_digest(engine.postings),
                "recomputed_blocks": second["work"][
                    "compression_blocks_recomputed"
                ],
                "reused_blocks": second["work"]["compression_blocks_reused"],
            }

        _check(checks, "incremental_equivalence", incremental_consistency)

        def config_generation() -> dict[str, Any]:
            engine = IndexEngine(root / "config-generation", config_path=config_v1)
            engine.run(batches)
            result = engine.run(batches, config_path=config_v1_gen2)
            engine.self_check()
            if result["work"]["analyzed_documents"] != 0:
                raise AssertionError("configuration upgrade re-analyzed documents")
            if result["work"]["compression_blocks_recomputed"] <= 0:
                raise AssertionError("chunk parameter change did not invalidate blocks")
            return {
                "config_generation": result["config"]["generation"],
                "invalidation": result["invalidation"],
                "recomputed_blocks": result["work"][
                    "compression_blocks_recomputed"
                ],
            }

        _check(checks, "config_generation_invalidation", config_generation)

        def replacement_validation() -> dict[str, Any]:
            engine = IndexEngine(root / "replacement", config_path=config_v2)
            engine.run(batches)
            result = engine.replace_batch(2, replacement)
            engine.self_check()
            invalidation = result["invalidation"]
            if invalidation["blocks_reused"] <= 0:
                raise AssertionError("replacement reused no compressed prefixes")
            if invalidation["blocks_recomputed"] <= 0:
                raise AssertionError("replacement recomputed no suffix blocks")
            return {
                "affected_terms": len(invalidation["affected_terms"]),
                "blocks_recomputed": invalidation["blocks_recomputed"],
                "blocks_reused": invalidation["blocks_reused"],
                "first_recomputed_block": invalidation["first_recomputed_block"],
                "reason": invalidation["reason"],
                "selfcheck": "ok",
            }

        _check(checks, "batch_replacement_suffix_reuse", replacement_validation)

        def fault_recovery() -> dict[str, Any]:
            work = root / "fault-recovery"
            engine = IndexEngine(work, config_path=config_v2)
            interrupted_phase = None
            try:
                engine.run(batches, fault_path=fault)
            except InjectedFault as injected:
                interrupted_phase = injected.phase
            else:
                raise AssertionError("fault was not injected")
            interrupted = IndexEngine(work)
            checkpoint_before = interrupted.checkpoint.version
            recovered = IndexEngine(work)
            result = recovered.run(recovery_path=recovery)
            recovered.self_check()
            if result["work"]["analyzed_documents"] != 0:
                raise AssertionError("recovery re-analyzed committed batches")
            if result["work"]["postings_added"] != 0:
                raise AssertionError("recovery re-added committed postings")
            return {
                "checkpoint_before_recovery": checkpoint_before,
                "checkpoint_after_recovery": result["checkpoint"]["version"],
                "interrupted_at": interrupted_phase,
                "recomputed_blocks": result["work"][
                    "compression_blocks_recomputed"
                ],
                "reused_blocks": result["work"]["compression_blocks_reused"],
                "selfcheck": "ok",
            }

        _check(checks, "fault_recovery", fault_recovery)

        def business_codes() -> dict[str, Any]:
            results: dict[str, Any] = {}
            invalid = _load_json(samples / "invalid" / "v1_missing_chunk.json")
            invalid_path = root / "invalid-config.json"
            _write_json(invalid_path, invalid)
            results["E_VERSION_UNSUPPORTED"] = _expect_code(
                "E_VERSION_UNSUPPORTED", lambda: load_config(invalid_path)
            )

            rollback = IndexEngine(root / "code-batch-rollback", config_path=config_v2)
            rollback.run([batches[1]])
            results["E_BATCH_ROLLBACK"] = _expect_code(
                "E_BATCH_ROLLBACK", lambda: rollback.run([batches[0]])
            )

            config_rollback = IndexEngine(
                root / "code-config-rollback", config_path=config_v1
            )
            config_rollback.run(batches)
            config_rollback.run(batches, config_path=config_v1_gen2)
            results["E_CONFIG_ROLLBACK"] = _expect_code(
                "E_CONFIG_ROLLBACK",
                lambda: config_rollback.run(batches, config_path=config_v1),
            )

            strict_config = _load_json(config_v1)
            strict_config["resource_limits"]["max_batch_docs"] = 1
            strict_path = root / "strict-resource.json"
            _write_json(strict_path, strict_config)
            limited = IndexEngine(root / "code-resource", config_path=strict_path)
            results["E_RESOURCE_LIMIT"] = _expect_code(
                "E_RESOURCE_LIMIT", lambda: limited.run([batches[0]])
            )

            duplicate_document = {
                "batch_no": 99,
                "content_hash": "duplicate-content",
                "doc_id": 999001,
                "doc_length": 1,
                "term_frequencies": {"search": 1},
                "terms": ["search"],
            }
            duplicate_path = root / "duplicate.jsonl"
            duplicate_path.write_text(
                canonical_json(duplicate_document)
                + "\n"
                + canonical_json(duplicate_document)
                + "\n",
                encoding="utf-8",
            )
            duplicate_engine = IndexEngine(
                root / "code-duplicate", config_path=config_v2
            )
            results["E_DUP_DOC"] = _expect_code(
                "E_DUP_DOC",
                lambda: duplicate_engine.run([duplicate_path]),
            )

            snapshot = VersionedSnapshotStore(
                root / "snapshot.json", format_name="validation-snapshot"
            )
            snapshot.commit(2, {"value": "current"})
            results["E_SNAPSHOT_ROLLBACK"] = _expect_code(
                "E_SNAPSHOT_ROLLBACK",
                lambda: snapshot.commit(1, {"value": "old"}),
            )
            return results

        _check(checks, "business_error_codes", business_codes)

    unit_tests = run_unit_tests(report_dir)
    report["unit_tests"] = unit_tests
    report["passed"] = bool(unit_tests["passed"]) and all(
        check["ok"] for check in checks
    )
    report_path = report_dir / "validation_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "report": str(report_path),
                "checks": [
                    {"name": check["name"], "ok": check["ok"]}
                    for check in checks
                ],
                "unit_tests": unit_tests,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
