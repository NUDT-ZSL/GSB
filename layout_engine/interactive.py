from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from .config import LayoutConfig
from .engine import LayoutEngine
from .errors import ConfigError
from .util import atomic_copy_bytes, atomic_write_json, read_json


class InteractiveSession:
    def __init__(
        self,
        *,
        images_dir: str | Path,
        config_path: str | Path,
        metadata_path: str | Path,
        snapshot_dir: str | Path,
        report_path: str | Path,
        truth_path: str | Path | None = None,
    ) -> None:
        self.images_dir = Path(images_dir)
        self.config_path = Path(config_path)
        self.metadata_path = Path(metadata_path)
        self.snapshot_dir = Path(snapshot_dir)
        self.report_path = Path(report_path)
        self.truth_path = Path(truth_path) if truth_path else None
        loaded = read_json(self.config_path)
        if not isinstance(loaded, dict):
            raise ConfigError("config JSON must be an object")
        self.config_raw: dict[str, Any] = loaded
        LayoutConfig.from_dict(self.config_raw)
        self.history: list[dict[str, Any]] = []
        self.last_report: dict[str, Any] | None = None

    def _persist_config(self) -> None:
        LayoutConfig.from_dict(self.config_raw)
        atomic_write_json(self.config_path, self.config_raw)

    def _run(self, command_label: str, started: float) -> dict[str, Any]:
        engine = LayoutEngine(
            self.images_dir,
            self.config_path,
            self.metadata_path,
            self.snapshot_dir,
            truth_path=self.truth_path,
            allow_config_change=True,
        )
        result = engine.run(
            command_label=command_label,
            interaction_history=self.history,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        budget = int(self.config_raw["runtime"]["interaction_latency_budget_ms"])
        commands = result.report["interaction"]["commands"]
        commands[-1]["elapsed_ms"] = round(elapsed_ms, 3)
        commands[-1]["within_budget"] = elapsed_ms <= budget
        result.report["interaction"]["all_within_budget"] = all(
            item["within_budget"] for item in commands
        )
        self.history = copy.deepcopy(commands)
        self.last_report = result.report
        atomic_write_json(self.report_path, result.report)
        return result.report

    def _mutate_and_run(self, label: str, mutator) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            mutator()
            self._persist_config()
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            budget = int(self.config_raw.get("runtime", {}).get("interaction_latency_budget_ms", 0) or 0)
            self.history.append(
                {
                    "command": label,
                    "elapsed_ms": round(elapsed_ms, 3),
                    "within_budget": budget > 0 and elapsed_ms <= budget,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        return self._run(label, started)

    def execute(self, line: str) -> tuple[bool, dict[str, Any] | None]:
        parts = line.strip().split()
        if not parts:
            return True, None
        command = parts[0]
        if command in {"quit", "exit"}:
            return False, None
        if command == "run":
            started = time.perf_counter()
            return True, self._run("run", started)
        if command == "threshold" and len(parts) == 2:
            value = int(parts[1])

            def mutate() -> None:
                self.config_raw["analysis"]["binary_threshold"] = value

            return True, self._mutate_and_run(f"threshold {value}", mutate)
        if command == "column-gap" and len(parts) == 2:
            value = int(parts[1])

            def mutate() -> None:
                self.config_raw["columns"]["gap_threshold"] = value

            return True, self._mutate_and_run(f"column-gap {value}", mutate)
        if command == "rule" and len(parts) == 2:
            value = parts[1]

            def mutate() -> None:
                self.config_raw["reading"]["rule"] = value

            return True, self._mutate_and_run(f"rule {value}", mutate)
        if command == "columns" and len(parts) == 2 and parts[1] in {"on", "off"}:
            value = parts[1] == "on"

            def mutate() -> None:
                self.config_raw["columns"]["enabled"] = value

            return True, self._mutate_and_run(f"columns {parts[1]}", mutate)
        if command == "replace-page" and len(parts) == 3:
            target = parts[1]
            source = Path(parts[2])

            def mutate() -> None:
                if Path(target).name != target or not target.lower().endswith(".png"):
                    raise ConfigError("page name must be a plain .png file name")
                atomic_copy_bytes(self.images_dir / target, source.read_bytes())

            return True, self._mutate_and_run(f"replace-page {target}", mutate)
        if command == "add-page" and len(parts) == 3:
            target = parts[1]
            source = Path(parts[2])

            def mutate() -> None:
                if Path(target).name != target or not target.lower().endswith(".png"):
                    raise ConfigError("page name must be a plain .png file name")
                destination = self.images_dir / target
                if destination.exists():
                    raise ConfigError(f"page already exists: {target}")
                atomic_copy_bytes(destination, source.read_bytes())

            return True, self._mutate_and_run(f"add-page {target}", mutate)
        if command == "remove-page" and len(parts) == 2:
            target = parts[1]

            def mutate() -> None:
                if Path(target).name != target:
                    raise ConfigError("page name must be a plain file name")
                path = self.images_dir / target
                if not path.exists():
                    raise ConfigError(f"page does not exist: {target}")
                path.unlink()

            return True, self._mutate_and_run(f"remove-page {target}", mutate)
        if command == "status":
            print(json.dumps(self.summary(), ensure_ascii=False, sort_keys=True))
            return True, self.last_report
        raise ConfigError(f"unknown or malformed command: {line.strip()}")

    def summary(self) -> dict[str, Any]:
        if self.last_report is None:
            return {
                "status": "not_run",
                "commands": self.history,
                "latency_budget_ms": self.config_raw["runtime"][
                    "interaction_latency_budget_ms"
                ],
            }
        report = self.last_report
        return {
            "status": report["run"]["status"],
            "summary": report["summary"],
            "business_codes": report["business_codes"],
            "latency_budget_ms": report["interaction"]["latency_budget_ms"],
            "commands": report["interaction"]["commands"],
            "all_within_budget": report["interaction"]["all_within_budget"],
        }


def run_repl(session: InteractiveSession) -> int:
    try:
        while True:
            try:
                line = input("layout> ")
            except EOFError:
                break
            try:
                keep_going, _ = session.execute(line)
            except Exception as exc:
                print(f"error: {type(exc).__name__}: {exc}")
                continue
            if not keep_going:
                break
        print(json.dumps(session.summary(), ensure_ascii=False, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps(session.summary(), ensure_ascii=False, sort_keys=True))
        return 0
