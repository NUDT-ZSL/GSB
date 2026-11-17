"""End-to-end integration example for the KLP audit component."""

from __future__ import annotations

import json
from pathlib import Path

from klp_audit import AuditConfig, AuditEngine, generate_samples


def main() -> int:
    root = Path("artifacts/demo")
    manifest_path = generate_samples(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    legal_sample = root / manifest["files"]["legal"]["sample"]
    report = AuditEngine(
        AuditConfig(
            concurrency=2,
            session_timeout_seconds=30.0,
            max_derivations_per_session=50_000,
            scale_bytes_threshold=1_048_576,
            scale_derived_threshold=10_000,
        ),
        checkpoint_path=root / "checkpoints" / "demo.checkpoint.json",
        workspace=root,
    ).audit_file(legal_sample)

    summary = {
        "conclusion": report["file"]["conclusion"],
        "sessions": report["summary"]["sessions"],
        "adaptive_mode": report["adaptive"]["mode"],
        "checkpoint": report["checkpoint"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["file"]["conclusion"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

