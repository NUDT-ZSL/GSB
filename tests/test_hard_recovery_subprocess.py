import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_real_sigkill_recovery_script_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_hard_recovery.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert '"child_killed_by_sigkill": true' in result.stdout
    assert '"business_code": "E_OK"' in result.stdout
