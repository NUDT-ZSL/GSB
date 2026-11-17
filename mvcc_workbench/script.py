"""Deterministic JSON transaction-script executor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import Workbench
from .errors import E_INPUT, E_VISIBLE, BusinessError
from .util import canonical_json, load_json


SCRIPT_FORMAT = "mvcc-workbench-script/v1"


class ScriptRunner:
    def __init__(self, workbench: Workbench) -> None:
        self.workbench = workbench

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(E_INPUT, "cannot read transaction script", {"path": str(path), "error": str(exc)})
        if data.get("format") != SCRIPT_FORMAT:
            raise BusinessError(
                E_INPUT,
                "unsupported transaction script format",
                {"path": str(path), "format": data.get("format")},
            )
        if not isinstance(data.get("steps"), list):
            raise BusinessError(E_INPUT, "transaction script steps must be a list", {"path": str(path)})
        return data

    def run(self, script: dict[str, Any]) -> dict[str, Any]:
        if script.get("format") != SCRIPT_FORMAT:
            raise BusinessError(E_INPUT, "unsupported transaction script format", {"format": script.get("format")})
        for table in script.get("tables", []):
            self.workbench.create_table(table["name"], list(table["columns"]))
        labels: dict[str, int] = {}
        reads: list[dict[str, Any]] = []
        mutations: list[dict[str, Any]] = []
        steps = script.get("steps", [])
        for index, step in enumerate(steps, start=1):
            operation = step.get("op")
            try:
                if operation == "begin":
                    label = step.get("tx") or step.get("label")
                    if not label:
                        raise BusinessError(E_INPUT, "begin needs a transaction label", {"step": index})
                    if label in labels:
                        raise BusinessError(E_INPUT, "transaction label was already begun", {"step": index, "tx": label})
                    tx = self.workbench.begin_transaction(
                        str(step.get("label") or label), step.get("txid")
                    )
                    labels[label] = int(tx["txid"])
                    mutations.append({"step": index, "op": "begin", **tx})
                elif operation in {"write", "delete"}:
                    txid = self._txid(step, labels, index)
                    if operation == "write":
                        result = self.workbench.write(
                            txid, step["table"], step["key"], step.get("value")
                        )
                    else:
                        result = self.workbench.delete(txid, step["table"], step["key"])
                    mutations.append({"step": index, "op": operation, **result})
                elif operation == "commit":
                    txid = self._txid(step, labels, index)
                    result = self.workbench.commit(txid)
                    mutations.append({"step": index, "op": "commit", **result})
                elif operation == "rollback":
                    txid = self._txid(step, labels, index)
                    result = self.workbench.rollback(txid)
                    mutations.append({"step": index, "op": "rollback", **result})
                elif operation in {"read", "query"}:
                    latest = bool(step.get("latest"))
                    txid = None if latest else self._txid(step, labels, index)
                    verify = bool(step.get("verify", True))
                    if verify:
                        response = self.workbench.verify(
                            step["table"], step["key"], txid=txid, record=True
                        )
                        result = response["authoritative"]
                        canonical_equal = response["canonical_equal"]
                    else:
                        response = self.workbench.query(
                            step["table"], step["key"], txid=txid, record=True
                        )
                        result = response["result"]
                        canonical_equal = None
                    expected = step.get("expect")
                    if expected is not None:
                        mismatches = {
                            key: {"expected": value, "actual": result.get(key)}
                            for key, value in expected.items()
                            if result.get(key) != value
                        }
                        if mismatches:
                            raise BusinessError(
                                E_VISIBLE,
                                "script expectation does not match the visibility result",
                                {
                                    "step": index,
                                    "txid": txid,
                                    "table": step["table"],
                                    "key": step["key"],
                                    "mismatches": mismatches,
                                    "actual_canonical": canonical_json(result),
                                },
                            )
                    reads.append(
                        {
                            "step": index,
                            "name": step.get("name"),
                            "txid": 0 if latest else txid,
                            "table": step["table"],
                            "key": step["key"],
                            "visible": result["visible"],
                            "value": result["value"],
                            "selected_version_id": result["selected_version_id"],
                            "view_version": result["view_version"],
                            "canonical_equal": canonical_equal,
                        }
                    )
                else:
                    raise BusinessError(E_INPUT, "unknown script operation", {"step": index, "op": operation})
            except BusinessError as exc:
                exc.details.setdefault("step", index)
                raise
        return {
            "script": script.get("name"),
            "steps": len(steps),
            "transactions": self.workbench.transactions(),
            "reads": reads,
            "mutations": mutations,
            "all_canonical_equal": all(
                item["canonical_equal"] in {True, None} for item in reads
            ),
            "state_version": self.workbench.state_version,
        }

    @staticmethod
    def _txid(step: dict[str, Any], labels: dict[str, int], index: int) -> int:
        label = step.get("tx")
        if label is None:
            if "txid" in step:
                return int(step["txid"])
            raise BusinessError(E_INPUT, "operation needs a transaction label", {"step": index})
        if label not in labels:
            raise BusinessError(E_INPUT, "transaction label has not begun", {"step": index, "tx": label})
        return labels[label]


def execute_script_file(workbench: Workbench, path: str | Path) -> dict[str, Any]:
    return ScriptRunner(workbench).run(ScriptRunner.load(path))
