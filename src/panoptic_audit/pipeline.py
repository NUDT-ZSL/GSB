from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import build_audit_report, write_report_atomic
from .errors import AuditError, ErrorCode
from .execution import evaluate_degradation, execute_probe
from .hashing import canonical_checksum
from .ledger import build_unified_ledger, conflict_policy
from .parsers import parse_rle, parse_v1, parse_v2
from .parsers.formats import iter_source_checksums
from .probes import parse_probe_plan
from .selfcheck import cross_module_check
from .store import LedgerStore

REQUIRED_CONFIG_KEYS = [
    "probe_time_budget_ms",
    "conflict_coverage_threshold",
    "format_priority",
    "ledger_version",
    "audit_output_path",
]


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "run configuration cannot be loaded", {"path": str(path)}) from exc
    missing = [key for key in REQUIRED_CONFIG_KEYS if key not in config]
    if missing:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "run configuration is missing fields", {"missing": missing})
    if config["format_priority"] != ["RLE", "v2", "v1"] and set(config["format_priority"]) != {"RLE", "v2", "v1"}:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "format_priority must contain RLE, v2 and v1")
    return config


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _paths(config: dict[str, Any], config_path: str | Path) -> tuple[Path, Path, Path, Path]:
    config_file = Path(config_path)
    base_dir = config_file.resolve().parent.parent if config_file.parent.name == "configs" else Path.cwd()
    sample_dir = Path(config.get("sample_dir", "samples/fixed_seed"))
    if not sample_dir.is_absolute():
        sample_dir = base_dir / sample_dir
    audit_path = Path(config["audit_output_path"])
    if not audit_path.is_absolute():
        audit_path = base_dir / audit_path
    ledger_path = Path(config.get("ledger_path", audit_path.parent / "audit_ledger.sqlite3"))
    if not ledger_path.is_absolute():
        ledger_path = base_dir / ledger_path
    hwm_path = Path(config.get("high_water_path", ledger_path.with_suffix(".hwm.json")))
    if not hwm_path.is_absolute():
        hwm_path = base_dir / hwm_path
    return sample_dir, audit_path, ledger_path, hwm_path


def load_sample_inputs(sample_dir: Path, config: dict[str, Any]):
    manifest = _load_json(sample_dir / "manifest.json")
    entities = {(int(e["category_id"]), int(e["instance_id"])) for e in manifest["entities"]}
    priority = list(config["format_priority"])
    parsers = {
        "v1": lambda: parse_v1(sample_dir / "gt_v1.json", entities),
        "v2": lambda: parse_v2(sample_dir / "gt_v2"),
        "RLE": lambda: parse_rle(sample_dir / "gt_rle.json", entities),
    }
    truths = {fmt: parsers[fmt]() for fmt in priority}
    plan_payload = _load_json(sample_dir / "probe_plan.json")
    height = int(manifest["height"])
    width = int(manifest["width"])
    accepted, rejected, suppressed = parse_probe_plan(plan_payload, height, width)
    ledger = build_unified_ledger(truths, priority)
    conflicts = conflict_policy(ledger, len(accepted), float(config["conflict_coverage_threshold"]))
    return manifest, truths, accepted, rejected, suppressed, ledger, conflicts


def _open_or_create_store(
    config: dict[str, Any],
    ledger_path: Path,
    hwm_path: Path,
    ledger,
    truths,
    *,
    fresh: bool,
) -> LedgerStore:
    source_checksums = sorted(
        iter_source_checksums(truths.values()),
        key=lambda item: (item["format"], item["block_id"]),
    )
    if fresh and ledger_path.exists():
        ledger_path.unlink()
        if hwm_path.exists():
            hwm_path.unlink()
    if ledger_path.exists():
        store = LedgerStore.open(ledger_path, hwm_path)
        if store.ledger_version != config["ledger_version"]:
            try:
                rolled_back = float(store.ledger_version) > float(config["ledger_version"])
            except (TypeError, ValueError):
                rolled_back = str(store.ledger_version) > str(config["ledger_version"])
            code = ErrorCode.SNAPSHOT_ROLLBACK if rolled_back else ErrorCode.FORMAT_UNSUPPORTED
            store.close()
            raise AuditError(code, "ledger version does not match run configuration", {
                "ledger_version": store.ledger_version,
                "configured_version": config["ledger_version"],
            })
        return store
    return LedgerStore.create(
        ledger_path,
        ledger_version=config["ledger_version"],
        adjudicated=ledger,
        source_checksums=source_checksums,
        high_water_path=hwm_path,
    )


def run_audit(
    config_path: str | Path = "configs/run_config.json",
    *,
    fresh: bool = False,
    timer=None,
) -> dict[str, Any]:
    config = load_config(config_path)
    sample_dir, audit_path, ledger_path, hwm_path = _paths(config, config_path)
    manifest, truths, accepted, rejected, suppressed, ledger, conflicts = load_sample_inputs(sample_dir, config)
    store = _open_or_create_store(config, ledger_path, hwm_path, ledger, truths, fresh=fresh)

    try:
        known_uids = store.existing_uids()
        pending = [probe for probe in accepted if probe.uid not in known_uids]
        round_size = int(config.get("probe_round_size", 2))
        budget_ns = int(round(float(config["probe_time_budget_ms"]) * 1_000_000))
        degradation_threshold = float(config.get("slow_budget_share_threshold", 0.35))
        fast_threshold = float(config.get("confidence_fast_threshold", 0.75))
        degraded = bool(store.load_progress().get("degraded", False))

        for start in range(0, len(pending), round_size):
            chunk = pending[start : start + round_size]
            records = []
            for probe in chunk:
                record = execute_probe(
                    probe,
                    ledger,
                    truths,
                    fast_confidence_threshold=fast_threshold,
                    degraded_aggregate=degraded,
                    timer=timer,
                ) if timer is not None else execute_probe(
                    probe,
                    ledger,
                    truths,
                    fast_confidence_threshold=fast_threshold,
                    degraded_aggregate=degraded,
                )
                records.append(record)

            existing_records = store.load_probe_records()
            evaluation = evaluate_degradation(
                existing_records + records,
                total_budget_ns=budget_ns,
                threshold=degradation_threshold,
            )
            degraded = degraded or bool(evaluation["triggered"])
            store.commit_round(
                records,
                budget_ns=budget_ns,
                degraded=degraded,
                degradation_reason=evaluation,
            )

        report = build_audit_report(
            config=config,
            manifest=manifest,
            accepted=accepted,
            rejected=rejected,
            suppressed=suppressed,
            truths=truths,
            ledger=ledger,
            conflict_summary=conflicts,
            store=store,
            degradation_history=store.load_degradation_history(),
        )
        check = cross_module_check(
            sample_dir=sample_dir,
            manifest=manifest,
            format_priority=list(config["format_priority"]),
            accepted=accepted,
            store=store,
            report=report,
        )
        report["self_check"] = check
        report["report_checksum"] = canonical_checksum({k: v for k, v in report.items() if k != "report_checksum"})
        write_report_atomic(audit_path, report)
        return report
    finally:
        store.close()


def regenerate_audit(config_path: str | Path = "configs/run_config.json") -> dict[str, Any]:
    config = load_config(config_path)
    sample_dir, audit_path, ledger_path, hwm_path = _paths(config, config_path)
    if not ledger_path.exists():
        raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "cannot regenerate audit because ledger is missing", {"ledger_path": str(ledger_path)})
    manifest, truths, accepted, rejected, suppressed, ledger, conflicts = load_sample_inputs(sample_dir, config)
    store = LedgerStore.open(ledger_path, hwm_path)
    try:
        report = build_audit_report(
            config=config,
            manifest=manifest,
            accepted=accepted,
            rejected=rejected,
            suppressed=suppressed,
            truths=truths,
            ledger=ledger,
            conflict_summary=conflicts,
            store=store,
            degradation_history=store.load_degradation_history(),
        )
        check = cross_module_check(
            sample_dir=sample_dir,
            manifest=manifest,
            format_priority=list(config["format_priority"]),
            accepted=accepted,
            store=store,
            report=report,
        )
        report["self_check"] = check
        report["report_checksum"] = canonical_checksum({k: v for k, v in report.items() if k != "report_checksum"})
        write_report_atomic(audit_path, report)
        return report
    finally:
        store.close()


def replay_coordinate(
    config_path: str | Path,
    x: int,
    y: int,
    ledger_revision: int | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    _sample_dir, _audit_path, ledger_path, hwm_path = _paths(config, config_path)
    store = LedgerStore.open(ledger_path, hwm_path)
    try:
        uid = f"{x},{y}"
        records = store.load_probe_records()
        record = next((item for item in records if item["uid"] == uid), None)
        if record is None:
            raise AuditError(ErrorCode.INCONSISTENT, "coordinate has no committed probe record", {"uid": uid})
        if ledger_revision is not None and int(record.get("committed_revision", -1)) != ledger_revision:
            progress = store.conn.execute(
                "SELECT revision FROM progress_snapshot WHERE revision=?", (ledger_revision,)
            ).fetchone()
            if progress is None:
                raise AuditError(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    "requested ledger revision does not exist",
                    {"requested": ledger_revision},
                )
            if int(record.get("committed_revision", -1)) > ledger_revision:
                raise AuditError(
                    ErrorCode.INCONSISTENT,
                    "probe was not visible in requested ledger revision",
                    {"uid": uid, "requested": ledger_revision, "committed_revision": record.get("committed_revision")},
                )
        return {
            "coordinate": {"x": x, "y": y},
            "ledger_version": config["ledger_version"],
            "requested_ledger_revision": ledger_revision,
            "current_ledger_revision": store.head()["revision"],
            "record": record,
        }
    finally:
        store.close()


def write_failure_trace(config_path: str | Path, error: AuditError) -> Path:
    try:
        config = load_config(config_path)
        _sample_dir, audit_path, _ledger, _hwm = _paths(config, config_path)
        path = Path(config.get("failure_trace_path", audit_path.parent / "failure_trace.json"))
        if not path.is_absolute():
            config_file = Path(config_path).resolve()
            base_dir = config_file.parent.parent if config_file.parent.name == "configs" else Path.cwd()
            path = base_dir / path
    except AuditError:
        path = Path("artifacts/failure_trace.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "trace_version": "failure-trace-v1",
        "ok": False,
        "error": error.to_dict(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
