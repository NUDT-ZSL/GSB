"""Reproducible fault-injection cases for every required business error."""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from .budget import plan_admission
from .comparison import compare_paths
from .config import load_config
from .errors import (
    E_CHAR_LEN_MISMATCH,
    E_COMPARE_BUDGET,
    E_CONTEXT_RANGE_INVALID,
    E_DUP_DOC,
    E_FEATURE_UNKNOWN,
    E_INDEX_INCONSISTENT,
    E_PATH_DIVERGENCE,
    E_RESOURCE_LIMIT,
    E_RULE_VERSION_UNKNOWN,
    E_SENSE_OUT_OF_RANGE,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_TERM_ENTRY_INVALID,
    E_WEIGHT_NEGATIVE,
    PipelineError,
)
from .pipeline import run_pipeline
from .runtime import load_committed_runtime
from .snapshots import SnapshotStore
from .util import read_json, write_json_atomic


EXPECTED_CODES = {
    "dup_doc": E_DUP_DOC,
    "char_len": E_CHAR_LEN_MISMATCH,
    "term_entry_invalid": E_TERM_ENTRY_INVALID,
    "context_range": E_CONTEXT_RANGE_INVALID,
    "weight_negative": E_WEIGHT_NEGATIVE,
    "feature_unknown": E_FEATURE_UNKNOWN,
    "sense_out_of_range": E_SENSE_OUT_OF_RANGE,
    "rule_version_unknown": E_RULE_VERSION_UNKNOWN,
    "compare_budget": E_COMPARE_BUDGET,
    "resource_limit": E_RESOURCE_LIMIT,
    "path_divergence": E_PATH_DIVERGENCE,
    "snapshot_rollback": E_SNAPSHOT_ROLLBACK,
    "snapshot_corrupt": E_SNAPSHOT_CORRUPT,
    "index_inconsistent": E_INDEX_INCONSISTENT,
}


def _config_template(root: Path, temp: Path) -> dict[str, Any]:
    loaded = load_config(root / "config.json")
    raw = read_json(root / "config.json")
    raw = copy.deepcopy(raw)
    raw["paths"] = {
        "shards": list(loaded["_resolved_paths"]["shards"]),
        "glossary": loaded["_resolved_paths"]["glossary"],
        "context_features": loaded["_resolved_paths"]["context_features"],
        "rules_dir": loaded["_resolved_paths"]["rules_dir"],
        "snapshot_dir": str(temp / "snapshots"),
        "out_dir": str(temp / "out"),
    }
    return raw


def _prepare(root: Path, temp: Path) -> tuple[dict[str, Any], Callable[[], Path]]:
    raw = _config_template(root, temp)

    def write_config() -> Path:
        path = temp / "config.json"
        write_json_atomic(path, raw)
        return path

    return raw, write_config


def _copy_json(path: Path, target: Path) -> None:
    value = read_json(path)
    write_json_atomic(target, value)


def _run_case(case: str, root: Path, temp: Path) -> None:
    raw, write_config = _prepare(root, temp)
    config_path = write_config()

    if case in {"dup_doc", "char_len"}:
        shard_path = Path(raw["paths"]["shards"][0])
        rows = [
            json.loads(line)
            for line in shard_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if case == "dup_doc":
            rows.append(copy.deepcopy(rows[0]))
        else:
            rows[0]["char_len"] += 1
        target = temp / shard_path.name
        target.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        raw["paths"]["shards"][0] = str(target)
        write_config()
        run_pipeline(config_path)
        return

    if case in {"term_entry_invalid"}:
        target = temp / "glossary.json"
        _copy_json(Path(raw["paths"]["glossary"]), target)
        value = read_json(target)
        value["entries"][0]["term"] = 123
        write_json_atomic(target, value)
        raw["paths"]["glossary"] = str(target)
        write_config()
        run_pipeline(config_path)
        return

    if case in {"context_range", "weight_negative"}:
        target = temp / "context_features.json"
        _copy_json(Path(raw["paths"]["context_features"]), target)
        value = read_json(target)
        if case == "context_range":
            value["observations"][0]["end"] = 10**9
        else:
            value["catalog"][0]["weight"] = -0.25
        write_json_atomic(target, value)
        raw["paths"]["context_features"] = str(target)
        write_config()
        run_pipeline(config_path)
        return

    if case in {"feature_unknown", "sense_out_of_range", "rule_version_unknown"}:
        if case == "rule_version_unknown":
            raw["rule_version"] = "rules-not-present"
            write_config()
            run_pipeline(config_path)
            return
        target_dir = temp / "rules"
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in Path(raw["paths"]["rules_dir"]).glob("*.json"):
            _copy_json(source, target_dir / source.name)
        selected = raw.get("rule_version", "rules-v1")
        target = target_dir / f"{selected}.json"
        value = read_json(target)
        if case == "feature_unknown":
            value["rules"].append(
                {
                    "rule_id": "R_INJECTED_UNKNOWN_FEATURE",
                    "term_id": "T_PROTOCOL",
                    "sense_id": 1,
                    "feature_id": "F_NOT_IN_CATALOG",
                    "weight": 1.0,
                }
            )
        else:
            value["rules"][0]["sense_id"] = 999
        write_json_atomic(target, value)
        raw["paths"]["rules_dir"] = str(target_dir)
        write_config()
        run_pipeline(config_path)
        return

    if case in {"compare_budget", "resource_limit"}:
        if case == "compare_budget":
            raw["budget"]["total_compare_budget"] = 1
        else:
            raw["budget"]["total_compare_budget"] = 10**9
            raw["budget"]["max_resident_cache_mb"] = 0
        write_config()
        run_pipeline(config_path)
        return

    if case == "path_divergence":
        raw["adaptive"]["exact_max_shards"] = 1
        raw["adaptive"]["approx_max_candidates_per_doc"] = 1
        raw["adaptive"]["approx_max_features_per_candidate"] = 1
        write_config()
        run_pipeline(config_path)
        runtime = load_committed_runtime(config_path)
        compare_paths(
            runtime["documents"],
            runtime["scored_documents"],
            runtime["glossary"],
            runtime["rules"],
            runtime["features"],
            runtime["path_decision"],
            runtime["config"],
            strict=True,
        )
        return

    if case == "snapshot_rollback":
        write_config()
        run_pipeline(config_path)
        store = SnapshotStore(temp / "snapshots")
        store.commit(
            0,
            {"documents": []},
            [],
            base_fingerprint="injected",
            forced_version=1,
        )
        return

    if case == "snapshot_corrupt":
        write_config()
        run_pipeline(config_path)
        store = SnapshotStore(temp / "snapshots")
        path = store.path_for(1)
        value = read_json(path)
        value["payload"]["documents"][0]["candidates"][0]["term"] = "CORRUPTED"
        write_json_atomic(path, value)
        run_pipeline(config_path)
        return

    if case == "index_inconsistent":
        write_config()
        run_pipeline(config_path)
        database = temp / "out" / "ledger.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "DELETE FROM disambiguations WHERE rowid = (SELECT MIN(rowid) FROM disambiguations)"
            )
            connection.commit()
        finally:
            connection.close()
        run_pipeline(config_path)
        return

    raise ValueError(f"unknown injection case: {case}")


def inject_error(case: str, root: str | Path) -> dict[str, Any]:
    if case not in EXPECTED_CODES:
        raise ValueError(f"unknown injection case: {case}")
    root_path = Path(root).resolve()
    expected = EXPECTED_CODES[case]
    with tempfile.TemporaryDirectory(prefix=f"term-inject-{case}-") as temp_text:
        temp = Path(temp_text)
        try:
            _run_case(case, root_path, temp)
        except PipelineError as exc:
            return {
                "case": case,
                "expected": expected,
                "observed": exc.code,
                "ok": exc.code == expected,
                "message": exc.message,
                "details": exc.details,
            }
        except Exception as exc:  # pragma: no cover - diagnostic path
            return {
                "case": case,
                "expected": expected,
                "observed": "INTERNAL_ERROR",
                "ok": False,
                "message": str(exc),
            }
    return {
        "case": case,
        "expected": expected,
        "observed": None,
        "ok": False,
        "message": "expected error was not raised",
    }


def budget_degradation_example(
    root: str | Path,
    *,
    total_compare_budget: int,
    max_resident_cache_mb: float,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix="term-budget-example-") as temp_text:
        temp = Path(temp_text)
        raw, write_config = _prepare(root_path, temp)
        raw["budget"]["policy"] = "report"
        raw["budget"]["total_compare_budget"] = total_compare_budget
        raw["budget"]["max_resident_cache_mb"] = max_resident_cache_mb
        config_path = write_config()
        run_pipeline(config_path)
        runtime = load_committed_runtime(config_path)
        return plan_admission(
            runtime["documents"],
            runtime["scored_documents"],
            runtime["features"],
            runtime["config"],
            policy="report",
        )

