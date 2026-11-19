"""Failover / failback adjudication executor with crash-safe commits.

Commit protocol per action (all state transitions are transactional):
  1. under the file lock, record a 'pending' intent (own transaction);
  2. dispatch the action to the loopback HTTP target;
  3. under the file lock, write watermark + stage + clock watermark +
     decision log and mark the intent 'committed' in one transaction.
A crash after step 1 leaves a half-committed intent that recovery replays;
a crash after step 3 is fully committed and never re-executed.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import numpy as np

from .clock import LogicalClock
from .errors import BizError, Err
from .locks import FileLock
from .models import UnifiedPlan, canonical_json, digest_of
from .store import Store


class SimulatedCrash(Exception):
    """Raised by the crash hook to emulate an abrupt process kill."""


def topo_order(plan: UnifiedPlan) -> list:
    """Dependency topological order; raises DEPENDENCY_CYCLE on cycles."""
    upstream = {name: tuple(sp.upstream) for name, sp in plan.services.items()}
    order: list = []
    state: dict = {}
    stack: list = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for up in upstream.get(node, ()):
            if up not in plan.services:
                continue
            if state.get(up, 0) == 1:
                cycle = stack[stack.index(up):] + [up]
                raise BizError(Err.DEPENDENCY_CYCLE, "dependency cycle detected",
                               cycle=cycle)
            if state.get(up, 0) == 0:
                visit(up)
        stack.pop()
        state[node] = 2
        order.append(node)

    for name in sorted(upstream):
        if state.get(name, 0) == 0:
            visit(name)
    return order


class Orchestrator:
    def __init__(self, store: Store, clock: LogicalClock, target_url: str,
                 lock_path=None, http_timeout: float = 2.0,
                 crash_hook=None) -> None:
        self.store = store
        self.clock = clock
        self.target_url = target_url.rstrip("/")
        self.http_timeout = http_timeout
        self.crash_hook = crash_hook or (lambda phase, ctx: None)
        self.lock_path = lock_path or str(store.db_path) + ".lock"
        # loopback-only: never honour ambient proxy settings
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.target_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.http_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ConnectionRefusedError):
                raise BizError(Err.TARGET_CONN_REFUSED,
                               "action target refused connection",
                               url=self.target_url + path) from exc
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise BizError(Err.TARGET_TIMEOUT,
                               "action target timed out",
                               url=self.target_url + path,
                               timeout_s=self.http_timeout) from exc
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise BizError(Err.TARGET_TIMEOUT,
                           "action target timed out",
                           url=self.target_url + path,
                           timeout_s=self.http_timeout) from exc

    def _get_health(self) -> bool:
        try:
            with self._opener.open(self.target_url + "/health",
                                   timeout=self.http_timeout) as resp:
                return bool(json.loads(resp.read().decode("utf-8"))["healthy"])
        except Exception:
            return False

    def _dispatch_action(self, service: str, action_id: str,
                         version: str, op: str) -> None:
        resp = self._post("/action", {
            "service": service, "action_id": action_id,
            "version": version, "op": op,
        })
        if resp.get("echo_version") != version:
            raise BizError(Err.TARGET_VERSION_ECHO_MISMATCH,
                           "target echoed a different action version",
                           service=service, action_id=action_id,
                           expected=version, echoed=resp.get("echo_version"))

    def _record_decision(self, conn, plan_version: int, service: str,
                         decision: str, trigger: str, clock_ms: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO decision_log"
            " (plan_version, service, decision, trigger_source, clock_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (plan_version, service, decision, trigger, clock_ms),
        )

    def _existing_decision_clock(self, plan_version: int, service: str,
                                 decision: str):
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT clock_ms FROM decision_log"
                " WHERE plan_version = ? AND service = ? AND decision = ?"
                " ORDER BY seq LIMIT 1",
                (plan_version, service, decision)).fetchone()
            return row["clock_ms"] if row else None
        finally:
            conn.close()

    def _bump_clock_watermark(self, conn, clock_ms: int) -> None:
        conn.execute(
            "INSERT INTO clock_watermark (id, clock_ms) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET clock_ms = excluded.clock_ms",
            (clock_ms,),
        )

    def _commit_action(self, plan_version: int, service: str, action: dict,
                       clock_ms: int) -> str:
        """Run the intent -> dispatch -> commit protocol for one action."""
        payload = {"service": service, "action_id": action["action_id"],
                   "version": "v{}:{}".format(action["generation"],
                                              action["action_id"]),
                   "op": action["op"]}
        digest = digest_of(payload)
        with FileLock(self.lock_path):
            conn = self.store.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT action_version, content_digest FROM action_watermark"
                    " WHERE plan_version = ? AND service = ? AND action_id = ?",
                    (plan_version, service, action["action_id"]),
                ).fetchone()
                if row is not None:
                    conn.commit()
                    if row["content_digest"] != digest:
                        raise BizError(
                            Err.DIGEST_MISMATCH,
                            "committed action digest differs from resubmission",
                            service=service, action_id=action["action_id"],
                            committed_digest=row["content_digest"],
                            submitted_digest=digest)
                    return "already_committed"
                conn.execute(
                    "INSERT OR IGNORE INTO action_intent"
                    " (plan_version, service, action_id, action_version,"
                    "  status, payload_json)"
                    " VALUES (?, ?, ?, ?, 'pending', ?)",
                    (plan_version, service, action["action_id"],
                     payload["version"], canonical_json(payload)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

            self.crash_hook("after_intent", payload)
            self._dispatch_action(service, action["action_id"],
                                  payload["version"], action["op"])
            self.crash_hook("after_dispatch", payload)

            conn = self.store.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO action_watermark"
                    " (plan_version, service, action_id, action_version,"
                    "  content_digest, clock_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (plan_version, service, action["action_id"],
                     payload["version"], digest, clock_ms),
                )
                conn.execute(
                    "UPDATE action_intent SET status = 'committed'"
                    " WHERE plan_version = ? AND service = ? AND action_id = ?",
                    (plan_version, service, action["action_id"]),
                )
                self._bump_clock_watermark(conn, clock_ms)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            self.crash_hook("after_commit", payload)
        return "committed"

    def _set_stage(self, plan_version: int, service: str, stage: str,
                   clock_ms: int, rung=None) -> None:
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if rung is None:
                conn.execute(
                    "UPDATE service_stage SET stage = ?"
                    " WHERE plan_version = ? AND service = ?",
                    (stage, plan_version, service))
            else:
                conn.execute(
                    "UPDATE service_stage SET stage = ?, traffic_rung = ?"
                    " WHERE plan_version = ? AND service = ?",
                    (stage, rung, plan_version, service))
            self._bump_clock_watermark(conn, clock_ms)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _stage_of(self, conn, plan_version: int, service: str):
        row = conn.execute(
            "SELECT stage, traffic_rung FROM service_stage"
            " WHERE plan_version = ? AND service = ?",
            (plan_version, service)).fetchone()
        return (row["stage"], row["traffic_rung"]) if row else (None, 0)

    def run_failover(self, plan: UnifiedPlan, plan_version: int) -> list:
        order = topo_order(plan)
        conn = self.store.connect()
        try:
            stages = {s: self._stage_of(conn, plan_version, s)[0]
                      for s in order}
        finally:
            conn.close()
        for service in order:
            sp = plan.services[service]
            if stages.get(service) == "failed_over":
                continue
            prior = self._existing_decision_clock(plan_version, service,
                                                  "FAILOVER_START")
            now = prior if prior is not None else self.clock.now_ms()
            not_ready = [u for u in sp.upstream
                         if stages.get(u) != "failed_over"]
            if not_ready:
                self._log_decision(plan_version, service, "WAIT",
                                   "upstream_not_ready:" + ",".join(not_ready),
                                   now)
                continue
            trigger = ("initial_fault" if not sp.upstream
                       else "upstream_ready:" + ",".join(sp.upstream))
            self._log_decision(plan_version, service, "FAILOVER_START",
                               trigger, now)
            failed = None
            for action in sp.actions:
                try:
                    self._commit_action(plan_version, service,
                                        _action_dict(action), now)
                except BizError as exc:
                    failed = exc
                    break
            if failed is not None:
                self._set_stage(plan_version, service, "failed", now)
                stages[service] = "failed"
                self._log_decision(plan_version, service, "FAIL",
                                   failed.code, now)
                continue
            self._set_stage(plan_version, service, "failed_over", now)
            stages[service] = "failed_over"
            self._log_decision(plan_version, service, "FAILED_OVER",
                               trigger, now)
        return self.decision_chain(plan_version)

    def _log_decision(self, plan_version: int, service: str, decision: str,
                      trigger: str, clock_ms: int) -> None:
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._record_decision(conn, plan_version, service, decision,
                                  trigger, clock_ms)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _advance_rung(self, plan_version: int, service: str, rung: int,
                      pct: int, clock_ms: int) -> None:
        """Persist ladder rung and its ADVANCE decision in one transaction."""
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE service_stage SET stage = 'failed_over',"
                " traffic_rung = ? WHERE plan_version = ? AND service = ?",
                (rung, plan_version, service))
            self._record_decision(conn, plan_version, service, "ADVANCE",
                                  f"rung={rung}:pct={pct}", clock_ms)
            self._bump_clock_watermark(conn, clock_ms)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run_failback(self, plan: UnifiedPlan, plan_version: int) -> list:
        order = list(reversed(topo_order(plan)))
        for service in order:
            sp = plan.services[service]
            ladder = np.asarray(sp.traffic_ladder, dtype=np.int64)
            if ladder.size == 0 or not np.all(np.diff(ladder) > 0) \
                    or ladder[-1] != 100:
                raise BizError(Err.DIGEST_MISMATCH,
                               "traffic ladder must be increasing to 100",
                               service=service,
                               ladder=[int(x) for x in ladder])
            conn = self.store.connect()
            try:
                stage, rung = self._stage_of(conn, plan_version, service)
            finally:
                conn.close()
            prior = self._existing_decision_clock(plan_version, service,
                                                  "FAILBACK_START")
            now = prior if prior is not None else self.clock.now_ms()
            if stage != "failed_over":
                self._log_decision(plan_version, service, "SKIP",
                                   "stage_not_failed_over:" + str(stage), now)
                continue
            self._log_decision(plan_version, service, "FAILBACK_START",
                               "rollback_schedule", now)
            for step in range(rung, len(ladder)):
                pct = int(ladder[step])
                healthy = self._get_health()
                conn = self.store.connect()
                try:
                    persisted_stage, _ = self._stage_of(conn, plan_version,
                                                        service)
                finally:
                    conn.close()
                if not healthy or persisted_stage != "failed_over":
                    self._log_decision(
                        plan_version, service, "HOLD",
                        "unhealthy_or_state_drift:rung={}:pct={}".format(
                            step, pct),
                        now)
                    break
                self._advance_rung(plan_version, service, step + 1, pct, now)
            else:
                self._set_stage(plan_version, service, "failed_back", now)
                self._log_decision(plan_version, service, "FAILED_BACK",
                                   "ladder_complete", now)
        return self.decision_chain(plan_version)

    def decision_chain(self, plan_version: int) -> list:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT service, decision, trigger_source, clock_ms"
                " FROM decision_log WHERE plan_version = ? ORDER BY seq",
                (plan_version,)).fetchall()
        finally:
            conn.close()
        chain: dict = {}
        for row in rows:
            entry = chain.setdefault(row["service"], {
                "service": row["service"], "first_trigger": None,
                "decisions": []})
            if entry["first_trigger"] is None:
                entry["first_trigger"] = row["trigger_source"]
            entry["decisions"].append({
                "decision": row["decision"],
                "trigger_source": row["trigger_source"],
                "clock_ms": row["clock_ms"]})
        return [chain[k] for k in sorted(chain)]

    def persisted_view(self, plan_version: int) -> dict:
        conn = self.store.connect()
        try:
            stages = conn.execute(
                "SELECT service, stage, traffic_rung FROM service_stage"
                " WHERE plan_version = ? ORDER BY service",
                (plan_version,)).fetchall()
            marks = conn.execute(
                "SELECT service, action_id, action_version, content_digest,"
                " clock_ms FROM action_watermark WHERE plan_version = ?"
                " ORDER BY service, action_id",
                (plan_version,)).fetchall()
            decisions = conn.execute(
                "SELECT service, decision, trigger_source, clock_ms"
                " FROM decision_log WHERE plan_version = ? ORDER BY seq",
                (plan_version,)).fetchall()
        finally:
            conn.close()
        return {
            "stages": [dict(r) for r in stages],
            "watermarks": [dict(r) for r in marks],
            "decisions": [dict(r) for r in decisions],
        }


def _action_dict(action) -> dict:
    return {"action_id": action.action_id, "op": action.op,
            "generation": action.generation}
