"""Request stream (JSONL) loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .errors import E_POLICY_TYPE, WorkbenchError


@dataclass
class Request:
    request_id: str
    context_fingerprint: str
    max_length: int
    eos_token_ids: list = field(default_factory=list)

    def sort_key(self):
        return self.request_id


def load_requests(path):
    requests = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            for key in ("request_id", "context_fingerprint", "max_length"):
                if key not in rec:
                    raise WorkbenchError(
                        E_POLICY_TYPE, f"request line {lineno} missing {key!r}"
                    )
            requests.append(
                Request(
                    request_id=str(rec["request_id"]),
                    context_fingerprint=str(rec["context_fingerprint"]),
                    max_length=int(rec["max_length"]),
                    eos_token_ids=[int(t) for t in rec.get("eos_token_ids", [])],
                )
            )
    requests.sort(key=lambda r: r.sort_key())
    return requests
