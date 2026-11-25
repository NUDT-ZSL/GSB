import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def cli_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def run_cli(
    *arguments: str,
    check: bool = False,
) -> Tuple[subprocess.CompletedProcess, Optional[Any]]:
    completed = subprocess.run(
        [sys.executable, "-m", "ckptmgr", *arguments],
        cwd=str(REPO_ROOT),
        env=cli_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            "CLI failed with {0}: stdout={1!r} stderr={2!r}".format(
                completed.returncode, completed.stdout, completed.stderr
            )
        )
    payload = None
    raw = completed.stdout if completed.stdout.strip() else completed.stderr
    if raw.strip():
        payload = json.loads(raw)
    return completed, payload


def sample_paths(base: Path) -> Dict[str, str]:
    from scripts.generate_sample import generate

    data = base / "data"
    generate(data)
    return {
        "plan": str(data / "plan.json"),
        "strategy": str(data / "strategy.json"),
        "save_plan": str(data / "checkpoint-manifest.json"),
        "crash_write": str(data / "crash-write.json"),
        "crash_commit": str(data / "crash-commit.json"),
    }


def plan_args(paths: Dict[str, str]) -> List[str]:
    return [
        "--plan",
        paths["plan"],
        "--strategy",
        paths["strategy"],
        "--save-plan",
        paths["save_plan"],
    ]


def save_cmd(root: Path, paths: Dict[str, str], version: int, clock: int, mode: str = "auto") -> List[str]:
    return [
        "save",
        "--root",
        str(root),
        *plan_args(paths),
        "--version",
        str(version),
        "--clock-ms",
        str(clock),
        "--mode",
        mode,
    ]
