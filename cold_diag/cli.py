from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .runner import run_scenario, summarize_result
from .sample_data import generate_sample
from .utils import write_json


def _command_generate(args: argparse.Namespace) -> int:
    paths = generate_sample(args.data_dir, seed=args.seed)
    print(json.dumps(paths, ensure_ascii=False, indent=2))
    return 0


def _command_run(args: argparse.Namespace) -> int:
    result = run_scenario(
        args.data_dir,
        db_path=args.db,
        crash_before_commit=args.crash_before_commit,
    )
    summary = summarize_result(result)
    payload = {
        "summary": summary,
        "state": result["state"],
        "policy_view": result["policy_view"],
    }
    if args.output:
        write_json(args.output, payload)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    result["engine"].close()
    return 0


def _command_crash_demo(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "cold_diag.cli",
            "run",
            "--data-dir",
            args.data_dir,
            "--db",
            str(db_path),
            "--crash-before-commit",
            args.step,
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    print(f"crashed_process_returncode={crashed.returncode}")
    if crashed.returncode not in (-9, 137):
        print(crashed.stdout)
        print(crashed.stderr, file=sys.stderr)
        raise RuntimeError("crash subprocess did not exit with SIGKILL")

    recovered = run_scenario(args.data_dir, db_path=db_path)
    summary = summarize_result(recovered)
    output = Path(args.output)
    write_json(
        output,
        {
            "crash_step": args.step,
            "crash_returncode": crashed.returncode,
            "recovery": summary["recovery_report"],
            "summary": summary,
            "state": recovered["state"],
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    recovered["engine"].close()
    return 0


def _command_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("cold_diag.api:app", host=args.host, port=args.port, reload=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serverless cold-start diagnostic engine")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-samples", help="generate deterministic data/ samples")
    generate.add_argument("--data-dir", default="data")
    generate.add_argument("--seed", type=int, default=20261127)
    generate.set_defaults(func=_command_generate)

    run = sub.add_parser("run", help="run and persist the deterministic scenario")
    run.add_argument("--data-dir", default="data")
    run.add_argument("--db", default="artifacts/diagnostic.sqlite")
    run.add_argument("--output", default="artifacts/run_result.json")
    run.add_argument("--crash-before-commit", default=None)
    run.set_defaults(func=_command_run)

    crash = sub.add_parser("crash-demo", help="SIGKILL a step, recover, and emit report")
    crash.add_argument("--data-dir", default="data")
    crash.add_argument("--db", default="artifacts/crash-recovery.sqlite")
    crash.add_argument("--step", default="request:2")
    crash.add_argument("--output", default="artifacts/crash_recovery_result.json")
    crash.set_defaults(func=_command_crash_demo)

    serve = sub.add_parser("serve", help="start FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
