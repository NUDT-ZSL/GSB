from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import BusinessError
from .engine import DiagnosticEngine
from .mock_service import LocalMockService, load_mock_config
from .scenario import parse_scenario, scenario_payload
from .store import Store


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def run_demo(
    *,
    workdir: Path,
    scenario_path: Path,
    mock_config_path: Path,
    port: int = 0,
) -> dict[str, object]:
    workdir.mkdir(parents=True, exist_ok=True)
    mock_config = load_mock_config(mock_config_path)
    service = LocalMockService(mock_config, port=port).start()
    try:
        raw_scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        for target in raw_scenario["targets"]:
            endpoint = target["endpoint"]
            prefix, separator, _rest = endpoint.partition("://")
            if not separator:
                raise BusinessError(
                    "DEMO_SCENARIO_INVALID",
                    "target endpoint is not a valid URL",
                    {"endpoint": endpoint},
                )
            host_part, slash, path = endpoint[len(prefix) + 3 :].partition("/")
            host = host_part.rsplit(":", 1)[0]
            target["endpoint"] = (
                f"{prefix}://{host}:{service.port}"
                + (f"/{path}" if slash else "")
            )
        scenario = parse_scenario(raw_scenario)
        materialized_scenario = write_stable_json(
            workdir / "scenario-demo.json", scenario_payload(scenario)
        )
        reports: list[bytes] = []
        db_files: list[str] = []
        for run_index in (1, 2):
            db_path = workdir / f"demo-run-{run_index}.db"
            _remove_db_files(db_path)
            with Store(db_path) as store:
                engine = DiagnosticEngine(store)
                engine.load_scenario(scenario)
                report = engine.run(all_rounds=True)
            payload = _json_bytes(report)
            report_path = workdir / f"report-run-{run_index}.json"
            report_path.write_bytes(payload)
            reports.append(payload)
            db_files.append(db_path.name)
        identical = reports[0] == reports[1]
        report_hash = hashlib.sha256(reports[0]).hexdigest()
        summary = {
            "ok": identical,
            "byte_identical": identical,
            "report_sha256": report_hash,
            "workdir": str(workdir),
            "scenario_file": materialized_scenario.name,
            "mock_base_url": service.base_url,
            "report_files": [
                f"report-run-{run_index}.json" for run_index in (1, 2)
            ],
            "db_files": db_files,
            "target_count": len(scenario.targets),
            "round_count": len(scenario.rounds),
            "probe_count": sum(
                len(round_spec.probes) for round_spec in scenario.rounds
            ),
            "final_status": {
                target["target"]: {
                    "state": target["state"],
                    "loss_rate": target["loss_rate"],
                    "abandoned_count": target["abandoned_count"],
                }
                for target in report["status"]["targets"]
            },
        }
        (workdir / "summary.json").write_bytes(_json_bytes(summary))
        return summary
    finally:
        service.stop()


def _remove_db_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def write_stable_json(path: Path, value: object) -> Path:
    path.write_bytes(_json_bytes(value))
    return path
