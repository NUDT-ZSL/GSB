from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from edgecache.config import ContentState, Policy
from edgecache.ledger import Ledger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = PROJECT_ROOT / "samples"


def load_doc(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def bootstrap_ledger(path: str | Path, policy_name: str = "policy.json") -> Ledger:
    ledger = Ledger(path)
    ledger.bootstrap(load_doc(policy_name), load_doc("content.json"))
    return ledger


def policy_doc(**updates: Any) -> dict[str, Any]:
    doc = copy.deepcopy(load_doc("policy.json"))
    doc.update(updates)
    return doc


class FakeClock:
    def __init__(self, start: int = 1_000_000) -> None:
        self.value = start

    def now(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class FakeSource:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def fetch_content(self, path: str, ledger_version: int) -> dict[str, Any]:
        active = self.ledger.assert_version(ledger_version)
        value = active.content.effective[path]
        return {
            "ledger_version": active.ledger_version,
            "content_set_hash": active.content_set_hash,
            "path": path,
            "etag": value.etag,
            "body": value.body_text(),
            "body_encoding": value.body_encoding,
            "sha256": value.body_sha256,
        }


def complete_content_plan(content_doc: dict[str, Any], policy_doc_value: dict[str, Any]) -> tuple[ContentState, Policy]:
    content = ContentState.from_doc(content_doc)
    policy = Policy.from_doc(policy_doc_value, content.paths)
    return content, policy

