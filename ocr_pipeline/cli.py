"""Command-line demo and consistency report entry point."""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

from .cache import RecognitionCache
from .chunking import assert_byte_identical, run_chunked, run_whole_page
from .detection import detect_lines
from .engine import OCREngine
from .errors import PipelineError
from .metrics import compute_metrics
from .recognition import RecognitionParams, collect_norm_stats
from .sample import PageSample, build_task_row, generate_page
from .snapshots import SnapshotStore
from .templates import build_library
from .util import (
    array_hash,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)
from .validation import (
    validate_full_page,
    validate_invalidation_sets,
    validate_query_vs_page,
)


def _load_config(path: str) -> dict:
    return read_json(path)


def _ensure_dirs(config: dict, root: str) -> dict[str, str]:
    dirs = {}
    for name, relative in config["paths"].items():
        absolute = os.path.join(root, relative)
        os.makedirs(absolute, exist_ok=True)
        dirs[name] = absolute
    return dirs


def _validate_task_row(row: dict) -> None:
    required = {"page_id", "image_seed", "image_shape", "line_count",
                "charset_version", "policy"}
    missing = required - set(row)
    if missing:
        from .errors import InvalidInputError
        raise InvalidInputError(
            "task row missing required fields",
            {"page_id": row.get("page_id"), "missing": sorted(missing)},
        )
    if not isinstance(row["image_seed"], int) or row["image_seed"] < 0:
        from .errors import InvalidInputError
        raise InvalidInputError(
            "image_seed must be a non-negative integer",
            {"page_id": row["page_id"], "image_seed": row["image_seed"]},
        )
    if row["line_count"] <= 0:
        from .errors import InvalidInputError
        raise InvalidInputError(
            "line_count must be positive",
            {"page_id": row["page_id"], "line_count": row["line_count"]},
        )


def run_demo(config_path: str, root: str) -> dict:
    config = _load_config(config_path)
    dirs = _ensure_dirs(config, root)
    report: dict = {"steps": [], "errors": []}

    def record(step: str, payload: dict) -> None:
        report["steps"].append({"step": step, "result": payload})

    gen = config["generation"]
    library = build_library(gen["initial_charset_version"],
                            gen["initial_library_version"])
    params = RecognitionParams(**config["recognition"]["params"])
    cache = RecognitionCache()
    store = SnapshotStore(dirs["snapshot_dir"])

    # 1. Deterministic sample generation + task manifest.
    pages: list[PageSample] = []
    task_rows = []
    for index in range(gen["page_count"]):
        seed = gen["base_seed"] + index * 101
        page = generate_page(
            f"page-{index:03d}", seed, library,
            shape=tuple(gen["image_shape"]), line_count=gen["line_count"],
        )
        pages.append(page)
        row = build_task_row(page, gen["initial_charset_version"], "standard")
        _validate_task_row(row)
        task_rows.append(row)
        np.save(
            os.path.join(dirs["samples_dir"], f"{page.page_id}.npy"), page.image
        )
        write_json(
            os.path.join(dirs["samples_dir"], f"{page.page_id}.truth.json"),
            page.to_json(),
        )
    manifest_path = os.path.join(dirs["samples_dir"], "tasks.jsonl")
    write_jsonl(manifest_path, task_rows)
    record("generate_samples", {
        "pages": len(pages),
        "manifest": manifest_path,
        "image_hashes": [array_hash(page.image) for page in pages],
    })

    # 2. Snapshot template library + params (versioned, checksummed).
    # Monotonic versions across repeated demo runs on an existing snapshot dir.
    lib_version = _next_version(store, "template_library")
    param_version = _next_version(store, "recognition_params")
    lib_env = store.commit("template_library", lib_version, library.to_json())
    param_env = store.commit("recognition_params", param_version, params.to_json())
    record("commit_snapshots", {
        "template_library": {"version": lib_env["version"],
                             "checksum": lib_env["checksum"]},
        "recognition_params": {"version": param_env["version"],
                               "checksum": param_env["checksum"]},
    })

    # 3. Whole-page vs chunked byte identity + engine processing + cache.
    engine = OCREngine(library, params, cache)
    page_results = {}
    cache_reports = {}
    for page in pages:
        result, cache_report = engine.process_page(
            page,
            chunk_size=config["recognition"]["chunk_size"],
            latency_budget_ms=config["recognition"]["latency_budget_ms"],
            memory_ceiling_bytes=config["recognition"]["memory_ceiling_bytes"],
        )
        page_results[page.page_id] = result
        cache_reports[page.page_id] = cache_report
        # Independent direct cross-check in addition to engine assertions.
        detections = detect_lines(page.image)
        norm = collect_norm_stats(page.image, detections)
        whole = run_whole_page(page.image, detections, library, params, norm)
        chunked, _ = run_chunked(
            page.image, detections, library, params, norm,
            config["recognition"]["chunk_size"],
        )
        assert_byte_identical(page.page_id, whole, chunked)

    page_payload = {
        page_id: result.payload() for page_id, result in page_results.items()
    }
    result_version = _next_version(store, "page_results")
    result_env = store.commit("page_results", result_version, page_payload)
    record("process_pages", {
        "page_count": len(page_results),
        "page_results_version": result_env["version"],
        "page_results_checksum": result_env["checksum"],
        "cache_reports": cache_reports,
    })

    # 4. Second run: identical inputs must be fully served from cache.
    second_reports = {}
    for page in pages:
        _, cache_report = engine.process_page(
            page,
            chunk_size=config["recognition"]["chunk_size"],
            latency_budget_ms=config["recognition"]["latency_budget_ms"],
            memory_ceiling_bytes=config["recognition"]["memory_ceiling_bytes"],
        )
        second_reports[page.page_id] = cache_report
    record("cache_reuse_run", second_reports)

    # 5. Library upgrade: targeted invalidation, recompute vs reused partition.
    upgraded = build_library(gen["initial_charset_version"],
                             gen["upgraded_library_version"])
    before_keys = {entry.key for entry in cache.all_entries()}
    removed = cache.invalidate(
        library_versions={library.library_version},
        library_checksums={library.checksum},
    )
    upgraded_engine = OCREngine(upgraded, params, cache)
    upgrade_reports = {}
    for page in pages:
        _, cache_report = upgraded_engine.process_page(
            page,
            chunk_size=config["recognition"]["chunk_size"],
            latency_budget_ms=config["recognition"]["latency_budget_ms"],
            memory_ceiling_bytes=config["recognition"]["memory_ceiling_bytes"],
        )
        upgrade_reports[page.page_id] = cache_report
        affected = list(range(len(page.truth)))
        validate_invalidation_sets(
            page.page_id, affected,
            cache_report["recompute_lines"], cache_report["reused_lines"],
        )
    after_keys = {entry.key for entry in cache.all_entries()}

    # Independent proof that a template-library version change alters scoring
    # while remaining traceable (same pixels, different checksums/scores).
    score_trace = _score_version_trace(pages[0], library, upgraded, params)
    record("library_upgrade_invalidation", {
        "removed_keys": removed,
        "removed_count": len(removed),
        "before_key_count": len(before_keys),
        "after_key_count": len(after_keys),
        "partition": upgrade_reports,
        "score_version_trace": score_trace,
    })

    # 6. Interactive query within budget.
    query_cfg = config["query"]
    query_page = pages[query_cfg["page_index"]]
    query_result = engine.query_line(
        query_page, query_cfg["line_no"],
        latency_budget_ms=query_cfg["latency_budget_ms"],
    )
    v1_detections = detect_lines(query_page.image)
    v1_norm = collect_norm_stats(query_page.image, v1_detections)
    v1_page_records = run_whole_page(
        query_page.image, v1_detections, library, params, v1_norm
    )
    validate_query_vs_page(
        query_page.page_id, query_result, v1_page_records,
        precision=query_cfg["precision"],
    )
    record("interactive_query", query_result)

    # 7. Over-budget query (real timing, zero budget => guaranteed overrun).
    over_cfg = config["over_budget_query"]
    try:
        engine.query_line(
            pages[over_cfg["page_index"]], over_cfg["line_no"],
            latency_budget_ms=over_cfg["latency_budget_ms"],
        )
    except PipelineError as exc:
        report["errors"].append(exc.to_dict())
        record("over_budget_query", exc.to_dict())

    # 8. Injected anomalous input.
    anomaly = config["injected_anomaly"]
    try:
        _validate_task_row(anomaly)
    except PipelineError as exc:
        report["errors"].append(exc.to_dict())
        record("injected_anomaly", exc.to_dict())

    # 9. Snapshot reload + corruption demonstration.
    loaded, changed = store.reload_if_changed(
        "page_results", previous=None
    )
    corruption_demo = _demo_corrupt_snapshot(dirs["snapshot_dir"])
    record("snapshot_lifecycle", {
        "reload_changed": changed,
        "loaded_version": loaded.version,
        "corruption_demo": corruption_demo,
    })

    # 10. Full independent validation and report.
    # Re-populate v1 cache entries (upgrade step invalidated them) so the
    # independent cache-coherence check has matching v1 keys to verify.
    for page in pages:
        engine.process_page(
            page,
            chunk_size=config["recognition"]["chunk_size"],
            latency_budget_ms=config["recognition"]["latency_budget_ms"],
            memory_ceiling_bytes=config["recognition"]["memory_ceiling_bytes"],
        )
    validation_summary = [
        validate_full_page(
            page, library, params,
            config["recognition"]["chunk_size"], cache,
        )
        for page in pages
    ]
    report["consistency_report"] = {
        "status": "PASS",
        "pages": validation_summary,
        "metrics": {
            page.page_id: compute_metrics(
                run_whole_page(
                    page.image, detect_lines(page.image), library, params,
                    collect_norm_stats(page.image, detect_lines(page.image)),
                ),
                page.truth,
            )
            for page in pages
        },
    }
    report_path = os.path.join(dirs["report_dir"], "consistency_report.json")
    write_json(report_path, report)
    return report


def _score_version_trace(page, library_a, library_b, params) -> dict:
    """Same pixels scored against two library versions; scores must change."""
    from .recognition import recognize_line
    detections = detect_lines(page.image)
    norm_a = collect_norm_stats(page.image, detections)
    record_a = recognize_line(page.image, detections[0], library_a, params, norm_a)
    # Re-normalize with library B's context; pixels are identical.
    norm_b = collect_norm_stats(page.image, detections)
    record_b = recognize_line(page.image, detections[0], library_b, params, norm_b)
    return {
        "page_id": page.page_id,
        "library_a": {"version": library_a.library_version,
                      "checksum": library_a.checksum,
                      "first_line_confidence": record_a["confidence"]},
        "library_b": {"version": library_b.library_version,
                      "checksum": library_b.checksum,
                      "first_line_confidence": record_b["confidence"]},
        "scores_differ": record_a["candidates"] != record_b["candidates"],
    }


def _next_version(store: SnapshotStore, category: str) -> int:
    path = store.path_for(category)
    if not os.path.exists(path):
        return 1
    try:
        loaded = store.load(category)
        return loaded.version + 1
    except Exception:
        # Corrupt pre-existing file must be replaced wholesale, not patched.
        return 1


def _demo_corrupt_snapshot(snapshot_dir: str) -> dict:
    """Write a deliberately corrupt snapshot and assert whole-file rejection."""
    from .errors import SnapshotCorruptError
    demo_store = SnapshotStore(snapshot_dir)
    corrupt_path = demo_store.path_for("recognition_params")
    previous = demo_store.load("recognition_params")
    try:
        with open(corrupt_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"category":"recognition_params","version":2,"payload":')
        try:  # corrupt/partial data must be rejected, never partially parsed
            demo_store.load("recognition_params")
            outcome = {"status": "UNEXPECTED_PASS"}
        except SnapshotCorruptError as exc:
            outcome = exc.to_dict()
    finally:
        # Restore via a new, strictly higher version (whole-snapshot replacement).
        demo_store.commit(
            "recognition_params", previous.version + 1, previous.payload
        )
    return outcome


def run_version_check() -> dict:
    return {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "python_ok": sys.version_info >= (3, 10),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic OCR pipeline demo")
    parser.add_argument(
        "command", choices=["version", "demo", "validate-manifest"]
    )
    parser.add_argument("--config", default="config/demo_config.json")
    parser.add_argument("--root", default=os.getcwd())
    args = parser.parse_args(argv)

    if args.command == "version":
        print(_print(run_version_check()))
        return 0
    if args.command == "demo":
        try:
            report = run_demo(args.config, args.root)
        except PipelineError as exc:
            print(_print(exc.to_dict()), file=sys.stderr)
            return 2
        print(_print({"status": report["consistency_report"]["status"],
                      "steps": len(report["steps"]),
                      "captured_expected_errors": len(report["errors"])}))
        return 0
    if args.command == "validate-manifest":
        rows = read_jsonl(os.path.join(args.root, "data/samples/tasks.jsonl"))
        for row in rows:
            _validate_task_row(row)
        print(_print({"valid_rows": len(rows), "status": "PASS"}))
        return 0
    return 1


def _print(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
