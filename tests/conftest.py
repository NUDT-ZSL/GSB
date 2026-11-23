from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lbworkbench.probes import ProbeHTTPServer  # noqa: E402


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("samples")
    subprocess.run(
        [sys.executable, str(ROOT / "samples" / "generate_samples.py"), "--out", str(output)],
        check=True,
        cwd=ROOT,
    )
    return output


@pytest.fixture(scope="session")
def baseline_package(samples: Path) -> Path:
    return samples / "package-2026.11.1.json"


@pytest.fixture(scope="session")
def replacement_package(samples: Path) -> Path:
    return samples / "package-2026.11.2.json"


@pytest.fixture(scope="session")
def check_plan(samples: Path) -> Path:
    return samples / "check_plan.json"


@pytest.fixture(scope="session")
def probe_server() -> ProbeHTTPServer:
    server = ProbeHTTPServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def sample_copy(samples: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "samples"
    shutil.copytree(samples, destination)
    return destination


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
