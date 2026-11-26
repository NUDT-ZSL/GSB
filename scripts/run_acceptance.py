#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quant_tuner.sample_data import generate_all


def run_cli(args: list[str]) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "quant_tuner.cli", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def wait_for(path: Path, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def versions(output: Path) -> list[int]:
    result = []
    for path in (output / "checkpoints").glob("checkpoint-*.json"):
        result.append(int(path.stem.split("-")[-1]))
    return sorted(result)


def checkpoint(output: Path, version: int) -> dict:
    return json.loads(
        (output / "checkpoints" / f"checkpoint-{version:06d}.json").read_text("utf-8")
    )


def main() -> int:
    paths = generate_all(ROOT / "samples")
    output = ROOT / "runs" / "acceptance"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    config = json.loads(Path(paths["config"]).read_text("utf-8"))
    config["arbitration"].update(
        {"worker_count": 3, "max_concurrency": 3, "timing_repeats": 4000, "warmup_repeats": 0}
    )
    kill_config = ROOT / "runs" / "acceptance-kill-config.json"
    kill_config.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", "utf-8")

    stdout_log = ROOT / "runs" / "acceptance-child.stdout.log"
    stderr_log = ROOT / "runs" / "acceptance-child.stderr.log"
    stdout = stdout_log.open("w", encoding="utf-8")
    stderr = stderr_log.open("w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [
            sys.executable, "-m", "quant_tuner.cli", "run",
            "--tasks", paths["tasks"],
            "--platform", paths["platform"],
            "--config", str(kill_config),
            "--output", str(output),
        ],
        cwd=ROOT,
        env=env,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        text=True,
    )

    inflight = output / "work" / "batch-000002.inflight"
    return_code = None
    try:
        if not wait_for(inflight):
            raise RuntimeError(f"missing batch 2 marker; stderr={stderr_log.read_text()}")
        deadline = time.time() + 5
        while time.time() < deadline and not list((output / "staging").glob("*.partial.json")):
            time.sleep(0.01)
        time.sleep(0.05)
        os.killpg(child.pid, signal.SIGKILL)
        return_code = child.wait(timeout=10)
    finally:
        stdout.close()
        stderr.close()

    if return_code == 0:
        raise RuntimeError("killed child unexpectedly exited zero")
    if versions(output) != [1]:
        raise RuntimeError(f"expected v1 only after kill, got {versions(output)}")

    before = checkpoint(output, 1)
    before_ns = {
        item["point_id"]: item["measured"]["elapsed_ns_median"]
        for item in before["payload"]["results"]
    }
    staged = sorted(str(p.relative_to(output)) for p in (output / "staging").glob("*.json"))

    resume = run_cli([
        "run", "--resume",
        "--tasks", paths["tasks"], "--platform", paths["platform"],
        "--config", str(kill_config), "--output", str(output),
    ])

    propagation_path = output / "propagation.json"
    cache_path = output / "quant-cache.json"
    cache_seed = run_cli([
        "cache-summaries", "--tasks", paths["tasks"], "--cache", str(cache_path)
    ])
    propagation = run_cli([
        "propagate",
        "--old-tasks", paths["tasks"], "--new-tasks", paths["revised_tasks"],
        "--old-v2", paths["v2"], "--new-v2", paths["v2_revised"],
        "--output", str(propagation_path), "--cache", str(cache_path),
    ])
    verification = run_cli([
        "verify", "--output", str(output),
        "--tasks", paths["tasks"], "--platform", paths["platform"],
        "--config", str(kill_config), "--propagation", str(propagation_path),
    ])

    final_versions = versions(output)
    final = checkpoint(output, final_versions[-1])
    after_ns = {
        item["point_id"]: item["measured"]["elapsed_ns_median"]
        for item in final["payload"]["results"]
    }
    preserved = {pid: before_ns[pid] == after_ns.get(pid) for pid in before_ns}
    if not all(preserved.values()):
        raise RuntimeError(f"committed timings changed: {preserved}")
    if final_versions != [1, 2, 3, 4]:
        raise RuntimeError(f"bad final versions {final_versions}")
    if list((output / "work").glob("*.inflight")):
        raise RuntimeError("inflight marker remains")

    result = {
        "ok": True,
        "signal": "SIGKILL",
        "child_return_code": return_code,
        "versions_after_kill": [1],
        "staged_files_observed": staged,
        "batch1_point_count": len(before_ns),
        "batch1_timings_preserved": preserved,
        "resume": resume,
        "propagation": propagation,
        "cache_seed": cache_seed,
        "verification": verification,
    }
    result_path = output / "acceptance-result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", "utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"ACCEPTANCE PASSED: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
