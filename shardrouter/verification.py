"""Reproducibility helpers for plans, reports, and demo artifacts."""

from __future__ import annotations

from typing import Any, Callable

from .util import canonical_json, sha256_text


def canonical_text(value: Any) -> str:
    return canonical_json(value) + "\n"


def verify_repeated(render: Callable[[], Any], *, runs: int = 2) -> dict[str, Any]:
    """Execute a renderer repeatedly and compare canonical bytes exactly."""

    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 2:
        raise ValueError("runs must be an integer >= 2")
    payloads = [canonical_text(render()) for _ in range(runs)]
    hashes = [sha256_text(payload) for payload in payloads]
    return {
        "byte_length": len(payloads[0].encode("utf-8")),
        "run_hashes": hashes,
        "runs": runs,
        "stable": all(payload == payloads[0] for payload in payloads[1:]),
    }


def compare_canonical(left: Any, right: Any) -> dict[str, Any]:
    left_text = canonical_text(left)
    right_text = canonical_text(right)
    return {
        "left_sha256": sha256_text(left_text),
        "right_sha256": sha256_text(right_text),
        "stable": left_text == right_text,
    }
