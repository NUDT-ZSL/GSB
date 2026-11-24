#!/usr/bin/env python3
"""Run from repository root: python examples/library_usage.py."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sbom_audit.engine import AuditEngine, Batch
from sbom_audit.merge import build_integrity_report
from sbom_audit.parser import parse_dependency_file, parse_sync_map, parse_v1_file, parse_v2_snapshot


def main() -> None:
    sample = Path("samples/generated")
    if not sample.exists():
        from sbom_audit.samplegen import generate_sample

        generate_sample(sample)

    v1 = parse_v1_file(sample / "v1_snapshot.json")
    v2 = parse_v2_snapshot(sample / "v2")
    edges, _, _ = parse_dependency_file(sample / "dependencies.json")
    sync = parse_sync_map(sample / "sync_markers.json")

    report = build_integrity_report(v1.records + v2.records, edges, sync_markers=sync)
    print("full closure:", report["closure"])

    ledger = Path(tempfile.mkdtemp(prefix="sbom-library-")) / "ledger.sqlite3"
    engine = AuditEngine(ledger, sync)
    engine.apply_batch(Batch("deps", edges=edges))
    engine.apply_batch(Batch("v1", records=v1.records))
    result = engine.apply_batch(Batch("v2", records=v2.records))
    print("incremental equivalence:", result.get("incremental"))


if __name__ == "__main__":
    main()
