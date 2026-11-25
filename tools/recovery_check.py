#!/usr/bin/env python3
"""Run a continuous pipeline and a SIGKILL/resume pipeline, then compare outputs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from term_pipeline.config import load_config
from term_pipeline.ledger import ledger_checksum
from term_pipeline.util import sha256_bytes


def _config_for_run(source_config: Path, target: Path) -> Path:
    loaded = load_config(source_config)
    raw = json.loads(source_config.read_text(encoding="utf-8"))
    raw["paths"] = {
        "shards": list(loaded["_resolved_paths"]["shards"]),
        "glossary": loaded["_resolved_paths"]["glossary"],
        "context_features": loaded["_resolved_paths"]["context_features"],
        "rules_dir": loaded["_resolved_paths"]["rules_dir"],
        "snapshot_dir": str(target / "snapshots"),
        "out_dir": str(target / "out"),
    }
    target.mkdir(parents=True, exist_ok=True)
    path = target / "config.json"
    path.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _run(config_path: Path, crash_stage: int | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "main.py",
        "run",
        "--config",
        str(config_path),
    ]
    if crash_stage is not None:
        command.extend(["--crash-after-stage", str(crash_stage)])
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def check(root: Path, config: Path, stage: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="term-recovery-baseline-") as baseline_text:
        with tempfile.TemporaryDirectory(prefix="term-recovery-resume-") as resume_text:
            baseline_dir = Path(baseline_text)
            resume_dir = Path(resume_text)
            baseline_config = _config_for_run(config, baseline_dir)
            resume_config = _config_for_run(config, resume_dir)

            continuous = _run(baseline_config)
            if continuous.returncode != 0:
                raise RuntimeError(
                    f"continuous run failed: {continuous.stdout} {continuous.stderr}"
                )
            killed = _run(resume_config, stage)
            if killed.returncode == 0:
                raise RuntimeError("fault injection did not terminate the process")
            resumed = _run(resume_config)
            if resumed.returncode != 0:
                raise RuntimeError(
                    f"resumed run failed: {resumed.stdout} {resumed.stderr}"
                )

            baseline_index = baseline_dir / "out" / "term_index.json"
            resumed_index = resume_dir / "out" / "term_index.json"
            baseline_bytes = baseline_index.read_bytes()
            resumed_bytes = resumed_index.read_bytes()
            baseline_ledger = ledger_checksum(
                baseline_dir / "out" / "ledger.sqlite3"
            )["checksum"]
            resumed_ledger = ledger_checksum(
                resume_dir / "out" / "ledger.sqlite3"
            )["checksum"]
            snapshot_versions = [
                json.loads(path.read_text(encoding="utf-8"))["version"]
                for path in sorted((resume_dir / "snapshots").glob("*.json"))
            ]
            result = {
                "ok": (
                    baseline_bytes == resumed_bytes
                    and baseline_ledger == resumed_ledger
                ),
                "crash_stage": stage,
                "crash_returncode": killed.returncode,
                "index_byte_identical": baseline_bytes == resumed_bytes,
                "index_sha256": sha256_bytes(resumed_bytes),
                "ledger_logical_checksum_identical": baseline_ledger
                == resumed_ledger,
                "ledger_logical_checksum": resumed_ledger,
                "snapshot_versions": snapshot_versions,
            }
            return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--stage", type=int, choices=(1, 2, 3, 4, 5), default=3)
    args = parser.parse_args()
    try:
        result = check(
            PROJECT_ROOT,
            Path(args.config).resolve(),
            args.stage,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

