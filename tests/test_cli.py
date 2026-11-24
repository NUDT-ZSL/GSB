from __future__ import annotations

import json
from pathlib import Path

from surface_stitch.cli import main


def test_cli_generate_and_run(sample_dir: Path, tmp_path: Path, capsys) -> None:
    output = tmp_path / "outputs"
    state = tmp_path / "state"
    exit_code = main(
        [
            "run",
            "--input",
            str(sample_dir),
            "--output-dir",
            str(output),
            "--state-dir",
            str(state),
            "--workers",
            "1",
        ]
    )
    assert exit_code == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["ok"] is True
    assert (output / "mosaic.png").exists()


def test_cli_invalid_input_returns_business_json_without_image(
    sample_dir: Path, tmp_path: Path, capsys
) -> None:
    project = sample_dir / "project.json"
    payload = json.loads(project.read_text(encoding="utf-8"))
    payload["canvas"]["width"] = -1
    project.write_text(json.dumps(payload), encoding="utf-8")

    output = tmp_path / "outputs"
    exit_code = main(
        [
            "run",
            "--input",
            str(sample_dir),
            "--output-dir",
            str(output),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    assert exit_code == 2
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["ok"] is False
    assert emitted["error"]["code"].startswith("E_")
    assert not (output / "mosaic.png").exists()
