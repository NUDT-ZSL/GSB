from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps
from .engine import compare_instance_results
from .errors import ResultMismatchError, WorkbenchError
from .storage import Storage


def load_ledger_file(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkbenchError("E_FILE_NOT_FOUND", f"ledger file not found: {path}", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError("E_JSON_INVALID", f"invalid ledger JSON: {exc.msg}", {"path": str(path), "line": exc.lineno}) from exc
    if not isinstance(value, dict) or "instances" not in value:
        raise WorkbenchError("E_LEDGER_INVALID", "ledger JSON must contain instances", {"path": str(path)})
    return value


def load_ledger_db(path: str | Path) -> dict[str, Any]:
    with Storage(path) as store:
        ledger = store.get_ledger()
    if ledger is None:
        raise WorkbenchError("E_LEDGER_EMPTY", "database has no committed ledger", {"db_path": str(path)})
    return ledger


def verify_sources(
    *,
    expected_path: str | Path | None,
    expected_db: str | Path | None = None,
    actual_paths: list[str | Path],
    actual_dbs: list[str | Path],
) -> dict[str, Any]:
    sources: list[tuple[str, dict[str, Any]]] = []
    if expected_path:
        expected = load_ledger_file(expected_path)
        baseline_name = str(expected_path)
    elif expected_db:
        expected = load_ledger_db(expected_db)
        baseline_name = f"db:{expected_db}"
    elif actual_paths or actual_dbs:
        first = actual_paths[0] if actual_paths else actual_dbs[0]
        expected = load_ledger_file(first) if actual_paths else load_ledger_db(first)
        baseline_name = str(first)
    else:
        raise WorkbenchError("E_VERIFY_ARGS", "verify requires --expected or at least one --actual/--actual-db", {})

    for path in actual_paths:
        sources.append((str(path), load_ledger_file(path)))
    for path in actual_dbs:
        sources.append((f"db:{path}", load_ledger_db(path)))
    if not expected_path and not expected_db and not actual_paths and not actual_dbs:
        raise WorkbenchError("E_VERIFY_ARGS", "no actual sources supplied", {})

    expected_canonical = canonical_dumps(expected)
    expected_instances = {item["instance_id"]: item for item in expected.get("instances", [])}
    verified = []
    for name, actual in sources:
        actual_canonical = canonical_dumps(actual)
        if actual_canonical != expected_canonical:
            actual_instances = {item["instance_id"]: item for item in actual.get("instances", [])}
            comparison = compare_instance_results(expected_instances, actual_instances)
            raise ResultMismatchError(
                "health ledger differs from reference",
                {
                    "reference": baseline_name,
                    "actual": name,
                    "first_instance_id": comparison["first_mismatch_instance_id"],
                    "first_observation_seq": comparison["first_mismatch_seq"],
                    "first_mismatch_instance": comparison["first_mismatch_instance_id"],
                    "first_seq": comparison["first_mismatch_seq"],
                    "mismatch_count": comparison["mismatch_count"],
                    "mismatches": comparison["mismatches"],
                    "expected_ledger_hash": expected.get("ledger_hash"),
                    "actual_ledger_hash": actual.get("ledger_hash"),
                },
            )
        verified.append({"source": name, "ledger_hash": actual.get("ledger_hash"), "canonical_sha256_match": True})

    return {
        "ok": True,
        "code": "OK",
        "reference": baseline_name,
        "expected_ledger_hash": expected.get("ledger_hash"),
        "verified": verified,
        "byte_order": "canonical JSON with sort_keys=True and compact separators",
    }
