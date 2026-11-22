#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PYTHON = sys.executable


def cli(
    root: Path,
    *args: str,
    stdin: str | None = None,
    timeout: int = 40,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [
            PYTHON,
            "-m",
            "config_center",
            "--root",
            str(root),
            "--data-dir",
            str(DATA),
            *args,
        ],
        cwd=ROOT,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def cli_async(root: Path, *args: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [
            PYTHON,
            "-m",
            "config_center",
            "--root",
            str(root),
            "--data-dir",
            str(DATA),
            *args,
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def parse_stdout(process: subprocess.CompletedProcess[str]) -> Any:
    return json.loads(process.stdout) if process.stdout.strip() else None


def error_probe(root: Path, label: str, *args: str) -> dict[str, Any]:
    process = cli(root, *args)
    payload = parse_stdout(process)
    error = (payload or {}).get("error", {})
    return {
        "label": label,
        "exit_code": process.returncode,
        "code": error.get("code"),
        "message": error.get("message"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="可重复演示热更新与数值恢复")
    parser.add_argument(
        "--root",
        default=str(ROOT),
        help="工作区根目录；SQLite 状态写入 <root>/state",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(ROOT))
    from config_center.service import ConfigCenter
    from config_center.util import atomic_write_json
    from config_center.verify import verify_center

    center = ConfigCenter(root, data_dir=DATA)
    center.reset()
    center.load_file(DATA / "snapshot.json")
    sample_input = json.loads((DATA / "compute_input.json").read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = []

    def record(name: str, status: str, **payload: Any) -> None:
        steps.append({"step": name, "status": status, **payload})
        print(f"[{status}] {name}")

    v1_result = center.compute_document(sample_input)
    record(
        "compute-v1",
        "ok",
        bound_version=v1_result["bound_version"],
        precision_sensitive=v1_result["result"]["sensitivity"][
            "precision_sensitive"
        ],
        result_digest=v1_result["result"]["result_digest"],
    )
    print(
        "  v1 threshold fixed/exact:",
        v1_result["result"]["metrics"]["threshold"]["fixed"],
        "/",
        v1_result["result"]["metrics"]["threshold"]["exact"],
    )

    crashed = cli(
        root,
        "compute",
        str(DATA / "compute_input.json"),
        "--round-key",
        "round-crash",
        "--crash-at",
        "before-commit",
    )
    record(
        "crash-before-commit",
        "expected-exit",
        exit_code=crashed.returncode,
        pending_jobs=[
            {
                "client_id": job["client_id"],
                "round_key": job["round_key"],
                "bound_version": job["bound_version"],
            }
            for job in center.store.list_pending_jobs("edge-pipeline")
        ],
    )

    patch2 = center.apply_file(DATA / "patches" / "002_add_weight_gamma.json")
    record(
        "apply-v2",
        "ok",
        version=patch2["version"],
        change_kind=patch2["change_kind"],
        parent_version=patch2["parent_version"],
    )

    delayed = cli_async(
        root,
        "compute",
        str(DATA / "compute_input.json"),
        "--round-key",
        "round-hot",
        "--delay-ms",
        "1000",
    )
    deadline = time.time() + 6
    bound_job = None
    while time.time() < deadline:
        pending = center.store.list_pending_jobs("edge-pipeline")
        bound_job = next(
            (job for job in pending if job["round_key"] == "round-hot"),
            None,
        )
        if bound_job is not None:
            break
        time.sleep(0.05)
    if bound_job is None:
        delayed.kill()
        raise RuntimeError("delayed compute did not bind a version")

    patch3 = center.apply_file(DATA / "patches" / "003_full_replace_same_values.json")
    delayed_stdout, delayed_stderr = delayed.communicate(timeout=20)
    if delayed.returncode != 0:
        raise RuntimeError(f"delayed compute failed: {delayed_stderr}")
    delayed_payload = json.loads(delayed_stdout)
    record(
        "hot-update-during-compute",
        "ok",
        applied_version_while_running=patch3["version"],
        compute_bound_version=delayed_payload["bound_version"],
        result_bound_version=delayed_payload["result"]["bound_version"],
        expected_old_version=2,
    )

    resumed = center.resume(namespace="edge-pipeline")
    record(
        "resume-pending",
        "ok",
        resumed_count=resumed["resumed_count"],
        pending_after=resumed["pending_after"],
        resumed=resumed["resumed"],
    )

    v3_result = center.compute_document(sample_input, round_key="round-v3")
    worker_b = center.compute_document(
        sample_input,
        client_id="worker-b",
        round_key="round-v3",
    )
    record(
        "compute-v3",
        "ok",
        worker_a_bound_version=v3_result["bound_version"],
        worker_b_bound_version=worker_b["bound_version"],
        v3_precision_sensitive=v3_result["result"]["sensitivity"][
            "precision_sensitive"
        ],
    )

    comparison = center.compare_versions(
        "edge-pipeline",
        1,
        3,
        sample_input,
        tolerance=Decimal("0.001"),
        scale_factor=100,
    )
    threshold_row = next(
        row
        for row in comparison["comparison"]["metrics"]
        if row["metric"] == "threshold"
    )
    record(
        "compare-v1-v3",
        "ok",
        precision_sensitive=comparison["comparison"]["precision_sensitive"],
        threshold_fixed_delta=threshold_row["fixed_delta"],
        threshold_exact_delta=threshold_row["exact_delta"],
        threshold_conclusion_changed=threshold_row["conclusion_changed"],
        higher_scale_flips=comparison["higher_scale"]["flips_conclusion"],
        higher_scale_explanation=comparison["higher_scale"]["explanation"],
    )

    client_comparison = center.compare_clients(
        "edge-pipeline",
        "worker-a",
        "round-v3",
        "worker-b",
        "round-v3",
        tolerance=Decimal("0.001"),
    )
    record(
        "compare-clients",
        "ok",
        precision_sensitive=client_comparison["comparison"][
            "precision_sensitive"
        ],
        left_digest=client_comparison["left"]["result_digest"],
        right_digest=client_comparison["right"]["result_digest"],
    )

    verification = verify_center(
        center,
        namespace="edge-pipeline",
        tolerance=Decimal("0.001"),
        scale_factor=100,
    )
    record(
        "verify",
        "ok" if verification["passed"] else "failed",
        summary=verification["summary"],
        checks=[
            {
                "name": check["name"],
                "status": check["status"],
                "code": check["code"],
            }
            for check in verification["checks"]
        ],
    )

    error_codes = [
        error_probe(
            root,
            "negative-clock",
            "load",
            str(DATA / "error_cases" / "bad_clock.json"),
        ),
        error_probe(
            root,
            "type-mismatch",
            "load",
            str(DATA / "error_cases" / "bad_type.json"),
        ),
        error_probe(
            root,
            "digest-mismatch",
            "apply",
            str(DATA / "error_cases" / "bad_digest_patch.json"),
        ),
        error_probe(
            root,
            "version-rollback",
            "apply",
            str(DATA / "patches" / "002_add_weight_gamma.json"),
        ),
    ]
    record(
        "business-error-codes",
        "ok"
        if [item["code"] for item in error_codes]
        == [
            "E_BAD_CLOCK",
            "E_BAD_CONFIG",
            "E_DIGEST_MISMATCH",
            "E_VERSION_ROLLBACK",
        ]
        else "failed",
        errors=error_codes,
    )

    status = center.status("edge-pipeline")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": {
            "python": sys.version.split()[0],
            "root": str(root),
            "data_dir": str(DATA),
            "sqlite_state": str(root / "state" / "config_center.db"),
        },
        "steps": steps,
        "current_version": status["current_version"],
        "logical_clock_watermark": status["logical_clock_watermark"],
        "precision_sensitive_count": status["precision_sensitive_count"],
        "clients": status["clients"],
        "pending_jobs": status["pending_jobs"],
        "comparison": {
            "from_version": comparison["from_version"],
            "to_version": comparison["to_version"],
            "tolerance": comparison["tolerance"],
            "precision_sensitive": comparison["comparison"][
                "precision_sensitive"
            ],
            "metrics": comparison["comparison"]["metrics"],
            "higher_scale": comparison["higher_scale"],
        },
        "client_comparison": {
            "precision_sensitive": client_comparison["comparison"][
                "precision_sensitive"
            ],
            "metrics": client_comparison["comparison"]["metrics"],
        },
        "verification": verification,
    }
    output = ROOT / "artifacts" / "verification.json"
    atomic_write_json(output, report)
    print(f"\nReport written to {output}")
    print(
        f"Final: v{status['current_version']}, "
        f"clock watermark {status['logical_clock_watermark']}, "
        f"precision-sensitive rounds {status['precision_sensitive_count']}, "
        f"pending jobs {len(status['pending_jobs'])}"
    )
    return 0 if verification["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

