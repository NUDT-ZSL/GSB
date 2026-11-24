"""Run the local acceptance checks and write a reproducible result artifact."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smuggling_engine.checkpoint import CheckpointManager
from smuggling_engine.engine import Engine, EngineConfig
from smuggling_engine.generator import generate_sample, verify_report_against_truth
from smuggling_engine.parsing import group_connections, parse_messages
from smuggling_engine.rules import load_rule_view


def _analyze_sample(directory: Path):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    paths = manifest["paths"]
    messages, rejects = parse_messages(directory / paths["messages"])
    groups, _ = group_connections(messages)
    view = load_rule_view(
        v1_path=directory / paths["rules_v1"],
        v2_shards=[directory / item for item in paths["v2_shards"]],
        v2_database=directory / paths["v2_database"],
    )
    return manifest, groups, rejects, view


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "validation.json"))
    parser.add_argument("--skip-tests", action="store_true")
    arguments = parser.parse_args(argv)

    pytest_result = {"skipped": True}
    if not arguments.skip_tests:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        pytest_result = {
            "skipped": False,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        pytest_result["test_count"] = sum(
            int(match.group(1))
            for match in re.finditer(r":\s*(\d+)\s*$", collected.stdout, re.MULTILINE)
        )
        if completed.returncode != 0:
            print(completed.stdout)
            print(completed.stderr, file=sys.stderr)
            return completed.returncode

    sample_directory = ROOT / "samples" / "default"
    manifest = generate_sample(sample_directory, seed=20261125)
    _, groups, rejects, view = _analyze_sample(sample_directory)
    engine = Engine(EngineConfig(concurrency=4, per_connection_timeout_ms=5000))
    full = engine.run(groups, view, rejected=rejects)
    full_dict = full.to_dict()
    (sample_directory / "report.json").write_text(
        json.dumps(full_dict, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    truth = json.loads(
        (sample_directory / "sync_truth.json").read_text(encoding="utf-8")
    )
    verification = verify_report_against_truth(full_dict, truth)
    (sample_directory / "verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    baseline_view = load_rule_view(
        v1_path=sample_directory / manifest["paths"]["rules_v1"]
    )
    baseline = engine.run(groups, baseline_view, rejected=rejects)
    incremental_ok, incremental, fresh = engine.verify_incremental(
        groups, view, baseline, rejected=rejects
    )

    checkpoint_root = Path(tempfile.mkdtemp(prefix="smuggling-checkpoint-"))
    checkpoint = CheckpointManager(checkpoint_root)
    prefix = engine.run_with_checkpoint(
        groups[:3],
        view,
        checkpoint,
        run_identity=manifest["sample_id"],
        rejected=rejects,
    )
    resumed = engine.run_with_checkpoint(
        groups,
        view,
        checkpoint,
        run_identity=manifest["sample_id"],
        rejected=rejects,
    )
    checkpoint_ok = resumed.deterministic_dict() == full.deterministic_dict()

    result = {
        "pytest": pytest_result,
        "sample": {
            "directory": str(sample_directory.relative_to(ROOT)),
            "seed": manifest["seed"],
            "message_count": manifest["message_count"],
            "truth_sha256": manifest["truth_sha256"],
        },
        "sync_truth_verification": verification,
        "summary": full_dict["summary"],
        "closure": full_dict["closure"],
        "incremental": {
            "byte_identical_to_full": incremental_ok
            and incremental.deterministic_dict()
            == fresh.deterministic_dict(),
            "recomputed_connections": incremental.metadata[
                "recomputed_connections"
            ],
            "reused_connections": incremental.metadata["reused_connections"],
        },
        "checkpoint": {
            "prefix_connection_count": len(prefix.connections),
            "resumed": resumed.metadata["checkpoint"],
            "byte_identical_to_full": resumed.deterministic_dict()
            == full.deterministic_dict(),
        },
    }
    output = Path(arguments.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if not verification["ok"] or not incremental_ok or not checkpoint_ok:
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

