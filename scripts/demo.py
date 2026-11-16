"""可重复演示脚本：首次调度 -> 数据规模变更重调度 -> 快照整体恢复与重放。

运行（Windows）:
    python scripts\\demo.py
输出:
    控制台结构化总结，并写入 artifacts\\demo_report.json 与 snapshots 目录。
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gpu_diag.errors import BusinessError
from gpu_diag.logging_utils import get_logger
from gpu_diag.manifest import load_manifest
from gpu_diag.session import Session

MANIFEST_PATH = os.path.join("samples", "manifest.json")
INVALID_PATH = os.path.join("samples", "invalid_manifest.json")
SNAPSHOT_DIR = os.path.join("snapshots")
REPORT_PATH = os.path.join("artifacts", "demo_report.json")


def _compact_run(session):
    summary = session.summary()
    summary["job_decisions"] = [
        {
            "job_id": result.job_id,
            "admitted": result.admitted,
            "code": result.code,
            "code_name": result.code_name,
            "slots": list(result.slots),
            "per_card_mib": result.estimate.per_card_mib,
            "total_mib": result.estimate.total_mib,
            "reason_chain": [reason.to_dict() for reason in result.reasons],
        }
        for result in session.current_run.results
    ]
    return summary


def scenario_initial(logger):
    session = Session(load_manifest(MANIFEST_PATH))
    logger.info("scenario 1 initial schedule, parameter_version=%s", session.parameter_version)
    session.save_snapshot("baseline", SNAPSHOT_DIR)
    return session, {
        "scenario": "1_initial_schedule",
        "parameter_version": session.parameter_version,
        "snapshot": os.path.join(SNAPSHOT_DIR, "snapshot_baseline.json").replace(os.sep, "/"),
        "summary": _compact_run(session),
    }


def scenario_rescale(session, logger):
    session.update_scale(batch_size=128)
    logger.info("scenario 2 rescale, parameter_version=%s", session.parameter_version)
    return {
        "scenario": "2_rescale_reschedule",
        "parameter_version": session.parameter_version,
        "stale_versions": [item["parameter_version"] for item in session.stale_runs],
        "summary": _compact_run(session),
    }


def scenario_snapshot_restore(session, logger):
    snapshot_path = os.path.join(SNAPSHOT_DIR, "snapshot_baseline.json")
    session.update_scale(batch_size=200)
    restored, restore_info = Session.load_snapshot(snapshot_path)
    logger.info("scenario 3 restore info=%s", restore_info)
    replay = Session.replay_snapshot(snapshot_path)
    return {
        "scenario": "3_snapshot_restore_replay",
        "snapshot": snapshot_path.replace(os.sep, "/"),
        "restore": restore_info,
        "replay": replay,
        "restored_summary": _compact_run(restored),
    }


def scenario_failure_codes(logger):
    report = {"scenario": "4_failure_business_codes", "manifest_error": None,
              "estimate_error": None, "replay_error": None}
    try:
        load_manifest(INVALID_PATH)
    except BusinessError as exc:
        logger.info("invalid manifest rejected code=%s", exc.code)
        report["manifest_error"] = exc.to_dict()

    session = Session(load_manifest(MANIFEST_PATH))
    snapshot_path = session.save_snapshot("tamper-demo", SNAPSHOT_DIR)
    try:
        session.update_scale(model_dim=99999)
    except BusinessError as exc:
        report["estimate_error"] = exc.to_dict()

    tampered_path = os.path.join(SNAPSHOT_DIR, "snapshot_tampered.json")
    try:
        Session.replay_snapshot(
            snapshot_path,
            quota_overrides={"team-alpha": {"max_cards": 1}})
    except BusinessError as exc:
        logger.info("tampered replay divergence code=%s detail=%s",
                    exc.code, exc.details)
        report["replay_error"] = exc.to_dict()
        report["tampered_snapshot"] = tampered_path
    return report


def main():
    logger = get_logger()
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    session, first = scenario_initial(logger)
    second = scenario_rescale(session, logger)
    third = scenario_snapshot_restore(session, logger)
    fourth = scenario_failure_codes(logger)
    report = {"scenarios": [first, second, third, fourth]}
    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print("\n结构化报告已写入: {}".format(REPORT_PATH))


if __name__ == "__main__":
    main()
