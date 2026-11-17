"""Match injected anomaly identifiers to item-local failures."""

from __future__ import annotations

from typing import Any

from .model import Failure


def match_injections(injection_config: dict[str, Any], failures: list[Failure]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for injection in injection_config.get("injections", []):
        if not isinstance(injection, dict):
            continue
        injection_id = str(injection.get("id", ""))
        expected = injection.get("expected_code")
        target = str(injection.get("target", ""))
        doc_id = injection.get("doc_id")
        entry_id = injection.get("entry_id")
        operation_index = injection.get("operation_index")
        match: Failure | None = None
        for failure in failures:
            if expected and failure.business_code != expected:
                continue
            if doc_id is not None and failure.doc_id != str(doc_id):
                continue
            if entry_id is not None and failure.entry_id != str(entry_id):
                continue
            if operation_index is not None and failure.operation_index != int(operation_index):
                continue
            if target in ("v1", "v2"):
                if not (failure.stage.endswith(":" + target) or failure.stage.startswith("convert:" + target)):
                    continue
            elif target in ("commands", "edit"):
                if failure.stage != "edit":
                    continue
            match = failure
            break
        hits.append(
            {
                "injection_id": injection_id,
                "kind": injection.get("kind"),
                "target": target,
                "expected_code": expected,
                "hit": match is not None,
                "failure": match.to_dict() if match is not None else None,
            }
        )
    return sorted(hits, key=lambda item: item["injection_id"])
