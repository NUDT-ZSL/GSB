from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

import numpy as np

from .config import SnapshotStore
from .errors import BusinessError
from .facade import (
    judge_equivalence,
    load_format,
    migrate,
    run_self_check,
    verify_roundtrip,
)
from .input import ResourceLimits, load_annotated_samples
from .models import FormatVersion
from .streaming import stream_migrate_samples


def _details_excerpt(exc: BusinessError) -> dict[str, Any]:
    details = exc.details
    keys = (
        "field",
        "line",
        "sent_id",
        "token_id",
        "category",
        "format_version",
        "expected",
        "actual",
        "unrestorable_fields",
        "tokens",
    )
    return {key: details[key] for key in keys if key in details}


def _run_case(
    case: dict[str, Any],
    *,
    root: Path,
    store: SnapshotStore,
) -> dict[str, Any]:
    kind = case["kind"]
    path = root / case["path"] if "path" in case else None
    snapshot = store.current
    try:
        if kind == "format":
            load_format(path, snapshot)
        elif kind == "raw":
            load_annotated_samples(path, snapshot)
        elif kind == "resource":
            limits_payload = case["limits"]
            load_annotated_samples(
                path,
                snapshot,
                ResourceLimits(
                    max_total_nodes=limits_payload["max_total_nodes"],
                    max_tokens_per_sentence=limits_payload["max_tokens_per_sentence"],
                    mode="strict",
                ),
            )
        elif kind == "equivalence":
            judge_equivalence(
                {
                    version: root / source_path
                    for version, source_path in case["sources"].items()
                },
                snapshot,
            )
        elif kind == "roundtrip":
            verify_roundtrip(
                path,
                snapshot,
                confidence_tolerance=case.get("confidence_tolerance"),
            )
        elif kind == "snapshot":
            SnapshotStore().load(path)
        elif kind == "retirement":
            local_store = SnapshotStore()
            local_store.load(root / "config" / "controlled_snapshot_v1.json")
            existing = [
                load_format(
                    root / "samples" / "arcs_v1.txt",
                    local_store.current,
                ).graph
            ]
            local_store.load(path, existing_graphs=existing)
        elif kind == "selfcheck":
            run_self_check(
                {
                    version: root / source_path
                    for version, source_path in case["sources"].items()
                },
                store,
            )
        else:
            raise ValueError(f"unknown injection case kind: {kind}")
    except BusinessError as exc:
        expected = case["expected_code"]
        return {
            "name": case["name"],
            "kind": kind,
            "expected_code": expected,
            "actual_code": exc.code.value,
            "passed": exc.code.value == expected,
            "message": exc.message,
            "details": _details_excerpt(exc),
        }
    return {
        "name": case["name"],
        "kind": kind,
        "expected_code": case["expected_code"],
        "actual_code": None,
        "passed": False,
        "message": "case did not raise a BusinessError",
        "details": {},
    }


def run_demo(data_root: str | Path) -> dict[str, Any]:
    root = Path(data_root).resolve()
    store = SnapshotStore()
    trace_v1 = store.load(root / "config" / "controlled_snapshot_v1.json")
    trace_v2 = store.load(root / "config" / "controlled_snapshot_v2.json")
    snapshot = store.current
    loaded = {
        "v1": load_format(root / "samples" / "arcs_v1.txt", snapshot),
        "v2": load_format(root / "samples" / "arcs_v2.txt", snapshot),
        "v3": load_format(root / "samples" / "arcs_v3.json", snapshot),
    }
    equivalence = judge_equivalence(loaded, snapshot)
    migrations: list[dict[str, Any]] = []
    seen_versions = (FormatVersion.V1, FormatVersion.V2, FormatVersion.V3)
    for source_version in seen_versions:
        for target_version in seen_versions:
            result = migrate(
                loaded[source_version.value],
                target_version,
                snapshot,
            )
            migrations.append(
                {
                    "source_version": source_version.value,
                    "target_version": target_version.value,
                    "fill_count": len(result.fills),
                    "drop_count": len(result.drops),
                    "replay_differences": len(result.replay_differences()),
                    "fill_fields": sorted({item.field for item in result.fills}),
                    "drop_fields": sorted({item.field for item in result.drops}),
                }
            )
    rich_v3 = load_format(root / "injection" / "semantic_drift_v3.json", snapshot)
    rich_downgrade = migrate(rich_v3, "v1", snapshot)
    rich_downgrade_fields = sorted({item.field for item in rich_downgrade.drops})
    roundtrips = {
        version: verify_roundtrip(
            root
            / "samples"
            / ("arcs_v3.json" if version == "v3" else f"arcs_{version}.txt"),
            snapshot,
        ).to_dict()
        for version in ("v1", "v2", "v3")
    }
    raw_loaded = load_annotated_samples(
        root / "samples" / "dependency_samples.jsonl",
        snapshot,
        ResourceLimits(max_total_nodes=10_000, max_tokens_per_sentence=512),
    )
    try:
        load_annotated_samples(
            root / "samples" / "dependency_samples.jsonl",
            snapshot,
            ResourceLimits(max_total_nodes=20, max_tokens_per_sentence=9),
        )
        strict_resource_result: dict[str, Any] = {
            "passed": False,
            "message": "strict resource limit did not raise",
        }
    except BusinessError as exc:
        strict_resource_result = {
            "passed": exc.code.value == "E_RESOURCE_LIMIT",
            "code": exc.code.value,
            "details": _details_excerpt(exc),
        }
    streamed = stream_migrate_samples(
        root / "samples" / "dependency_samples.jsonl",
        "v2",
        snapshot,
        limits=ResourceLimits(
            mode="stream",
            max_total_nodes=20,
            max_tokens_per_sentence=9,
        ),
    )
    baseline_by_sent = {
        graph.sent_ids()[0]: graph.sentences[graph.sent_ids()[0]].core_signature()
        for graph in raw_loaded.graphs
    }
    stream_arcs_preserved = all(
        chunk.migration.graph.sentences[chunk.sent_id].core_signature()
        == baseline_by_sent[chunk.sent_id]
        for chunk in streamed.chunks
    )
    case_payload = json.loads(
        (root / "injection" / "cases.json").read_text(encoding="utf-8")
    )
    injection_results = [
        _run_case(case, root=root, store=store) for case in case_payload["cases"]
    ]
    selfcheck = run_self_check(
        {
            "v1": root / "samples" / "arcs_v1.txt",
            "v2": root / "samples" / "arcs_v2.txt",
            "v3": root / "samples" / "arcs_v3.json",
        },
        store,
    )
    all_passed = all(
        (
            all(item["replay_differences"] == 0 for item in migrations),
            all(item["passed"] for item in injection_results),
            all(result["consistent"] for result in roundtrips.values()),
            stream_arcs_preserved,
            strict_resource_result.get("passed", False),
            all(check["status"] == "pass" for check in selfcheck.checks),
        )
    )
    return {
        "workspace": str(root),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.system(),
        },
        "seed": case_payload["seed"],
        "snapshots": {
            "current_versions": {
                "pos_tags": snapshot.pos_tags.version,
                "deprels": snapshot.deprels.version,
                "migration_policy": snapshot.policy.version,
            },
            "first_load": trace_v1.to_dict(),
            "second_load": trace_v2.to_dict(),
            "history": [trace.to_dict() for trace in store.history],
        },
        "formats": {
            version: document.to_dict() for version, document in loaded.items()
        },
        "equivalence": {
            "versions": list(equivalence.versions),
            "sent_ids": equivalence.sent_ids,
            "arc_count": equivalence.arc_count,
            "reconstructed_field_count": len(equivalence.reconstructed_fields),
        },
        "migrations": migrations,
        "rich_v3_downgrade": {
            "drop_count": len(rich_downgrade.drops),
            "drop_fields": rich_downgrade_fields,
            "replay_differences": len(rich_downgrade.replay_differences()),
        },
        "roundtrips": roundtrips,
        "raw_samples": raw_loaded.to_dict(),
        "resource_limit": {
            "strict": strict_resource_result,
            "streaming": streamed.to_dict(),
            "stream_arcs_preserved": stream_arcs_preserved,
        },
        "injection_results": injection_results,
        "selfcheck": selfcheck.to_dict(),
        "all_passed": all_passed,
    }


def write_report(report: dict[str, Any], path: str | Path) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    Path(path).write_text(payload, encoding="utf-8", newline="\n")
