from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any

from voice_dialog.config import DEFAULTS, load_config, load_injections, normalize_config
from voice_dialog.pipeline import Pipeline
from voice_dialog.reporting import stable_report_view
from voice_dialog.synthetic import generate_demo
from voice_dialog.util import atomic_write_json


class TempCase:
    def __init__(self):
        self._temp = tempfile.TemporaryDirectory(prefix="voice-dialog-test-")
        self.root = Path(self._temp.name)

    def close(self) -> None:
        self._temp.cleanup()

    def config(self, **overrides: Any) -> dict[str, Any]:
        raw = copy.deepcopy(DEFAULTS)
        for key, value in overrides.items():
            if key == "fusion" and isinstance(value, dict):
                raw["fusion"].update(value)
            elif key == "scale_tiers" and isinstance(value, dict):
                raw["scale_tiers"].update(value)
            else:
                raw[key] = value
        return normalize_config(raw)

    def write_config(self, config: dict[str, Any], name: str = "config.json") -> Path:
        path = self.root / name
        atomic_write_json(path, {key: value for key, value in config.items() if key != "config_fingerprint"})
        return path

    def dataset(
        self,
        name: str = "dataset",
        *,
        segments: int = 16,
        turns: int = 8,
        anomalies: bool = True,
    ) -> Path:
        out = self.root / name
        generate_demo(
            out,
            seed=73117,
            segments=segments,
            turns=turns,
            duration=0.74,
            include_anomalies=anomalies,
            write_default_configs=True,
        )
        return out


def run_once(
    dataset: Path,
    config_path: Path,
    state: Path,
    *,
    mode: str = "incremental",
    injections_path: Path | None = None,
) -> dict[str, Any]:
    pipeline = Pipeline(
        samples_dir=dataset,
        script_path=dataset / "dialog_script.json",
        config=load_config(config_path),
        injections=load_injections(injections_path or dataset / "anomalies.json"),
        state_dir=state,
        mode=mode,
    )
    return pipeline.run()


def assert_stable_equal(testcase, left: dict[str, Any], right: dict[str, Any]) -> None:
    lv = stable_report_view(left)
    rv = stable_report_view(right)
    testcase.assertEqual(json.dumps(lv, sort_keys=True), json.dumps(rv, sort_keys=True))
