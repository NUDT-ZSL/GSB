#!/usr/bin/env python3.11
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def run_command(
    repo: Path,
    python: str,
    db: Path,
    args: list[str],
    transcript: list[str],
    *,
    label: str,
    clocks: dict[str, int],
    env_extra: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    command = [python, "-m", "eventbus", *args, "--db", str(db)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        command,
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    transcript.append(f"### {label}\n")
    if completed.stdout:
        transcript.append(completed.stdout)
    if completed.stderr:
        transcript.append(completed.stderr)
    transcript.append(f"### exit {completed.returncode}\n")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command did not emit JSON: {label}: {exc}: {completed.stdout!r}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {label}: {completed.stdout}")
    return parsed, completed.stdout


def run_scenario(
    repo: Path,
    python: str,
    run_dir: Path,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    db = run_dir / "bus.db"
    clocks = scenario["clocks"]
    seed = int(scenario["seed"])
    transcript: list[str] = []

    load_result, _ = run_command(
        repo,
        python,
        db,
        ["load", "--samples", "samples", "--clock", str(clocks["load"])],
        transcript,
        label="load",
        clocks=clocks,
    )
    run_command(
        repo,
        python,
        db,
        ["bridge", "--clock", str(clocks["bridge"])],
        transcript,
        label="bridge",
        clocks=clocks,
    )

    rng = random.Random(seed)
    for round_index in range(64):
        status, _ = run_command(
            repo,
            python,
            db,
            ["status"],
            transcript,
            label=f"status-before-consume-{round_index}",
            clocks=clocks,
        )
        lag_by_consumer = {
            item["consumer_id"]: int(item["lag"])
            for item in status["subscriptions"]
        }
        active = [
            consumer_id
            for consumer_id, lag in lag_by_consumer.items()
            if lag > 0
        ]
        if not active:
            break
        rng.shuffle(active)
        for consumer_id in active:
            run_command(
                repo,
                python,
                db,
                [
                    "consume",
                    "--consumer",
                    consumer_id,
                    "--clock",
                    str(clocks["consume_ledger"]),
                ],
                transcript,
                label=f"consume-{round_index}-{consumer_id}",
                clocks=clocks,
            )
    else:
        raise RuntimeError("consumer loop did not converge")

    run_command(
        repo,
        python,
        db,
        ["cleanup", "--clock", str(clocks["cleanup"])],
        transcript,
        label="cleanup",
        clocks=clocks,
    )
    final_status, status_text = run_command(
        repo,
        python,
        db,
        ["status"],
        transcript,
        label="status-final",
        clocks=clocks,
    )
    status_file = run_dir / "status-final.json"
    status_file.write_text(status_text, encoding="utf-8")
    verify_result, _ = run_command(
        repo,
        python,
        db,
        [
            "verify",
            "--samples",
            "samples",
            "--status-file",
            str(status_file),
        ],
        transcript,
        label="verify",
        clocks=clocks,
    )
    resume_result, _ = run_command(
        repo,
        python,
        db,
        ["resume"],
        transcript,
        label="resume",
        clocks=clocks,
    )
    transcript_path = run_dir / "transcript.txt"
    transcript_bytes = "".join(transcript).encode("utf-8")
    transcript_path.write_bytes(transcript_bytes)
    return {
        "appended_on_load": int(load_result["appended"]),
        "db": str(db),
        "final_state_digest": final_status["state_digest"],
        "resume_pending": int(resume_result["pending_remaining"]),
        "sample_digest": verify_result["sample_digest"],
        "transcript": str(transcript_path),
        "transcript_bytes": len(transcript_bytes),
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
        "verify_ok": bool(verify_result["ok"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed sample scenario twice and compare every byte."
    )
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", default="out/repeat")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    output = (repo / args.output).resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    scenario = json.loads(
        (repo / "samples" / "scenario.json").read_text(encoding="utf-8")
    )
    if int(scenario.get("seed", -1)) != 149:
        raise RuntimeError("scenario seed must be the fixed value 149")

    first = run_scenario(repo, args.python, output / "run1", scenario)
    second = run_scenario(repo, args.python, output / "run2", scenario)
    first_bytes = Path(first["transcript"]).read_bytes()
    second_bytes = Path(second["transcript"]).read_bytes()
    identical = first_bytes == second_bytes
    summary = {
        "byte_identical": identical,
        "first": first,
        "ok": bool(identical and first["verify_ok"] and second["verify_ok"]),
        "second": second,
        "seed": scenario["seed"],
    }
    summary_path = output / "summary.json"
    summary_path.write_text(
        canonical_json(summary) + "\n", encoding="utf-8"
    )
    sys.stdout.write(canonical_json(summary) + "\n")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

