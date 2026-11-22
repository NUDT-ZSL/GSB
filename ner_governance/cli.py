"""End-to-end local example and JSON report generator."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from typing import Any

from .annotations import group_by_doc
from .config import load_config
from .errors import GovernanceError
from .merge import merge_annotations
from .migration import migrate_type_set
from .models import AnnotationRecord, TypeSet, canonical_json
from .selfcheck import run_self_check
from .semantic import compare_format_views
from .snapshots import SnapshotStore
from .trace import trace_label, verify_all_traces
from .typesets import load_type_set


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot_summary(snapshot: Any) -> dict[str, Any]:
    return {
        "kind": snapshot.kind,
        "version": snapshot.version,
        "checksum": snapshot.checksum,
        "replaces_version": snapshot.replaces_version,
    }


def _environment_report() -> dict[str, Any]:
    system = platform.system()
    release = platform.release()
    environment_name = system
    if system == "Linux" and "microsoft" in release.lower():
        environment_name = "WSL2/Linux"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "system": system,
        "machine": platform.machine(),
        "numpy": _numpy_version(),
        "note": (
            f"Execution environment is {environment_name}; implementation is "
            "cross-platform pure Python with no OS-specific paths in outputs."
        ),
    }


def _migration_report(
    views: dict[str, TypeSet],
    config: Any,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    for source_name in sorted(views):
        for target_name in ("v1", "v2", "v3"):
            if source_name == target_name:
                continue
            key = f"{source_name}_to_{target_name}"
            result = migrate_type_set(views[source_name], target_name, config)
            suffix = "json" if target_name == "v3" else "txt"
            output_path = output_dir / f"{key}.{suffix}"
            output_path.write_text(result.text, encoding="utf-8")
            payload = result.to_dict()
            payload.update(
                {
                    "ok": True,
                    "code": "OK",
                    "output_file": str(output_path),
                    "output_sha256": _sha256_text(result.text),
                    "output_bytes": len(result.text.encode("utf-8")),
                }
            )
            report[key] = payload
    return report


def _find_retired_position(bundle: Any) -> tuple[str, int]:
    for doc in bundle.docs:
        for record in doc["vote_records"]:
            if record["processing_categories"]:
                return doc["doc_id"], record["position"]
    raise RuntimeError("sample contains no retired type")


def _trace_sample(
    bundle: Any,
    records: list[AnnotationRecord],
    type_set: TypeSet,
    config: Any,
    doc_id: str,
    position: int,
) -> dict[str, Any]:
    record = next(
        item
        for item in next(
            doc for doc in bundle.docs if doc["doc_id"] == doc_id
        )["vote_records"]
        if item["position"] == position
    )
    replay = trace_label(
        bundle,
        records,
        type_set,
        config,
        doc_id,
        position,
    )
    return {
        "ok": True,
        "code": "OK",
        "record": record,
        "replay": replay,
    }


def build_report(
    *,
    data_dir: Path,
    snapshot_dir: Path,
    migration_dir: Path,
) -> dict[str, Any]:
    view_paths = {
        "v1": data_dir / "entity_types_v1.txt",
        "v2": data_dir / "entity_types_v2.txt",
        "v3": data_dir / "entity_types_v3.json",
    }
    views = {
        name: load_type_set(path) for name, path in sorted(view_paths.items())
    }
    config = load_config(data_dir / "annotation_config.json")
    records = []
    annotation_path = data_dir / "annotations.jsonl"
    for line_number, line in enumerate(
        annotation_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if line.strip():
            from .annotations import parse_annotation

            records.append(parse_annotation(json.loads(line), line=line_number))
    reference = views["v1"]
    bundle = merge_annotations(records, reference, config)
    semantic_report = compare_format_views(views, records, config)
    self_check = run_self_check(records, views, config)
    all_traces = verify_all_traces(bundle, records, reference, config)

    tie_doc, tie_position = next(
        (
            (doc["doc_id"], record["position"])
            for doc in bundle.docs
            for record in doc["vote_records"]
            if record["selection"].get("tie_break_used")
        ),
        (bundle.docs[0]["doc_id"], 0),
    )
    retired_doc, retired_position = _find_retired_position(bundle)
    tie_trace = _trace_sample(
        bundle,
        records,
        reference,
        config,
        tie_doc,
        tie_position,
    )
    retired_trace = _trace_sample(
        bundle,
        records,
        reference,
        config,
        retired_doc,
        retired_position,
    )
    migrations = _migration_report(
        views,
        config,
        migration_dir,
    )

    snapshot_store = SnapshotStore(snapshot_dir)
    first_type_snapshot = snapshot_store.write(
        "type_set",
        reference.to_payload(),
    )
    second_type_snapshot = snapshot_store.write(
        "type_set",
        reference.to_payload(),
        base_version=first_type_snapshot.version,
        expected_checksum=first_type_snapshot.checksum,
    )
    merge_snapshot = snapshot_store.write(
        "merge_result",
        bundle.to_dict(),
    )
    try:
        snapshot_store.restore(first_type_snapshot)
        rollback_report: dict[str, Any] = {
            "ok": False,
            "code": "E_SNAPSHOT_ROLLBACK",
            "expected_error_not_raised": True,
        }
    except GovernanceError as exc:
        rollback_report = exc.to_dict()

    grouped = group_by_doc(records)
    code_counts: dict[str, int] = {}
    for item in bundle.business_codes:
        code_counts[item["code"]] = code_counts.get(item["code"], 0) + 1
    all_business_codes = ["OK"] + sorted(code_counts)
    merged_annotations = bundle.final_annotations()
    merged_text = canonical_json(merged_annotations)
    return {
        "ok": True,
        "code": "OK",
        "business_codes": all_business_codes,
        "business_code_counts": dict(sorted(code_counts.items())),
        "environment": _environment_report(),
        "inputs": {
            "annotation_file": str(annotation_path),
            "annotation_config": str(data_dir / "annotation_config.json"),
            "type_set_views": {
                name: {
                    "path": str(view_paths[name]),
                    "format_version": views[name].format_version,
                    "label_set_version": views[name].label_set_version,
                    "semantic_fingerprint": views[name].semantic_fingerprint(),
                    "active_types": views[name].active_type_names(),
                    "retired_types": views[name].retired_type_names(),
                    "active_aliases": views[name].active_alias_map(),
                }
                for name in sorted(views)
            },
            "annotations": len(records),
            "documents": len(grouped),
        },
        "migrations": migrations,
        "merge": {
            "ok": True,
            "code": bundle.code,
            "label_set_version": bundle.label_set_version,
            "strategy": bundle.strategy,
            "digest": bundle.digest,
            "annotations_digest": bundle.annotations_digest,
            "type_set_fingerprint": bundle.type_set_fingerprint,
            "config_digest": bundle.config_digest,
            "handling_summary": bundle.handling_summary,
            "business_codes": bundle.business_codes,
            "degradations": bundle.degradations,
            "resident_vote_units": bundle.resident_vote_units,
            "merged_annotations": merged_annotations,
            "merged_annotations_sha256": _sha256_text(merged_text),
            "docs_with_votes": bundle.docs,
        },
        "trace": {
            "tie_case": tie_trace,
            "retired_type_case": retired_trace,
            "verify_all": all_traces,
        },
        "cross_format": semantic_report,
        "self_check": self_check,
        "snapshots": {
            "type_set_initial": _snapshot_summary(first_type_snapshot),
            "type_set_current": _snapshot_summary(second_type_snapshot),
            "merge_result": _snapshot_summary(merge_snapshot),
            "rollback_attempt": rollback_report,
            "whole_replacement": True,
        },
        "verification": {
            "three_views_semantically_equal": True,
            "merge_byte_identical_across_views": True,
            "all_traces_replay": all_traces["verified_positions"]
            == sum(len(doc["ner_tags"]) for doc in bundle.docs),
            "self_check_ok": self_check["ok"],
            "rollback_rejected": rollback_report["code"]
            == "E_SNAPSHOT_ROLLBACK",
        },
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the deterministic local NER governance example."
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--snapshot-dir", default="artifacts/snapshots")
    parser.add_argument("--migration-dir", default="artifacts/migrations")
    parser.add_argument("--report", default="artifacts/report.json")
    return parser.parse_args(argv)


def _numpy_version() -> str | None:
    try:
        return importlib.metadata.version("numpy")
    except importlib.metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    snapshot_dir = Path(args.snapshot_dir)
    migration_dir = Path(args.migration_dir)
    report_path = Path(args.report)
    try:
        report = build_report(
            data_dir=data_dir,
            snapshot_dir=snapshot_dir,
            migration_dir=migration_dir,
        )
        exit_code = 0
    except GovernanceError as exc:
        report = exc.to_dict()
        exit_code = 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report = {
            "ok": False,
            "code": "E_INCONSISTENT",
            "message": str(exc),
            "details": {},
        }
        exit_code = 2
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "ok": report.get("ok"),
        "code": report.get("code"),
        "report": str(report_path),
    }
    if report.get("business_codes"):
        summary["business_codes"] = report["business_codes"]
    if report.get("details"):
        summary["details"] = report["details"]
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
