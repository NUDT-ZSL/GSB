from __future__ import annotations

import os
import subprocess
import sys

from conftest import ROOT


def test_library_example_is_byte_repeatable() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    outputs: list[str] = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "examples" / "library_demo.py")],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]
    assert '"algorithm": "aggregate"' in outputs[0]
    assert '"algorithm": "direct"' in outputs[0]
