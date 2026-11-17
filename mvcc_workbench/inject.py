"""Input-file-only anomaly injection for acceptance and tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .engine import Workbench
from .errors import E_CACHE, E_CHAIN, E_IO, E_SNAPSHOT, E_TXN, BusinessError
from .util import atomic_write_json, canonical_json, load_json, sha256_text


INJECTION_CODES = {
    "chain": E_CHAIN,
    "cache": E_CACHE,
    "txn": E_TXN,
}


def _payload(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key != "checksum"}


def inject_snapshot(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    kind: str,
) -> dict[str, Any]:
    if kind not in INJECTION_CODES:
        raise BusinessError(
            "E_INPUT",
            "unknown anomaly injection kind",
            {"kind": kind, "supported": sorted(INJECTION_CODES)},
        )
    try:
        data = load_json(source)
    except (OSError, ValueError) as exc:
        raise BusinessError(E_IO, "cannot read source snapshot for injection", {
            "source": str(source), "error": str(exc)
        })
    expected_checksum = sha256_text(canonical_json(_payload(data)))
    if data.get("checksum") != expected_checksum:
        raise BusinessError(
            E_SNAPSHOT,
            "source snapshot checksum is invalid before injection",
            {"source": str(source), "expected": expected_checksum, "actual": data.get("checksum")},
        )
    if Path(destination).exists():
        raise BusinessError(E_IO, "injection destination already exists", {"path": str(destination)})

    state = data["state"]
    mutation: dict[str, Any]
    if kind == "cache":
        original = int(data["cache_version"])
        data["cache_version"] = int(data["session_version"]) + 1
        mutation = {"field": "cache_version", "from": original, "to": data["cache_version"]}
    elif kind == "txn":
        max_txid = max((int(row["txid"]) for row in state["transactions"]), default=0)
        original = int(state["meta"]["next_txid"])
        state["meta"]["next_txid"] = str(max_txid)
        mutation = {"field": "state.meta.next_txid", "from": original, "to": max_txid,
                    "max_existing_txid": max_txid}
    else:
        heads = sorted(state["row_heads"], key=lambda row: (row["table_name"], row["row_key"]))
        if not heads:
            raise BusinessError(E_CHAIN, "cannot inject a chain fault into a snapshot with no chains", {})
        head = heads[0]
        table = head["table_name"]
        key = head["row_key"]
        head_id = int(head["head_version_id"])
        target = next(row for row in state["versions"] if int(row["version_id"]) == head_id)
        original = target["prev_version_id"]
        target["prev_version_id"] = head_id
        mutation = {
            "field": "state.versions.prev_version_id",
            "table": table,
            "key": key,
            "version_id": head_id,
            "from": original,
            "to": head_id,
            "chain_position": int(head["chain_len"]),
        }
    data["injection"] = {"kind": kind, "expected_code": INJECTION_CODES[kind], "mutation": mutation}
    data["checksum"] = sha256_text(canonical_json(_payload(data)))
    atomic_write_json(destination, data)
    return {
        "kind": kind,
        "source": str(source),
        "destination": str(destination),
        "expected_code": INJECTION_CODES[kind],
        "mutation": mutation,
        "checksum": data["checksum"],
    }


def run_anomaly_demo(
    workbench: Workbench,
    source_snapshot: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    baseline_version = workbench.state_version
    checks = []
    for kind, expected_code in INJECTION_CODES.items():
        path = output / f"injected-{kind}.mvccsnap.json"
        if path.exists():
            path.unlink()
        injection = inject_snapshot(source_snapshot, path, kind)
        actual_code = None
        message = None
        details: dict[str, Any] = {}
        try:
            workbench.restore_snapshot_path(path)
        except BusinessError as exc:
            actual_code = exc.code
            message = exc.message
            details = exc.details
        after_version = workbench.state_version
        passed = actual_code == expected_code and after_version == baseline_version
        record = {
            "kind": kind,
            "expected_code": expected_code,
            "actual_code": actual_code,
            "passed": passed,
            "message": message,
            "details": details,
            "state_version_unchanged": after_version == baseline_version,
            "injected_file": str(path),
            "mutation": injection["mutation"],
        }
        workbench.logger.event("anomaly_injection", **record)
        checks.append(record)
    summary = {
        "all_passed": all(item["passed"] for item in checks),
        "baseline_version": baseline_version,
        "checks": checks,
    }
    workbench.logger.event("anomaly_demo_complete", **summary)
    return summary
