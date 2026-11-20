from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from .manager import (
    explain_workspace,
    load_workspace,
    resume_workspace,
    run_migration,
    status_workspace,
    verify_workspace,
)
from .util import canonical_json, digest_json, pretty_json


ROOT = Path(__file__).resolve().parent.parent


def _run_one(workspace: Path) -> Dict[str, Any]:
    if workspace.exists():
        shutil.rmtree(workspace)
    transcript: List[Dict[str, Any]] = []
    transcript.append(
        load_workspace(
            workspace,
            ROOT / "samples/schemas.json",
            ROOT / "samples/migrations.json",
            ROOT / "samples/queries.json",
        )
    )
    crash = subprocess.run(
        [
            sys.executable,
            "-m",
            "schema_migrator",
            "--workspace",
            str(workspace),
            "migrate",
            "--migration",
            "m001",
            "--failpoint",
            "alter_column_type",
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    transcript.append(
        {
            "step": "forced_termination",
            "exit_code": crash.returncode,
            "forced": crash.returncode == 91,
        }
    )
    interrupted_status = status_workspace(workspace)
    transcript.append(
        {
            "step": "status_after_termination",
            "current_version": interrupted_status["current_version"],
            "pending": interrupted_status["pending_journal"],
        }
    )
    transcript.append(resume_workspace(workspace))
    transcript.append(run_migration(workspace, "m002"))
    transcript.append(run_migration(workspace, "m003"))
    transcript.append(status_workspace(workspace))
    transcript.append(explain_workspace(workspace, "q_order_totals", migration_id="m001"))
    transcript.append(verify_workspace(workspace))
    return {
        "transcript": transcript,
        "digest": digest_json(transcript),
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m schema_migrator.demo")
    parser.add_argument("--base", default=".demo-runs")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    base = (ROOT / args.base).resolve() if not Path(args.base).is_absolute() else Path(args.base)
    run_a = _run_one(base / "run_a")
    run_b = _run_one(base / "run_b")
    identical = run_a["digest"] == run_b["digest"]
    output = {
        "ok": identical,
        "command": "demo",
        "runs": 2,
        "identical": identical,
        "transcript_digest": run_a["digest"],
        "run_digests": [run_a["digest"], run_b["digest"]],
        "scenario": {
            "versions": [1, 2, 3, 4],
            "migrations": ["m001", "m002", "m003"],
            "forced_termination": "alter_column_type",
            "recovery": "resume",
        },
        "summary": {
            "final_version": run_a["transcript"][5]["current_version"],
            "verify_ok": run_a["transcript"][-1]["ok"],
            "recovered": run_a["transcript"][3]["resumed"],
            "template_statuses": {
                item["id"]: item["status"]
                for item in run_a["transcript"][5]["report"]["templates"]
            },
        },
    }
    if args.verbose:
        output["transcript"] = run_a["transcript"]
    sys.stdout.write(pretty_json(output))
    sys.stdout.write("\n")
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())

