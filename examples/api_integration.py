#!/usr/bin/env python3
"""Programmatic end-to-end integration example.

The example generates an isolated sample group, starts one loopback node per
replica, resolves truth through JSON-lines TCP, repairs all mismatches, writes a
JSON+SQLite ledger, and verifies a whole-view reopen.
"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from replica_ledger import (  # noqa: E402
    LedgerStore,
    NodeServer,
    analyze_remote,
    build_ledger_payload,
    diagnose_grid,
    execute_repairs,
    generate_samples,
    load_replica_group,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="replica-ledger-api-") as temp:
        root = Path(temp)
        generate_samples(root / "samples", force=True)
        spec_path = root / "samples/scenarios/single/spec.json"
        spec = load_replica_group(spec_path)
        with ExitStack() as stack:
            servers = {
                node.node_id: stack.enter_context(
                    NodeServer(
                        spec,
                        node.node_id,
                        port=0,
                        log_path=root / "logs" / f"{node.node_id}.jsonl",
                    )
                )
                for node in sorted(spec.nodes, key=lambda item: item.node_id)
            }
            endpoints = {node_id: server.endpoint for node_id, server in servers.items()}
            before = analyze_remote(spec, endpoints, 8)
            diagnosis = diagnose_grid(spec, [8, 16])
            repaired_spec, execution = execute_repairs(spec, before.plan, endpoints)
            after = analyze_remote(repaired_spec, endpoints, 8)
        payload = build_ledger_payload(
            before=before,
            after=after,
            execution=execution,
            grid_diagnostics=diagnosis,
        )
        store = LedgerStore(root / "ledger.json", root / "ledger.sqlite")
        store.save(payload)
        reopened = store.open(expected_group_state_digest=after.group_state_digest)
        summary = {
            "group_id": spec.group_id,
            "before_mismatch": {
                block_id: list(decision.mismatch_nodes)
                for block_id, decision in sorted(before.consensus.decisions.items())
            },
            "repair_tasks": len(before.plan.tasks),
            "source_selection_flips": [
                event["event_id"]
                for event in diagnosis["flips"]
                if event["kind"] == "SOURCE_SELECTION_CHANGED"
            ],
            "after_all_mismatch_sets_empty": all(
                decision.mismatch_count == 0
                for decision in after.consensus.decisions.values()
            ),
            "ledger_reopen_status": reopened.status,
            "ledger_content_digest": reopened.content_digest,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
