"""Consistency verification across ledger, result file and trace mappings."""

from __future__ import annotations

import json
from pathlib import Path

from .operators import digest_records
from .pipeline import Pipeline
from .store import STATE_COMMITTED, Ledger


def verify(pipeline: Pipeline, input_records: list[dict], db_path: str | Path,
           out_path: str | Path) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, **detail):
        checks.append({"check": name, "ok": ok, **detail})

    ledger = Ledger(db_path)
    nodes = ledger.all_nodes()
    committed_ledger = {n["name"] for n in nodes if n["state"] == STATE_COMMITTED}
    out = Path(out_path)
    result_nodes: dict = {}
    if out.is_file():
        result_nodes = json.loads(out.read_text(encoding="utf-8")).get("nodes", {})
    committed_file = set(result_nodes)
    check(
        "ledger_results_same_committed_set",
        committed_ledger == committed_file,
        ledger_only=sorted(committed_ledger - committed_file),
        file_only=sorted(committed_file - committed_ledger),
    )
    bad_digests = []
    for name in sorted(committed_ledger & committed_file):
        row = ledger.get_node(name)
        entry = result_nodes[name]
        recomputed = digest_records(entry["records"])
        if not (row["digest"] == entry["digest"] == recomputed):
            bad_digests.append(name)
    check("digests_consistent", not bad_digests, nodes=bad_digests)
    # Trace mapping: every output record must map to its node and to input
    # records that actually exist (in the dataset or in committed parents).
    input_ids = {str(r.get("id")) for r in input_records}
    available_ids = set(input_ids)
    for name in sorted(committed_ledger & committed_file):
        available_ids |= {str(r.get("id")) for r in result_nodes[name]["records"]}
    trace_errors = []
    for name in sorted(committed_ledger & committed_file):
        entry = result_nodes[name]
        record_ids = {str(r.get("id")) for r in entry["records"]}
        for out_id, mapping in entry["trace"].items():
            if mapping.get("node") != name:
                trace_errors.append({"node": name, "output": out_id, "error": "wrong source node"})
            if out_id not in record_ids:
                trace_errors.append({"node": name, "output": out_id, "error": "trace target missing"})
            for src in mapping.get("inputs", []):
                if src not in available_ids:
                    trace_errors.append({"node": name, "output": out_id, "error": f"unknown input {src}"})
    check("trace_mapping_consistent", not trace_errors, errors=trace_errors)
    # Stage watermarks must be monotonic in the logical clock.
    clocks = [w["clock"] for w in ledger.watermarks()]
    check("watermarks_monotonic", clocks == sorted(clocks), clocks=clocks)
    # Every committed node's parents must be committed too.
    orphan = []
    for name in sorted(committed_ledger):
        for parent in pipeline.nodes[name].parents:
            if parent not in committed_ledger:
                orphan.append({"node": name, "parent": parent})
    check("committed_parents_committed", not orphan, violations=orphan)
    ledger.close()
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
