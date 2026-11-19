from __future__ import annotations

import hashlib
import json
from pathlib import Path

from qrw.cli import main
from qrw.interactive import InteractiveApp
from scripts.generate_samples import generate_samples


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_sample_generation_is_deterministic(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_samples(first, 12345)
    first_hash = _tree_hash(first / "samples")
    generate_samples(first, 12345)
    assert _tree_hash(first / "samples") == first_hash
    generate_samples(second, 12345)
    assert _tree_hash(second / "samples") == first_hash
    third = tmp_path / "third"
    generate_samples(third, 999)
    assert _tree_hash(third / "samples") != first_hash


def test_cli_demo_writes_json(tmp_path: Path, project_root: Path, capsys):
    output = tmp_path / "report.json"
    code = main(
        [
            "demo",
            "--root",
            str(project_root),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["equivalence"] == "EQUIVALENT"
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["validation"]["ok"] is True


def test_interactive_dialect_switching(project_root: Path):
    app = InteractiveApp(project_root)
    assert "[v3]" in app.execute("view v3")
    assert app.dialect == "v3"
    tree = app.execute("ir")
    assert "rule n_" in tree
    candidates = app.execute("candidates q001 all")
    payload = json.loads(candidates)
    assert set(payload) == {"v1", "v2", "v3"}


def test_windows_entrypoints(project_root: Path):
    assert ".venv\\Scripts\\python.exe" in (project_root / "qrw.cmd").read_text(
        encoding="utf-8"
    )
    assert "artifacts\\demo_report.json" in (project_root / "run_demo.cmd").read_text(
        encoding="utf-8"
    )
    assert "-m qrw" in (project_root / "run_demo.ps1").read_text(encoding="utf-8")

