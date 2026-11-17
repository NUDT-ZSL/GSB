"""强制终止后的会话恢复。

恢复只能读取 SQLite 中已完整落盘的记录：
* COMPLETE 会话：恢复完整报告；
* RUNNING 会话：标记为 UNKNOWN，未完成处理要求重跑，不生成退出报告；
* 恢复报告列出恢复簇数、游标位置与版本差异，并显式声明无法生成退出报告。
"""
from __future__ import annotations

from dataclasses import dataclass

from .persistence import STATUS_COMPLETE, STATUS_RUNNING, STATUS_UNKNOWN, WorkbenchStore


@dataclass(frozen=True)
class RecoveryResult:
    session_id: str
    status: str
    recovered_clusters: int
    cursor: int
    observation_total: int
    topology_version: int
    rule_version: int
    version_diff_count: int
    injection_count: int
    rerun_required: bool
    note: str


def recover_session(store: WorkbenchStore, session_id: str) -> RecoveryResult:
    sessions = {row["session_id"]: row for row in store.list_sessions()}
    if session_id not in sessions:
        raise KeyError(f"unknown session: {session_id}")
    row = sessions[session_id]
    report = store.load_report(session_id)
    diffs = store.load_diffs(session_id)
    injections = store.load_injections(session_id)

    if row["status"] == STATUS_RUNNING:
        store.mark_unknown_on_recovery(session_id)
        result = RecoveryResult(
            session_id=session_id,
            status=STATUS_UNKNOWN,
            recovered_clusters=0,
            cursor=row["observation_cursor"],
            observation_total=row["observation_total"],
            topology_version=row["topology_version"],
            rule_version=row["rule_version"],
            version_diff_count=len(diffs),
            injection_count=len(injections),
            rerun_required=True,
            note=(
                "process was force-terminated mid-run; incomplete state marked UNKNOWN; "
                "no exit report can be produced, rerun required"
            ),
        )
        store.log_recovery(session_id, result.note)
        return result

    if row["status"] == STATUS_UNKNOWN:
        result = RecoveryResult(
            session_id=session_id,
            status=STATUS_UNKNOWN,
            recovered_clusters=0,
            cursor=row["observation_cursor"],
            observation_total=row["observation_total"],
            topology_version=row["topology_version"],
            rule_version=row["rule_version"],
            version_diff_count=len(diffs),
            injection_count=len(injections),
            rerun_required=True,
            note="session previously marked UNKNOWN; rerun required",
        )
        store.log_recovery(session_id, result.note)
        return result

    assert report is not None and row["status"] == STATUS_COMPLETE
    result = RecoveryResult(
        session_id=session_id,
        status=STATUS_COMPLETE,
        recovered_clusters=len(report.clusters),
        cursor=row["observation_cursor"],
        observation_total=row["observation_total"],
        topology_version=row["topology_version"],
        rule_version=row["rule_version"],
        version_diff_count=len(diffs),
        injection_count=len(injections),
        rerun_required=False,
        note="full snapshot recovered from SQLite",
    )
    store.log_recovery(session_id, result.note)
    return result


def render_recovery_report(result: RecoveryResult) -> str:
    lines = [
        "=== recovery report ===",
        f"session_id={result.session_id}",
        f"status={result.status}",
        f"recovered_clusters={result.recovered_clusters}",
        f"cursor={result.cursor}/{result.observation_total}",
        f"topology_version={result.topology_version}",
        f"rule_version={result.rule_version}",
        f"version_diffs={result.version_diff_count}",
        f"injections={result.injection_count}",
        f"rerun_required={result.rerun_required}",
        f"note={result.note}",
    ]
    return "\n".join(lines)
