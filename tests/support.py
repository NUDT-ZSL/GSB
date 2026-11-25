from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools.generate_samples import build_fixture

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "ledger.py"


def fixture_bundle(seed: int = 20261126) -> dict[str, Any]:
    return copy.deepcopy(build_fixture(seed))


def write_bundle(path: Path, bundle: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def cli(
    *args: str,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LEDGER), *args],
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
