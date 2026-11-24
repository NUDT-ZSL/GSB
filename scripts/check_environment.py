#!/usr/bin/env python3
"""Check the local runtime and atomic-write prerequisites."""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import uuid
from pathlib import Path

try:
    import numpy as np
except Exception as exc:  # pragma: no cover - diagnostic script
    print(json.dumps({"ok": False, "code": "E_DEPENDENCY", "message": str(exc)}, ensure_ascii=False))
    raise SystemExit(2)


def probe(path: Path) -> dict[str, object]:
    path.mkdir(parents=True, exist_ok=True)
    source = path / f".probe-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    target = path / f".probe-{os.getpid()}-{uuid.uuid4().hex}.done"
    source.write_bytes(b"probe")
    os.replace(source, target)
    target.unlink()
    return {"ok": True, "path": str(path), "atomic_replace": True}


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    report = {
        "ok": True,
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "numpy": np.__version__,
        "temp_dir": tempfile.gettempdir(),
        "workspace": str(root),
    }
    for name in ("state", "output"):
        report[name] = probe(root / name)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

