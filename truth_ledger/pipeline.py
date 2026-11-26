from __future__ import annotations

import os
import signal
import shutil
from pathlib import Path
from typing import Any

from .adjudicate import adjudicate_from_disk
from .cache import CacheStore
from .errors import LedgerError, error_record
from .formats_dispatch import read_ledger
from .hashing import read_json, write_json_atomic
from .ingest import validate_csr
from .migrate import migrate_three_views, convert_ledger
from .model import CSRMatrix
from .recompute import recompute_all_views
from .selfcheck import run_self_check
from .state import STAGES, StateStore
from .writes import receive_write_batch
from .workspace import CaseWorkspace, initial_lineage


def migration_matrix(matrix: CSRMatrix, work_dir: Path) -> tuple[dict[int, Path], list[dict[str, Any]]]:
    primary_dir = work_dir / "primary"
    conversion_dir = work_dir / "conversions"
    conversion_dir.mkdir(parents=True, exist_ok=True)
    primary_paths = migrate_three_views(matrix, primary_dir)
    reports: list[dict[str, Any]] = []
    for source_version in (1, 2, 3):
        for target_version in (1, 2, 3):
            if source_version == target_version:
                continue
            if target_version == 2:
                target_path = conversion_dir / f"{source_version}-to-{target_version}"
            else:
                target_path = conversion_dir / f"{source_version}-to-{target_version}.json"
            reports.append(
                convert_ledger(
                    source_version,
                    primary_paths[source_version],
                    target_version,
                    target_path,
                    expected=matrix,
                )
            )
    return primary_paths, reports


class Pipeline:
    def __init__(self, case_root: str | Path) -> None:
        self.ws = CaseWorkspace(case_root).ensure_dirs()
        self.state = StateStore(self.ws)
        self.cache = CacheStore(self.ws.cache_dir)

    def recover(self) -> tuple[dict[str, Any] | None, dict[str, int]]:
        latest = self.state.latest()
        committed_keys = self.cache.reconcile_to_snapshot(latest)
        gc_report = self.cache.gc_uncommitted(committed_keys)
        self.state.reset_uncommitted_work()
        self._reconcile_committed_state(latest)
        return latest, gc_report

    def _reconcile_committed_state(self, latest: dict[str, Any] | None) -> None:
        if latest is None:
            if self.ws.committed_csr_path.exists():
                self.ws.committed_csr_path.unlink()
            if self.ws.lineage_path.exists():
                self.ws.lineage_path.unlink()
            return

        expected_digest = str(latest["csr_digest"])
        committed_is_valid = False
        if self.ws.committed_csr_path.exists():
            try:
                committed = CSRMatrix.from_json_dict(read_json(self.ws.committed_csr_path))
                committed_is_valid = committed.digest == expected_digest
            except Exception:
                committed_is_valid = False

        if not committed_is_valid:
            csr_dict = latest.get("csr_dict")
            lineage = latest.get("lineage")
            if not isinstance(csr_dict, dict) or not isinstance(lineage, dict):
                raise LedgerError(
                    "E_INCONSISTENT",
                    "latest snapshot cannot restore committed CSR state",
                    stage="recovery",
                    details={"snapshot_version": latest.get("snapshot_version")},
                )
            write_json_atomic(self.ws.committed_csr_path, csr_dict)
            self.ws.save_lineage(
                {str(key): int(value) for key, value in lineage.items()},
                int(latest.get("write_version", 0)),
            )

    def _kill_at(self, crash_at: str | None, point: str) -> None:
        if crash_at == point:
            os.kill(os.getpid(), signal.SIGKILL)

    def _record_failure(
        self, exc: BaseException, target_version: int, injection: str | None
    ) -> None:
        record = error_record(exc)
        record["target_write_version"] = target_version
        if injection:
            record["injection"] = injection
        self.state.append_stage_record(record)

    def run_through(
        self,
        target_version: int,
        *,
        crash_at: str | None = None,
        injection: str | None = None,
    ) -> dict[str, Any]:
        target_version = int(target_version)
        try:
            return self._run_through(
                target_version, crash_at=crash_at, injection=injection
            )
        except BaseException as exc:
            self._record_failure(exc, target_version, injection)
            raise

    def _run_through(
        self,
        target_version: int,
        *,
        crash_at: str | None,
        injection: str | None,
    ) -> dict[str, Any]:
        latest, gc_report = self.recover()
        if (
            latest is not None
            and int(latest["write_version"]) == target_version
            and int(latest["stage_index"]) == 4
        ):
            if not self.ws.self_check_path.exists():
                final_check = run_self_check(self.ws.root)
                write_json_atomic(self.ws.self_check_path, final_check)
            return {
                "ok": True,
                "status": "already_complete",
                "target_write_version": target_version,
                "recovery_gc": gc_report,
                "latest_snapshot": latest,
                "adjudication": read_json(self.ws.adjudication_path)
                if self.ws.adjudication_path.exists()
                else None,
                "self_check": read_json(self.ws.self_check_path)
                if self.ws.self_check_path.exists()
                else None,
            }
        if latest is not None and int(latest["write_version"]) > target_version:
            raise LedgerError(
                "E_WRITE_CONFLICT",
                "cannot move state machine backwards to an older write version",
                stage="pipeline",
                details={
                    "requested": target_version,
                    "latest_committed": int(latest["write_version"]),
                },
            )

        event_path = self.ws.events_dir / f"write-{target_version:03d}.json"
        event = read_json(event_path)
        if injection == "write_hash_mismatch":
            event["writes"][0]["content_hash"] = "sha256:" + "0" * 64

        completed_stage = (
            int(latest["stage_index"])
            if latest is not None and int(latest["write_version"]) == target_version
            else 0
        )
        stage_runs: list[dict[str, Any]] = []
        initial_snapshot_version = None if latest is None else latest["snapshot_version"]
        matrix: CSRMatrix | None = None
        interval_versions: dict[str, int] = {}
        recompute_report: dict[str, Any] | None = None

        for stage_index, stage_name in STAGES:
            if completed_stage >= stage_index:
                stage_runs.append(
                    {
                        "stage_index": stage_index,
                        "stage": stage_name,
                        "status": "skipped_committed",
                    }
                )
                if stage_index == 1:
                    matrix = self.ws.load_committed_csr()
                    interval_versions = {
                        str(k): int(v)
                        for k, v in self.ws.load_lineage()["interval_versions"].items()
                    }
                if stage_index == 3 and latest is not None:
                    recompute_report = latest.get("report")
                continue

            stage_runs.append(
                {
                    "stage_index": stage_index,
                    "stage": stage_name,
                    "status": "executed",
                }
            )

            if stage_index == 1:
                if latest is not None:
                    base_matrix = self.ws.load_committed_csr()
                else:
                    base_matrix = self.ws.load_initial_csr()
                if injection == "struct_nonmonotonic_row_ptr":
                    base_matrix.row_ptr = base_matrix.row_ptr.copy()
                    anchor = min(2, base_matrix.nrows - 1)
                    base_matrix.row_ptr[anchor] = base_matrix.row_ptr[anchor + 1] + 1
                if injection == "struct_column_oob":
                    base_matrix.col_idx = base_matrix.col_idx.copy()
                    base_matrix.col_idx[0] = base_matrix.ncols + 99
                validate_csr(base_matrix, stage="parse_validate")

                ledger_dir = (
                    self.ws.committed_ledgers_dir
                    if latest is not None and any(self.ws.committed_ledgers_dir.iterdir())
                    else self.ws.initial_ledgers_dir
                )
                parsed_views: dict[str, Any] = {}
                for version in (1, 2, 3):
                    path = (
                        ledger_dir / "v2"
                        if version == 2
                        else ledger_dir / f"v{version}_ledger.json"
                    )
                    if path.exists():
                        _view_matrix, view_info = read_ledger(version, path)
                        parsed_views[f"v{version}"] = {
                            key: value for key, value in view_info.items() if key != "path"
                        }

                matrix, receipt = receive_write_batch(event, base_matrix)
                if self.ws.lineage_path.exists():
                    lineage = self.ws.load_lineage()["interval_versions"]
                else:
                    lineage = initial_lineage(base_matrix)
                interval_versions = {str(k): int(v) for k, v in lineage.items()}
                for invalidated in receipt["invalidated_intervals"]:
                    interval_versions[invalidated["interval_id"]] = target_version

                stage_work = self.ws.work_dir / "stage1"
                stage_work.mkdir(parents=True, exist_ok=True)
                write_json_atomic(stage_work / "csr.json", matrix.to_json_dict())
                write_json_atomic(stage_work / "lineage.json", interval_versions)
                write_json_atomic(stage_work / "receipt.json", receipt)
                self._kill_at(crash_at, "parse_before_commit")

                self.ws.save_committed_csr(matrix)
                self.ws.save_lineage(interval_versions, target_version)
                write_json_atomic(
                    self.ws.reports_dir / f"write-w{target_version:03d}.json", receipt
                )
                self._kill_at(crash_at, "parse_after_publish_before_snapshot")
                carry_keys = list((latest or {}).get("cache_keys", []))
                latest = self.state.commit_stage(
                    stage_index=1,
                    stage=stage_name,
                    write_version=target_version,
                    all_intervals=[
                        matrix.interval_id(s, e) for s, e in matrix.interval_bounds()
                    ],
                    completed_intervals=[
                        matrix.interval_id(s, e) for s, e in matrix.interval_bounds()
                    ],
                    csr_digest=matrix.digest,
                    cache_keys=carry_keys,
                    report=receipt,
                    extra={
                        "parsed_previous_views": parsed_views,
                        "csr_dict": matrix.to_json_dict(),
                        "lineage": interval_versions,
                    },
                )
                self.state.append_stage_record(
                    {
                        "ok": True,
                        "stage": stage_name,
                        "write_version": target_version,
                        "snapshot_version": latest["snapshot_version"],
                        "invalidated_intervals": [
                            item["interval_id"] for item in receipt["invalidated_intervals"]
                        ],
                    }
                )

            elif stage_index == 2:
                assert matrix is not None
                stage_work = self.ws.work_dir / "stage2"
                primary_paths, conversion_reports = migration_matrix(matrix, stage_work)
                self._kill_at(crash_at, "migrate_before_commit")

                v2_target = self.ws.ledger_path(2)
                if v2_target.exists():
                    shutil.rmtree(v2_target)
                shutil.copy2(primary_paths[1], self.ws.ledger_path(1))
                shutil.copytree(primary_paths[2], v2_target)
                shutil.copy2(primary_paths[3], self.ws.ledger_path(3))
                migration_summary = {
                    "conversion_count": len(conversion_reports),
                    "semantic_equal": all(item["semantic_equal"] for item in conversion_reports),
                    "completed": [
                        item for report in conversion_reports for item in report["completed"]
                    ],
                    "discarded": [
                        item for report in conversion_reports for item in report["discarded"]
                    ],
                    "conversions": [
                        {
                            "source": report["source_version"],
                            "target": report["target_version"],
                            "semantic_equal": report["semantic_equal"],
                            "completed_count": len(report["completed"]),
                            "discarded_count": len(report["discarded"]),
                        }
                        for report in conversion_reports
                    ],
                }
                write_json_atomic(
                    self.ws.reports_dir / f"migration-w{target_version:03d}.json",
                    {**migration_summary, "reports": conversion_reports},
                )
                latest = self.state.commit_stage(
                    stage_index=2,
                    stage=stage_name,
                    write_version=target_version,
                    all_intervals=[
                        matrix.interval_id(s, e) for s, e in matrix.interval_bounds()
                    ],
                    completed_intervals=[
                        matrix.interval_id(s, e) for s, e in matrix.interval_bounds()
                    ],
                    csr_digest=matrix.digest,
                    cache_keys=list((latest or {}).get("cache_keys", [])),
                    report=migration_summary,
                )
                self.state.append_stage_record(
                    {
                        "ok": True,
                        "stage": stage_name,
                        "write_version": target_version,
                        "snapshot_version": latest["snapshot_version"],
                        "conversion_count": len(conversion_reports),
                    }
                )

            elif stage_index == 3:
                assert matrix is not None
                views = {
                    version: read_ledger(version, self.ws.ledger_path(version))[0]
                    for version in (1, 2, 3)
                }

                def hook(event_name: str, payload: dict[str, Any]) -> None:
                    if crash_at == "recompute_after_first_new" and event_name == "interval_recomputed":
                        self._kill_at(crash_at, "recompute_after_first_new")

                recompute_report = recompute_all_views(
                    views,
                    cache=self.cache,
                    dense_b=self.ws.load_rhs()[0],
                    sparse_b=self.ws.load_rhs()[1],
                    interval_versions=interval_versions,
                    injection=injection if injection == "column_oob" else None,
                    event_hook=hook,
                )
                current_keys = sorted(
                    {
                        item["cache_key"]
                        for view_report in recompute_report["views"].values()
                        for item in view_report["interval_results"]
                    }
                )
                recompute_report["cache_keys"] = current_keys
                self.cache.write_manifest(
                    current_keys, snapshot_version=self.state.next_version()
                )
                cache_gc = self.cache.gc_uncommitted(current_keys)
                recompute_report["cache_gc_after_commit"] = cache_gc
                write_json_atomic(
                    self.ws.reports_dir / f"recompute-w{target_version:03d}.json",
                    recompute_report,
                )
                latest = self.state.commit_stage(
                    stage_index=3,
                    stage=stage_name,
                    write_version=target_version,
                    all_intervals=recompute_report["all_intervals"],
                    completed_intervals=recompute_report["all_intervals"],
                    csr_digest=matrix.digest,
                    cache_keys=current_keys,
                    report=recompute_report,
                )
                self.cache.write_manifest(
                    current_keys,
                    snapshot_version=int(latest["snapshot_version"]),
                )
                self.state.append_stage_record(
                    {
                        "ok": True,
                        "stage": stage_name,
                        "write_version": target_version,
                        "snapshot_version": latest["snapshot_version"],
                        "recomputed": recompute_report["recomputed_intervals"],
                        "reused": recompute_report["reused_intervals"],
                    }
                )

            else:
                adjudication = adjudicate_from_disk(self.ws.root)
                write_json_atomic(self.ws.adjudication_path, adjudication)
                pre_self_check = run_self_check(
                    self.ws.root, require_committed_summary=False
                )
                stage4_report = {
                    "adjudication": {
                        key: value
                        for key, value in adjudication.items()
                        if key not in ("views", "intervals")
                    },
                    "self_check_pre_commit": pre_self_check,
                }
                latest = self.state.commit_stage(
                    stage_index=4,
                    stage=stage_name,
                    write_version=target_version,
                    all_intervals=pre_self_check["all_intervals"],
                    completed_intervals=pre_self_check["all_intervals"],
                    csr_digest=self.ws.load_committed_csr().digest,
                    cache_keys=list((latest or {}).get("cache_keys", [])),
                    report=stage4_report,
                )
                final_self_check = run_self_check(self.ws.root)
                write_json_atomic(self.ws.self_check_path, final_self_check)
                self.state.append_stage_record(
                    {
                        "ok": True,
                        "stage": stage_name,
                        "write_version": target_version,
                        "snapshot_version": latest["snapshot_version"],
                        "self_check_ok": True,
                    }
                )

        assert latest is not None
        return {
            "ok": True,
            "status": "completed",
            "target_write_version": target_version,
            "resumed_from_snapshot_version": initial_snapshot_version,
            "recovery_gc": gc_report,
            "stage_runs": stage_runs,
            "latest_snapshot_version": latest["snapshot_version"],
            "recompute": None
            if recompute_report is None
            else {
                "recomputed_intervals": recompute_report["recomputed_intervals"],
                "reused_intervals": recompute_report["reused_intervals"],
                "trigger_write_version": recompute_report["trigger_write_version"],
            },
            "adjudication_path": str(self.ws.adjudication_path),
            "self_check_path": str(self.ws.self_check_path),
            "self_check": read_json(self.ws.self_check_path)
            if self.ws.self_check_path.exists()
            else None,
        }
