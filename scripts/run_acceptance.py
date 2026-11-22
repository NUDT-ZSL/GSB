from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def cli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "ckptlab.cli", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def payload(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if not process.stdout.strip():
        raise RuntimeError(
            f"CLI produced no JSON: rc={process.returncode}, stderr={process.stderr}"
        )
    return json.loads(process.stdout)


def common(root: Path) -> tuple[str, ...]:
    return "--db", str(root / "ledger.sqlite3"), "--store", str(root / "store")


def query(db: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="ckptlab-acceptance-"))
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    def check(name: str, condition: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    plan_v1 = SAMPLES / "training_plan.json"
    plan_v2 = SAMPLES / "training_plan_v2.json"
    shards = SAMPLES / "shard_policy.json"

    invalid_plan = root / "invalid-plan.json"
    invalid_plan.write_text(
        json.dumps(
            {
                "seed": 1,
                "layers": [
                    {
                        "name": "bad",
                        "shape": [2],
                        "dtype": "not-a-dtype",
                        "has_optimizer_state": False,
                    }
                ],
                "save": {
                    "version": 1,
                    "logical_clock_ms": 0,
                    "max_concurrency": 1,
                    "logical_time_budget_ms": 10,
                    "incremental": False,
                },
            }
        ),
        encoding="utf-8",
    )
    invalid_root = root / "invalid"
    invalid = cli(
        "save",
        "--plan",
        str(invalid_plan),
        "--shards",
        str(shards),
        *common(invalid_root),
    )
    invalid_error = json.loads(invalid.stderr)["error"]["code"]
    check(
        "invalid_input_no_persistence",
        invalid.returncode == 1
        and invalid_error == "DTYPE_INVALID"
        and not (invalid_root / "ledger.sqlite3").exists()
        and not (invalid_root / "store").exists(),
        {"returncode": invalid.returncode, "code": invalid_error},
    )

    incremental_root = root / "incremental"
    v1_process = cli(
        "save", "--plan", str(plan_v1), "--shards", str(shards), *common(incremental_root)
    )
    v1 = payload(v1_process)
    v1_manifest = v1["manifest_digest"]
    check(
        "parallel_v1_save",
        v1_process.returncode == 0
        and v1["committed"]
        and v1["summary"]["written"] == 4,
        v1["summary"],
    )

    v2_process = cli(
        "save", "--plan", str(plan_v2), "--shards", str(shards), *common(incremental_root)
    )
    v2 = payload(v2_process)
    check(
        "incremental_hit_reuse",
        v2_process.returncode == 0
        and v2["committed"]
        and v2["summary"]["hit"] == 3
        and v2["summary"]["written"] == 1,
        {
            "summary": v2["summary"],
            "hits": [
                {"shard_id": item["shard_id"], "reason": item["reason"]}
                for item in v2["hits"]
            ],
        },
    )

    full_root = root / "full-v2"
    full_process = cli(
        "save", "--plan", str(plan_v2), "--shards", str(shards), *common(full_root)
    )
    full_v2 = payload(full_process)
    incremental_manifest_path = (
        incremental_root / "store" / "manifests" / f"{v2['manifest_digest']}.json"
    )
    full_manifest_path = (
        full_root / "store" / "manifests" / f"{full_v2['manifest_digest']}.json"
    )
    manifest_bytes_equal = (
        incremental_manifest_path.read_bytes() == full_manifest_path.read_bytes()
    )
    check(
        "incremental_full_manifest_identity",
        full_process.returncode == 0
        and full_v2["manifest_digest"] == v2["manifest_digest"]
        and manifest_bytes_equal,
        {
            "incremental_manifest": v2["manifest_digest"],
            "full_manifest": full_v2["manifest_digest"],
            "manifest_bytes_equal": manifest_bytes_equal,
        },
    )

    restore_dir = root / "restore-output"
    restore_process = cli(
        "restore",
        *common(incremental_root),
        "--output-dir",
        str(restore_dir),
    )
    restored = payload(restore_process)
    npy_count = len(list(restore_dir.glob("*.npy")))
    check(
        "restore_concat_and_independent_rebuild",
        restore_process.returncode == 0
        and restored["reconstruction"]["agreement"] is True
        and npy_count == 20,
        {
            "layer_count": restored["layer_count"],
            "shard_count": restored["shard_count"],
            "arrays": npy_count,
            "chain_digest": restored["chain_digest"],
        },
    )
    metrics["v2_chain_digest"] = restored["chain_digest"]

    verify_process = cli("verify", *common(incremental_root))
    verify = payload(verify_process)
    check(
        "verify_hash_and_layers",
        verify_process.returncode == 0 and verify["verified"] is True,
        {"version": verify["version"], "layers": verify["layer_count"]},
    )
    status_first = cli("status", *common(incremental_root))
    status_second = cli("status", *common(incremental_root))
    status = payload(status_first)
    check(
        "status_stable_sorted",
        status_first.returncode == 0
        and status_first.stdout == status_second.stdout
        and status["recovery_decision"]["decision"] == "visible_committed",
        {
            "summary": status["summary"],
            "hits": [item["shard_id"] for item in status["hits"]],
        },
    )

    crash_root = root / "crash-incomplete"
    crash_process = cli(
        "save",
        "--plan",
        str(plan_v1),
        "--shards",
        str(shards),
        "--crash-plan",
        str(SAMPLES / "crash_after_two_shards.json"),
        *common(crash_root),
    )
    crash_builds = query(crash_root / "ledger.sqlite3", "SELECT status FROM builds")
    crash_commits = query(
        crash_root / "ledger.sqlite3", "SELECT shard_id FROM shard_commits ORDER BY shard_id"
    )
    crash_pointer = query(crash_root / "ledger.sqlite3", "SELECT version FROM pointer")
    visible_before_resume = cli("restore", *common(crash_root))
    resume_process = cli(
        "resume", "--plan", str(plan_v1), "--shards", str(shards), *common(crash_root)
    )
    resumed = payload(resume_process)
    check(
        "forced_exit_and_resume_identity",
        crash_process.returncode == 91
        and crash_builds == [("writing",)]
        and [item[0] for item in crash_commits] == ["shard-0000", "shard-0001"]
        and crash_pointer == [(None,)]
        and visible_before_resume.returncode == 1
        and resume_process.returncode == 0
        and resumed["committed"]
        and resumed["manifest_digest"] == v1_manifest
        and resumed["summary"]["written"] == 1
        and resumed["summary"]["hit"] == 3,
        {
            "crash_returncode": crash_process.returncode,
            "committed_before_exit": [item[0] for item in crash_commits],
            "pointer_before_resume": crash_pointer[0][0],
            "resume_summary": resumed["summary"],
            "manifest_matches_uninterrupted": resumed["manifest_digest"] == v1_manifest,
        },
    )

    manifest_crash_root = root / "crash-manifest-write"
    manifest_crash = cli(
        "save",
        "--plan",
        str(plan_v1),
        "--shards",
        str(shards),
        "--crash-plan",
        str(SAMPLES / "crash_during_manifest_write.json"),
        *common(manifest_crash_root),
    )
    stale_temps_before = list(
        (manifest_crash_root / "store" / "manifests").glob(".tmp-v1-manifest-*")
    )
    manifest_resume = cli(
        "resume",
        "--plan",
        str(plan_v1),
        "--shards",
        str(shards),
        *common(manifest_crash_root),
    )
    manifest_resumed = payload(manifest_resume)
    stale_temps_after = list(
        (manifest_crash_root / "store" / "manifests").glob(".tmp-v1-manifest-*")
    )
    check(
        "partial_manifest_cleanup",
        manifest_crash.returncode == 92
        and len(stale_temps_before) == 1
        and not stale_temps_after
        and manifest_resume.returncode == 0
        and manifest_resumed["manifest_digest"] == v1_manifest,
        {
            "crash_returncode": manifest_crash.returncode,
            "temp_before": len(stale_temps_before),
            "temp_after": len(stale_temps_after),
        },
    )

    budget_root = root / "budget"
    budget_plan = json.loads(plan_v1.read_text(encoding="utf-8"))
    budget_plan["save"]["logical_time_budget_ms"] = 1
    budget_plan_path = root / "budget-plan.json"
    budget_plan_path.write_text(json.dumps(budget_plan), encoding="utf-8")
    budget_save = cli(
        "save",
        "--plan",
        str(budget_plan_path),
        "--shards",
        str(shards),
        *common(budget_root),
    )
    budget_result = payload(budget_save)
    budget_resume = cli(
        "resume",
        "--plan",
        str(budget_plan_path),
        "--shards",
        str(shards),
        "--logical-time-budget-ms",
        "200",
        *common(budget_root),
    )
    budget_resumed = payload(budget_resume)
    check(
        "budget_outcomes_and_resume",
        budget_save.returncode == 3
        and budget_result["committed"] is False
        and budget_result["summary"]["budget_unstarted"] == 4
        and budget_resume.returncode == 0
        and budget_resumed["committed"],
        {
            "initial_summary": budget_result["summary"],
            "resume_summary": budget_resumed["summary"],
        },
    )

    concurrency_root = root / "concurrent"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    command = [
        sys.executable,
        "-m",
        "ckptlab.cli",
        "save",
        "--plan",
        str(plan_v1),
        "--shards",
        str(shards),
        *common(concurrency_root),
    ]
    first = subprocess.Popen(
        command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    second = subprocess.Popen(
        command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    first_json = json.loads(first_out) if first_out.strip() else {}
    second_json = json.loads(second_out) if second_out.strip() else {}
    builds_count = len(query(concurrency_root / "ledger.sqlite3", "SELECT * FROM builds"))
    commit_count = len(
        query(concurrency_root / "ledger.sqlite3", "SELECT * FROM shard_commits")
    )
    check(
        "multiprocess_idempotent_commit",
        first.returncode == 0
        and second.returncode == 0
        and sorted(
            [first_json.get("idempotent"), second_json.get("idempotent")]
        )
        == [False, True]
        and builds_count == 1
        and commit_count == 4,
        {
            "returncodes": [first.returncode, second.returncode],
            "idempotent": [
                first_json.get("idempotent"),
                second_json.get("idempotent"),
            ],
            "builds": builds_count,
            "shard_commits": commit_count,
            "stderr": [first_err, second_err],
        },
    )

    sys.path.insert(0, str(ROOT))
    from ckptlab.web import create_app

    app = create_app(incremental_root / "ledger.sqlite3", incremental_root / "store")
    web_client = app.test_client()
    web_status = web_client.get("/api/status")
    web_verify = web_client.get("/api/verify")
    check(
        "flask_workbench_read_only",
        web_status.status_code == 200
        and web_verify.status_code == 200
        and web_verify.get_json()["verified"] is True,
        {"status": web_status.status_code, "verify": web_verify.status_code},
    )

    passed = all(item["passed"] for item in checks)
    summary = {
        "passed": passed,
        "check_count": len(checks),
        "failed_checks": [item["name"] for item in checks if not item["passed"]],
        "workspace": str(root),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "python_meets_declared_target": sys.version_info >= (3, 11),
            "numpy": version("numpy"),
            "flask": version("Flask"),
            "pytest": version("pytest"),
        },
        "checks": checks,
        "metrics": {
            **metrics,
            "v1_manifest_digest": v1_manifest,
            "v2_manifest_digest": v2["manifest_digest"],
            "incremental_hits": v2["summary"]["hit"],
            "incremental_writes": v2["summary"]["written"],
        },
    }
    text = json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
