#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bsl_workbench.checkpoint import (  # noqa: E402
    verify_checkpoint,
    write_checkpoint_document,
    write_snapshot,
)
from bsl_workbench.generator import generate_tree  # noqa: E402
from bsl_workbench.ledger import Ledger  # noqa: E402
from bsl_workbench.parser import ParseOptions, parse_file  # noqa: E402
from bsl_workbench.revision import compute_invalidation  # noqa: E402
from bsl_workbench.util import atomic_write_json, stable_json_dumps  # noqa: E402


def main() -> int:
    sample_root = ROOT / "samples" / "generated"
    manifest = generate_tree(sample_root)

    sample_results = []
    failures = []
    for relative, meta in manifest["files"].items():
        options = ParseOptions(**meta.get("options", {}))
        report = parse_file(
            sample_root / relative,
            file_id=meta.get("file_id", "acceptance"),
            options=options,
        )
        codes = report["summary"]["error_codes"]
        expected = meta.get("expect_codes", [])
        ok = all(code in codes for code in expected) and (
            bool(expected) or not codes
        )
        sample_results.append(
            {
                "file": relative,
                "expected_codes": expected,
                "actual_codes": codes,
                "ok": ok,
            }
        )
        if not ok:
            failures.append(relative)

    old = parse_file(
        sample_root / "revisions/v1/revision_demo.bsl",
        file_id="revision_demo",
    )
    new = parse_file(
        sample_root / "revisions/v2/revision_demo.bsl",
        file_id="revision_demo",
    )
    invalidation = compute_invalidation(old, new, file_id="revision_demo")
    revision_ok = invalidation["recompute_segments"] == [
        "/base",
        "/mid",
        "/top",
    ]
    if not revision_ok:
        failures.append("revision-closure")

    with tempfile.TemporaryDirectory(prefix="bsl-acceptance-") as temp_dir:
        workdir = Path(temp_dir)
        with Ledger(workdir / "ledger.sqlite3") as ledger:
            completed: dict = {}
            checkpoint = None
            for letter in ("a", "b"):
                report = parse_file(
                    sample_root / f"recovery/recovery_{letter}.bsl",
                    file_id=f"recovery_{letter}",
                )
                checkpoint = ledger.commit_file_report(
                    report,
                    source_path=f"recovery_{letter}.bsl",
                    completed_files=completed,
                )
                completed = checkpoint["completed_files"]
                write_checkpoint_document(checkpoint, workdir)
                write_snapshot(checkpoint, workdir)
            checkpoint_verification = verify_checkpoint(workdir, ledger=ledger)
        checkpoint_ok = checkpoint_verification["ok"]
        if not checkpoint_ok:
            failures.append("checkpoint")

    result = {
        "schema_version": "bsl-acceptance-v1",
        "ok": not failures,
        "failure_targets": failures,
        "sample_count": len(sample_results),
        "samples": sample_results,
        "revision": {
            "recompute_segments": invalidation["recompute_segments"],
            "changed_segments": invalidation["changed_segments"],
        },
        "checkpoint": {
            "ok": checkpoint_ok,
            "latest_sequence": checkpoint_verification["latest_sequence"],
            "ledger_digest": checkpoint_verification["ledger_digest"],
        },
    }
    output = ROOT / "artifacts" / "acceptance-report.json"
    atomic_write_json(output, result)
    print(stable_json_dumps({"ok": result["ok"], "report": str(output)}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
