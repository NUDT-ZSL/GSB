"""SQLite 回滚执行账本：幂等写入、版本冲突检测、崩溃恢复与一致性重放校验。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import E_LEDGER, E_REPLAY, E_VERSION, OpsError
from .graph import topo_order
from .models import NormalizedTicket, RollbackPlan
from .risk import RiskReport, compute_risk

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_version INTEGER NOT NULL,
    ticket_revision INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    change_key TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    risk_error INTEGER NOT NULL,
    executed_at TEXT NOT NULL,
    UNIQUE (plan_version, ticket_revision, step_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DeterministicClock:
    """确定性时钟：固定起点、每次调用递增固定步长，保证演示与测试可复现。"""

    def __init__(self, base="2026-01-01T00:00:00", step_seconds=1):
        self._base = datetime.fromisoformat(base)
        self._step = timedelta(seconds=step_seconds)
        self._ticks = 0

    def __call__(self):
        value = self._base + self._ticks * self._step
        self._ticks += 1
        return value.isoformat()


def open_ledger(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _meta_get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    plan_version: int
    ticket_revision: int
    step_id: str
    change_key: str
    action: str
    result: str
    risk_score: int
    risk_error: int
    executed_at: str
    idempotent_replay: bool = False

    def to_dict(self):
        return dict(self.__dict__)


def _row_to_entry(row, replay=False) -> LedgerEntry:
    return LedgerEntry(
        seq=row["seq"], plan_version=row["plan_version"],
        ticket_revision=row["ticket_revision"], step_id=row["step_id"],
        change_key=row["change_key"], action=row["action"], result=row["result"],
        risk_score=row["risk_score"], risk_error=row["risk_error"],
        executed_at=row["executed_at"], idempotent_replay=replay,
    )


def fetch_entries(conn, plan_version=None, ticket_revision=None) -> list:
    sql = "SELECT * FROM ledger"
    clauses, params = [], []
    if plan_version is not None:
        clauses.append("plan_version = ?")
        params.append(plan_version)
    if ticket_revision is not None:
        clauses.append("ticket_revision = ?")
        params.append(ticket_revision)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq"
    return [_row_to_entry(r) for r in conn.execute(sql, params).fetchall()]


def _check_version_conflict(conn, plan_version, ticket_revision):
    latest_plan = _meta_get(conn, "latest_plan_version")
    latest_rev = _meta_get(conn, "latest_ticket_revision")
    if latest_plan is None:
        return
    latest_plan, latest_rev = int(latest_plan), int(latest_rev)
    if (plan_version, ticket_revision) < (latest_plan, latest_rev):
        raise OpsError(
            E_VERSION, "ledger version conflict: stale plan/ticket version",
            {"attempted": {"plan_version": plan_version, "ticket_revision": ticket_revision},
             "latest": {"plan_version": latest_plan, "ticket_revision": latest_rev}})


class RollbackExecutor:
    """按拓扑序执行回滚计划，逐步写入账本；重复执行幂等返回已记录结果。"""

    def __init__(self, conn, clock=None, action_runner=None, crash_hook=None):
        self.conn = conn
        self.clock = clock or DeterministicClock()
        self.action_runner = action_runner or default_action_runner
        self.crash_hook = crash_hook  # crash_hook(step_index) 在写入前触发，模拟强制终止

    def execute_plan(self, plan: RollbackPlan, ticket: NormalizedTicket,
                     report: RiskReport) -> list:
        _check_version_conflict(self.conn, plan.plan_version, ticket.revision)
        order = topo_order(plan)
        step_map = plan.step_map()
        entries = []
        for index, step_id in enumerate(order):
            step = step_map[step_id]
            existing = self.conn.execute(
                "SELECT * FROM ledger WHERE plan_version=? AND ticket_revision=? AND step_id=?",
                (plan.plan_version, ticket.revision, step_id)).fetchone()
            if existing is not None:
                entries.append(_row_to_entry(existing, replay=True))
                continue
            result = self.action_runner(step, ticket)
            if self.crash_hook is not None:
                self.crash_hook(index)  # 模拟在写入账本前被强制终止
            executed_at = self.clock()
            with self.conn:  # 每步独立事务：只有完整写入的行才算完成
                self.conn.execute(
                    "INSERT INTO ledger (plan_version, ticket_revision, step_id, change_key,"
                    " action, result, risk_score, risk_error, executed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (plan.plan_version, ticket.revision, step.step_id, step.change_key,
                     step.action, result, report.total_score, report.total_error_bound,
                     executed_at))
                _meta_set(self.conn, "latest_plan_version", str(plan.plan_version))
                _meta_set(self.conn, "latest_ticket_revision", str(ticket.revision))
                versions = json.loads(_meta_get(self.conn, "executed_versions") or "{}")
                versions[f"{plan.plan_version}:{ticket.revision}"] = {
                    "ticket_fingerprint": ticket.fingerprint(),
                    "plan_fingerprint": plan.fingerprint(),
                    "risk_score": report.total_score,
                    "risk_error": report.total_error_bound,
                }
                _meta_set(self.conn, "executed_versions", json.dumps(versions, sort_keys=True))
            row = self.conn.execute(
                "SELECT * FROM ledger WHERE plan_version=? AND ticket_revision=? AND step_id=?",
                (plan.plan_version, ticket.revision, step_id)).fetchone()
            entries.append(_row_to_entry(row))
        return entries


def default_action_runner(step, ticket):
    item = ticket.item_map().get(step.change_key)
    old = "null" if item is None else json.dumps(item.old_value, ensure_ascii=False, sort_keys=True)
    return f"{step.action}:{step.change_key}<-{old}"


def verify_ledger(conn, plan: RollbackPlan, ticket_revision: int) -> None:
    """账本记录须与回滚计划步骤逐项对应；缺失或多余均抛 E_LEDGER。"""
    rows = fetch_entries(conn, plan_version=plan.plan_version, ticket_revision=ticket_revision)
    ledger_ids = sorted(r.step_id for r in rows)
    plan_ids = sorted(s.step_id for s in plan.steps)
    missing = sorted(set(plan_ids) - set(ledger_ids))
    extra = sorted(set(ledger_ids) - set(plan_ids))
    if missing or extra:
        raise OpsError(E_LEDGER, "ledger entries diverge from rollback plan",
                       {"missing_steps": missing, "extra_steps": extra,
                        "plan_version": plan.plan_version,
                        "ticket_revision": ticket_revision})


@dataclass(frozen=True)
class StepRecoveryState:
    step_id: str
    status: str          # completed | unknown
    ledger_seq: int | None
    require_rerun: bool


@dataclass(frozen=True)
class RecoveryReport:
    plan_version: int
    ticket_revision: int
    steps: tuple             # StepRecoveryState，按拓扑序
    exit_report_generated: bool  # 恢复路径永不生成退出报告
    replay_verified: bool

    def to_dict(self):
        return {
            "plan_version": self.plan_version,
            "ticket_revision": self.ticket_revision,
            "steps": [s.__dict__ for s in self.steps],
            "exit_report_generated": self.exit_report_generated,
            "replay_verified": self.replay_verified,
        }


def recover(conn, plan: RollbackPlan, ticket: NormalizedTicket) -> RecoveryReport:
    """强制终止后的恢复：仅承认账本中完整写入的步骤，其余标记 unknown 并要求重跑；
    随后按账本记录的版本重算风险与顺序并逐项重放校验（不可复现抛 E_REPLAY）。"""
    _check_version_conflict_recover(conn, plan, ticket)
    order = topo_order(plan)
    rows = {r.step_id: r for r in fetch_entries(
        conn, plan_version=plan.plan_version, ticket_revision=ticket.revision)}

    states = []
    for step_id in order:
        row = rows.get(step_id)
        if row is None:
            states.append(StepRecoveryState(step_id=step_id, status="unknown",
                                            ledger_seq=None, require_rerun=True))
        else:
            states.append(StepRecoveryState(step_id=step_id, status="completed",
                                            ledger_seq=row.seq, require_rerun=False))

    replay_verified = _replay_check(conn, plan, ticket, order, rows)
    return RecoveryReport(
        plan_version=plan.plan_version,
        ticket_revision=ticket.revision,
        steps=tuple(states),
        exit_report_generated=False,
        replay_verified=replay_verified,
    )


def _check_version_conflict_recover(conn, plan, ticket):
    versions = json.loads(_meta_get(conn, "executed_versions") or "{}")
    record = versions.get(f"{plan.plan_version}:{ticket.revision}")
    if record is None:
        raise OpsError(E_VERSION, "no ledger record for requested plan/ticket version",
                       {"plan_version": plan.plan_version, "ticket_revision": ticket.revision,
                        "recorded_versions": sorted(versions)})
    if record["ticket_fingerprint"] != ticket.fingerprint():
        raise OpsError(E_VERSION, "ticket fingerprint mismatch with ledger record",
                       {"plan_version": plan.plan_version, "ticket_revision": ticket.revision})
    if record["plan_fingerprint"] != plan.fingerprint():
        raise OpsError(E_VERSION, "plan fingerprint mismatch with ledger record",
                       {"plan_version": plan.plan_version, "ticket_revision": ticket.revision})


def _replay_check(conn, plan, ticket, order, rows) -> bool:
    """按账本记录的版本重新计算风险分数与回滚顺序，与账本逐项比较。"""
    report = compute_risk(ticket)
    recomputed_order = topo_order(plan)
    if list(order) != list(recomputed_order):
        raise OpsError(E_REPLAY, "recomputed rollback order diverges",
                       {"step_id": "<order>", "basis": "topo_order mismatch",
                        "ledger": list(order), "recomputed": list(recomputed_order)})

    ledger_rows = fetch_entries(conn, plan_version=plan.plan_version,
                                ticket_revision=ticket.revision)
    completed_ids = [r.step_id for r in ledger_rows]
    expected_prefix = [sid for sid in recomputed_order if sid in set(completed_ids)]
    if completed_ids != expected_prefix:
        raise OpsError(E_REPLAY, "ledger execution order is not reproducible",
                       {"step_id": completed_ids[len(expected_prefix) - 1]
                        if completed_ids else "<none>",
                        "basis": "ledger seq order vs recomputed topo order",
                        "ledger": completed_ids, "recomputed": expected_prefix})

    for row in ledger_rows:
        if row.risk_score != report.total_score or row.risk_error != report.total_error_bound:
            raise OpsError(E_REPLAY, "risk snapshot not reproducible",
                           {"step_id": row.step_id,
                            "basis": "recomputed risk vs ledger snapshot",
                            "ledger": {"score": row.risk_score, "error": row.risk_error},
                            "recomputed": {"score": report.total_score,
                                           "error": report.total_error_bound}})
        step = plan.step_map().get(row.step_id)
        if step is None or step.change_key != row.change_key or step.action != row.action:
            raise OpsError(E_REPLAY, "ledger step payload not reproducible",
                           {"step_id": row.step_id,
                            "basis": "plan step vs ledger row"})
    return True
