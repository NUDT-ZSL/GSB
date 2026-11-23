from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_ledger import AuditEngine, AuditStore, load_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="audit/embedded-example.db")
    parser.add_argument("--samples", default="samples")
    args = parser.parse_args()

    samples = Path(args.samples)
    bundle = load_bundle(
        samples / "events.json",
        samples / "quotas.json",
        samples / "policy.json",
    )
    store = AuditStore(args.db)
    load_report = store.load_bundle(bundle)
    write_report = AuditEngine(store).write()
    status = store.status()
    verification = store.verify(
        json.loads((samples / "expected_summary.json").read_text("utf-8"))
    )
    output = {
        "load": load_report,
        "write_summary": write_report["summary"],
        "status": status,
        "verify_ok": verification["ok"],
        "verify_mismatches": verification["mismatches"],
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if verification["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
