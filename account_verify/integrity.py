from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .digests import file_sha256


def verify_samples_against_truth(samples_dir: Path) -> Dict[str, Any]:
    samples_dir = Path(samples_dir)
    truth_path = samples_dir / "truth.json"
    if not truth_path.exists():
        return {"checked": False, "unchanged": False, "code": "E_INPUT_HASH_MISMATCH", "reason": "truth.json missing"}
    with open(truth_path, "r", encoding="utf-8") as handle:
        truth = json.load(handle)

    expected = truth.get("input_sha256", {})
    mismatches = []
    missing = []
    for relative, expected_hash in sorted(expected.items()):
        path = samples_dir / relative
        if not path.exists():
            missing.append(relative)
            continue
        actual = file_sha256(path)
        if actual != expected_hash:
            mismatches.append({"file": relative, "expected": expected_hash, "actual": actual})

    unchanged = not mismatches and not missing
    return {
        "checked": True,
        "unchanged": unchanged,
        "code": "OK" if unchanged else "E_INPUT_HASH_MISMATCH",
        "files_checked": len(expected),
        "mismatches": mismatches,
        "missing": missing,
    }
