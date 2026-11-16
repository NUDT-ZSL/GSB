"""Interactive offline console for the lineage migration engine.

Run with ``python -m lineage_migrator.console``. Commands:

  load                 rebuild SQLite baseline from local JSON samples
  apply [version]      apply one sample version (default: next version)
  apply-all            replay every sample version in order
  versions             list committed versions and fingerprints
  snapshot [version]   show a snapshot (default: last committed version)
  diff A B [--table T] [--field F]
                       lineage diff between two snapshots with drill-down
  rollback VERSION     truncate versions newer than VERSION
  anomalies            list configured anomaly scenarios
  inject [ID]          run one or all anomaly scenarios in scratch DBs
  state                print current live counts and last version
  help                 show this help
  exit                 leave the console
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .diff import diff_versions
from .engine import Engine
from .errors import MigrationError
from .injector import load_anomalies, run_all_scenarios, run_scenario
from .models import Sample
from .store import Store

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Console:
    def __init__(self, sample_dir: Path, db_path: Path):
        self.sample_dir = sample_dir
        self.db_path = db_path
        self.sample = Sample.load(sample_dir)
        self.store = Store(db_path)

    def load(self) -> str:
        self.store.close()
        engine = Engine.bootstrap(self.db_path, self.sample)
        self.store = engine.store
        return f"baseline loaded: {len(self.store.live_fields())} fields, {len(self.store.live_edges())} edges"

    def apply(self, version: int | None) -> str:
        target = version if version is not None else self.store.last_version() + 1
        if target not in self.sample.versions:
            return f"version {target} has no sample rules"
        result = Engine(self.store).apply_version(target, self.sample.versions[target])
        return json.dumps(result, ensure_ascii=False)

    def apply_all(self) -> str:
        output = []
        for version in self.sample.ordered_versions():
            if version <= self.store.last_version():
                continue
            output.append(self.apply(version))
        return "\n".join(output) or "all sample versions already applied"

    def snapshot(self, version: int | None) -> str:
        target = self.store.last_version() if version is None else version
        if self.store.get_version(target) is None:
            return f"snapshot {target} not found"
        payload = {
            "fields": [item.__dict__ for item in self.store.snapshot_fields(target)],
            "edges": [item.__dict__ for item in self.store.snapshot_edges(target)],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def diff(self, from_version: int, to_version: int, table: str | None, field_ref: str | None) -> str:
        try:
            payload = diff_versions(
                self.store, from_version, to_version, table=table, field_ref=field_ref
            )
        except ValueError as exc:
            return str(exc)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def rollback(self, target: int) -> str:
        try:
            self.store.rollback_to(target)
        except ValueError as exc:
            return str(exc)
        return f"rolled back to v{target}; edges={len(self.store.live_edges())}"

    def anomalies(self) -> str:
        scenarios = load_anomalies(self.sample_dir / "anomalies.json")
        return json.dumps(
            [
                {
                    "scenario_id": item.scenario_id,
                    "version": item.version,
                    "expected_code": item.expected_code,
                    "description": item.description,
                }
                for item in scenarios
            ],
            indent=2,
            ensure_ascii=False,
        )

    def inject(self, scenario_id: str | None) -> str:
        scenarios = load_anomalies(self.sample_dir / "anomalies.json")
        workspace = self.db_path.parent / "anomaly_runs"
        if scenario_id is None:
            results = run_all_scenarios(self.sample, self.sample_dir / "anomalies.json", workspace)
        else:
            match = next((item for item in scenarios if item.scenario_id == scenario_id), None)
            if match is None:
                return f"scenario {scenario_id} not found"
            results = [run_scenario(match, self.sample, workspace)]
        return json.dumps(results, indent=2, ensure_ascii=False)

    def state(self) -> str:
        return json.dumps(
            {
                "db_path": str(self.db_path),
                "initialized": self.store.is_initialized(),
                "last_version": self.store.last_version(),
                "field_count": len(self.store.live_fields()),
                "edge_count": len(self.store.live_edges()),
            },
            indent=2,
            ensure_ascii=False,
        )

    def close(self) -> None:
        self.store.close()


HELP_TEXT = __doc__ or ""


def _parse_diff_args(parts: list[str]) -> tuple[int, int, str | None, str | None]:
    table = field_ref = None
    positional: list[str] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        if token == "--table":
            table = parts[index + 1]
            index += 2
        elif token == "--field":
            field_ref = parts[index + 1]
            index += 2
        else:
            positional.append(token)
            index += 1
    return int(positional[0]), int(positional[1]), table, field_ref


def run_repl(console: Console) -> None:
    print("Lineage Migration Console. Type 'help' for commands, 'exit' to quit.")
    while True:
        try:
            raw = input("lineage> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        parts = raw.split()
        command, args = parts[0], parts[1:]
        try:
            if command == "exit":
                break
            elif command == "help":
                print(HELP_TEXT)
            elif command == "load":
                print(console.load())
            elif command == "apply":
                print(console.apply(int(args[0]) if args else None))
            elif command == "apply-all":
                print(console.apply_all())
            elif command == "versions":
                print(json.dumps(console.store.versions(), indent=2, ensure_ascii=False))
            elif command == "snapshot":
                print(console.snapshot(int(args[0]) if args else None))
            elif command == "diff":
                print(console.diff(*_parse_diff_args(args)))
            elif command == "rollback":
                print(console.rollback(int(args[0])))
            elif command == "anomalies":
                print(console.anomalies())
            elif command == "inject":
                print(console.inject(args[0] if args else None))
            elif command == "state":
                print(console.state())
            else:
                print(f"unknown command: {command} (try 'help')")
        except MigrationError as exc:
            print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False))
        except (ValueError, IndexError) as exc:
            print(f"bad arguments: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lineage migration interactive console")
    parser.add_argument("--sample-dir", default=str(PROJECT_ROOT / "samples"))
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "lineage.db"))
    args = parser.parse_args()
    console = Console(Path(args.sample_dir), Path(args.db))
    try:
        run_repl(console)
    finally:
        console.close()


if __name__ == "__main__":
    main()
