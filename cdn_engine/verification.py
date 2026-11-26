from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .engine import Engine
from .errors import EngineError
from .origin import start_origin_server
from .parser import parse_sample, read_json
from .seed import EPOCH_MS, generate_sample
from .storage import Store


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FINAL_CLOCK = EPOCH_MS + 120_000
PATCH_CLOCK = EPOCH_MS + 125_000
PACKAGE_CLOCK = EPOCH_MS + 140_000


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _sequence(store: Store) -> List[Dict[str, Any]]:
    rows = store.conn.execute(
        """
        SELECT node_name, rule_name, start_ms, verdict_code, result_digest, result_json
        FROM conclusions ORDER BY start_ms, node_name, rule_name, conclusion_id
        """
    ).fetchall()
    return [
        {
            "node": row["node_name"],
            "rule": row["rule_name"],
            "start_ms": row["start_ms"],
            "verdict": row["verdict_code"],
            "digest": row["result_digest"],
        }
        for row in rows
    ]


def _run_cli(workspace: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "cdn_engine.cli", *arguments],
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(arguments)}\n{result.stderr}\n{result.stdout}")
    return result


def run_verification(
    workspace: Path,
    *,
    requested_port: int = 18080,
    offline_port: int = 18099,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def check(name: str, condition: bool, detail: Any = None) -> None:
        if not condition:
            raise AssertionError(f"verification check failed: {name}: {detail}")
        checks.append({"name": name, "ok": True, "detail": detail})

    workspace = Path(workspace).resolve()
    if workspace.exists():
        if workspace.name == ".verify-run":
            shutil.rmtree(workspace)
        else:
            parent = workspace if workspace.is_dir() else workspace.parent
            parent.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix="cdn-verify-", dir=str(parent)))
    workspace.mkdir(parents=True, exist_ok=True)

    port = requested_port or free_port()
    offline_port = offline_port or free_port()
    sample_dir = workspace / "sample"
    generate_sample(sample_dir, port=port, offline_port=offline_port)
    content = read_json(sample_dir / "origin-content.json")["content"]
    server = start_origin_server(content, port=port, content_path=sample_dir / "origin-content.json")

    try:
        state_path = workspace / "state.db"
        store = Store(state_path)
        engine = Engine(store)
        load_result = engine.load_sample(sample_dir, EPOCH_MS)
        merge_result = engine.merge(EPOCH_MS)
        check("load_validates_sample", load_result["observations"] == 7, load_result)
        check("merge_rule_count", merge_result["summary"]["rule_count"] == 8, merge_result["summary"])

        rules = {rule["name"]: rule for rule in merge_result["rules"]}
        check(
            "equal_clock_uses_source_priority",
            rules["shared-config"]["source_version"] == "v2",
            rules["shared-config"]["decision_basis"]["tie_break_applied"],
        )
        check(
            "v1_relative_time_conversion_recorded",
            rules["api-auth"]["conversion"]["scale_ms_per_second"] == 1000
            and rules["api-auth"]["effective_at_ms"] == EPOCH_MS + 30_000,
            rules["api-auth"]["conversion"],
        )

        probe_result = engine.probe(FINAL_CLOCK)
        check("probe_counts_all_observations", probe_result["processed_new"] == 7, {
            "processed_new": probe_result["processed_new"],
            "closed": probe_result["closed_windows"],
        })
        status = engine.status()
        check("conclusions_committed", status["counts"]["conclusions"] == 42, status["counts"])
        check("expected_verdicts", status["conclusions_by_verdict"].get("DIGEST_MISMATCH") == 1, status["conclusions_by_verdict"])
        check("origin_refusal_distinguished", status["conclusions_by_verdict"].get("ORIGIN_CONNECTION_REFUSED") == 1, status["conclusions_by_verdict"])

        conclusions = engine.list_conclusions()
        by_key = {
            (item["window"]["node"], item["window"]["rule_name"], item["window"]["start_ms"]): item
            for item in conclusions
        }
        hit = by_key[("edge-a", "catalog-page", EPOCH_MS)]
        mismatch = by_key[("edge-b", "catalog-page", EPOCH_MS)]
        refused = by_key[("edge-offline", "origin-auth", EPOCH_MS)]
        no_observation = by_key[("edge-a", "origin-catalog", EPOCH_MS)]
        check("hit_verdict", hit["verdict_code"] == "CACHE_HIT_VERIFIED", hit["verdict_code"])
        check("digest_mismatch_verdict", mismatch["verdict_code"] == "DIGEST_MISMATCH", mismatch["verdict_code"])
        check("connection_refused_verdict", refused["verdict_code"] == "ORIGIN_CONNECTION_REFUSED", refused["verdict_code"])
        check("no_observation_verdict", no_observation["verdict_code"] == "NO_OBSERVATION", no_observation["verdict_code"])
        arbitration = next(
            item["origin_arbitration"]
            for item in hit["observations"]
            if item["origin_arbitration"] and item["origin_arbitration"].get("url")
        )
        check(
            "origin_headers_digest_and_version_output",
            bool(arbitration["request_headers"].get("X-CDN-Rule"))
            and arbitration["response_digest"].startswith("sha256:")
            and hit["rule_version"]["source_format"] == "v2_sharded_sqlite",
            {"headers": arbitration["request_headers"], "rule_version": hit["rule_version"]},
        )
        first_sequence = _sequence(store)

        repeat_state = workspace / "repeat.db"
        repeat_store = Store(repeat_state)
        repeat_engine = Engine(repeat_store)
        repeat_engine.load_sample(sample_dir, EPOCH_MS)
        repeat_engine.merge(EPOCH_MS)
        repeat_engine.probe(FINAL_CLOCK)
        second_sequence = _sequence(repeat_store)
        check("conclusion_sequence_bitwise_repeatable", first_sequence == second_sequence, len(first_sequence))
        repeat_store.close()

        patch_result = engine.apply_patch_file(sample_dir / "patches" / "patch-origin-catalog.json", PATCH_CLOCK)
        closure = patch_result["closure"]
        page_entry = next(entry for entry in closure["entries"] if entry["rule_name"] == "catalog-page")
        check(
            "dependency_closure_path",
            closure["affected_rules"] == ["catalog-list", "catalog-page", "origin-catalog"]
            and page_entry["dependency_path"] == ["origin-catalog", "catalog-list", "catalog-page"],
            closure["affected_rules"],
        )
        recompute = engine.recompute_closure(closure["closure_id"], PATCH_CLOCK)
        check("closure_recompute_windows", recompute["recomputed_windows"] == 18, recompute["recomputed_windows"])
        patch_again = engine.apply_patch_file(sample_dir / "patches" / "patch-origin-catalog.json", PATCH_CLOCK)
        check("patch_idempotent", patch_again["idempotent_replay"] is True)

        package_result = engine.load_package(sample_dir / "packages" / "rules-package-3", PACKAGE_CLOCK)
        check("package_full_replace", package_result["decision"] == "full_replace", package_result["decision"])
        try:
            engine.load_package(sample_dir / "v2", PACKAGE_CLOCK)
            rollback_rejected = False
        except EngineError as exc:
            rollback_rejected = exc.code == "E_PACKAGE_ROLLBACK"
        check("package_rollback_rejected", rollback_rejected)
        store.close()

        # Hard-kill recovery through the real CLI.
        crash_state = workspace / "crash.db"
        _run_cli(workspace, "load", "--state", str(crash_state), "--sample", str(sample_dir), "--clock", str(EPOCH_MS))
        _run_cli(workspace, "merge", "--state", str(crash_state), "--clock", str(EPOCH_MS))
        crashed = _run_cli(
            workspace,
            "probe",
            "--state",
            str(crash_state),
            "--clock",
            str(FINAL_CLOCK),
            "--crash-after-obs",
            "1",
            check=False,
        )
        check("sigkill_fault_injection", crashed.returncode in (-9, 137), crashed.returncode)
        resumed = _run_cli(
            workspace, "resume", "--state", str(crash_state), "--clock", str(FINAL_CLOCK)
        )
        resume_payload = json.loads(resumed.stdout)
        check(
            "resume_replays_only_unfinished",
            resume_payload["probe"]["processed_new"] == 6
            and resume_payload["probe"]["skipped_duplicates"] == 1,
            resume_payload["probe"],
        )
        crash_status = json.loads(
            _run_cli(workspace, "status", "--state", str(crash_state)).stdout
        )
        check(
            "recovery_no_duplicate_conclusions",
            crash_status["counts"]["processed_observations"] == 7
            and crash_status["counts"]["conclusions"] == 42,
            crash_status["counts"],
        )

        package_crash_state = workspace / "package-crash.db"
        _run_cli(workspace, "load", "--state", str(package_crash_state), "--sample", str(sample_dir), "--clock", str(EPOCH_MS))
        _run_cli(workspace, "merge", "--state", str(package_crash_state), "--clock", str(EPOCH_MS))
        crashed_package = _run_cli(
            workspace,
            "load",
            "--state",
            str(package_crash_state),
            "--package",
            str(sample_dir / "packages" / "rules-package-3"),
            "--clock",
            str(PACKAGE_CLOCK),
            "--crash-before-commit",
            check=False,
        )
        check("package_switch_sigkill", crashed_package.returncode in (-9, 137), crashed_package.returncode)
        package_status_before = json.loads(
            _run_cli(workspace, "status", "--state", str(package_crash_state)).stdout
        )
        check(
            "package_pointer_unchanged_before_recovery",
            package_status_before["active_view_id"] == 1
            and package_status_before["counts"]["staging_operations"] == 1,
            package_status_before["counts"],
        )
        package_resume = json.loads(
            _run_cli(workspace, "resume", "--state", str(package_crash_state)).stdout
        )
        check(
            "package_staging_aborted_by_recovery",
            len(package_resume["aborted_staging_operations"]) == 1,
            package_resume["aborted_staging_operations"],
        )
        package_status_after = json.loads(
            _run_cli(workspace, "status", "--state", str(package_crash_state)).stdout
        )
        check(
            "package_pointer_stable_after_recovery",
            package_status_after["active_view_id"] == 1
            and package_status_after["counts"]["staging_operations"] == 0,
            package_status_after["counts"],
        )

        # Real multi-process parallel node processing.
        conc_state = workspace / "conc.db"
        _run_cli(workspace, "load", "--state", str(conc_state), "--sample", str(sample_dir), "--clock", str(EPOCH_MS))
        _run_cli(workspace, "merge", "--state", str(conc_state), "--clock", str(EPOCH_MS))
        processes = [
            subprocess.Popen(
                [
                    sys.executable, "-m", "cdn_engine.cli", "probe",
                    "--state", str(conc_state), "--node", node, "--clock", str(FINAL_CLOCK),
                ],
                cwd=str(PROJECT_ROOT),
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for node in ("edge-a", "edge-b", "edge-offline")
        ]
        outputs = []
        for process in processes:
            outputs.append(process.wait(timeout=60))
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        check("parallel_node_processes_exit_zero", outputs == [0, 0, 0], outputs)
        conc_store = Store(conc_state)
        conc_status = Engine(conc_store).status()
        check(
            "parallel_no_duplicate_counts",
            conc_status["counts"]["processed_observations"] == 7
            and conc_status["counts"]["conclusions"] == 42,
            conc_status["counts"],
        )
        conc_store.close()

        _verify_negative_inputs(workspace, sample_dir, checks, check)

        return {
            "ok": True,
            "command": "verify",
            "workspace": str(workspace),
            "origin_port": port,
            "check_count": len(checks),
            "checks": checks,
            "conclusion_sequence_digest": _digest_sequence(first_sequence),
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": "verify",
            "workspace": str(workspace),
            "checks": checks,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    finally:
        server.shutdown()
        server.server_close()


def _digest_sequence(sequence: List[Dict[str, Any]]) -> str:
    from .hashing import sha256_object

    return sha256_object(sequence)


def _verify_negative_inputs(workspace: Path, sample_dir: Path, checks: List[Dict[str, Any]], check: Any) -> None:
    duplicate_dir = workspace / "bad-duplicate"
    shutil.copytree(sample_dir, duplicate_dir)
    v1_path = duplicate_dir / "v1" / "rules.json"
    v1 = read_json(v1_path)
    v1["rules"].append(dict(v1["rules"][0]))
    from .seed import write_json

    write_json(v1_path, v1)
    try:
        parse_sample(duplicate_dir)
        code = None
    except EngineError as exc:
        code = exc.code
    check("duplicate_rule_rejected", code == "E_DUPLICATE_RULE_NAME", code)

    hash_dir = workspace / "bad-hash"
    shutil.copytree(sample_dir, hash_dir)
    shard_path = hash_dir / "v2" / "shards" / "shard-00-core.json"
    shard = read_json(shard_path)
    shard["tampered"] = True
    write_json(shard_path, shard)
    try:
        parse_sample(hash_dir)
        code = None
    except EngineError as exc:
        code = exc.code
    check("shard_hash_mismatch_rejected", code == "E_SHARD_HASH_MISMATCH", code)
