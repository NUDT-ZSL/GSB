"""测试公共辅助：定位工程根、用固定种子生成一次性样本目录、调用 CLI。"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    seed_path = ROOT / "engineering_delivery" / "seed_samples.py"
    spec = importlib.util.spec_from_file_location("gsb_seed_samples", seed_path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_SEED = _load_seed_module()
_SAMPLES: Optional[Path] = None


def samples_dir() -> Path:
    """整个测试会话共享一份由固定种子生成的样本。"""
    global _SAMPLES
    if _SAMPLES is None:
        _SAMPLES = Path(tempfile.mkdtemp(prefix="gsb_samples_"))
        _SEED.generate(_SAMPLES, force=True)
    return _SAMPLES


def fresh_sample_copy(name: str = "case") -> Path:
    dst = Path(tempfile.mkdtemp(prefix=f"gsb_{name}_")) / "samples"
    shutil.copytree(samples_dir(), dst)
    return dst


def fresh_state_dir(name: str = "state") -> Path:
    return Path(tempfile.mkdtemp(prefix=f"gsb_{name}_")) / "state"


def run_cli(args: List[str], *, samples: Optional[Path] = None,
            state: Optional[Path] = None, expect_ok: bool = True,
            timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "gsb"]
    cmd += ["--samples", str(samples or samples_dir())]
    if state is not None:
        cmd += ["--state", str(state)]
    cmd += [str(a) for a in args]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(
            f"CLI failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
        )
    return proc


def run_cli_json(args: List[str], **kw: Any) -> Dict[str, Any]:
    proc = run_cli(args, **kw)
    return json.loads(proc.stdout)
