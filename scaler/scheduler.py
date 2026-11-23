"""Deterministic parallel reconcile scheduler.

The scheduler simulates a wall clock free parallel runtime: services become
ready in dependency topological order, run grouped by failure domain, and are
gated by two real constraints:

* the per round logical time budget (reserved atomically before start);
* the per failure domain concurrency cap.

Actions already started always run to completion.  Three endings are kept
strictly separate: budgeted but never started, failed after start, and
actively abandoned (tombstone or exhausted budget).
"""

from dataclasses import dataclass

from .errors import ScalerError
from .topology import topological_order


@dataclass
class Inflight:
    service: str
    finish_ms: int
    failure_domain: str


class ReconcileScheduler:
    def __init__(self, services, unified, engine, pool, storage, config,
                 round_no, clock_ms, version, all_services=None):
        self.services = {s.name: s for s in services}
        self.all_services = {
            s.name: s for s in (all_services or services)
        }
        self.unified = unified
        self.engine = engine
        self.pool = pool
        self.storage = storage
        self.config = config
        self.round_no = round_no
        self.clock_ms = clock_ms
        self.version = version
        self.action_cost = config["action_cost_ms"]
        self.fd_cap = config["fd_concurrency"]
        self.results = []

    def _ready_order(self):
        # dependencies may live in another failure domain, so derive the
        # global order first and keep only services owned by this scheduler
        global_order = topological_order(list(self.all_services.values()))
        owned = {name for name in self.services}
        return [name for name in global_order if name in owned]

    def run(self):
        ordered = self._ready_order()
        remaining = list(ordered)
        inflight = []
        now_ms = 0
        fd_active = {}
        completed = set()
        budget_dead = False

        def dependencies_done(name):
            # upstream services outside this scheduler are treated as already
            # handled by their own failure domain process
            return all(
                up in completed or up not in self.services
                for up in self.all_services[name].upstream
            )

        while remaining or inflight:
            if budget_dead:
                # started actions must still run to completion before the rest
                # are recorded as abandoned
                self._drain_inflight(inflight, fd_active, completed)
                self._abandon_remaining(remaining, ordered, reason="budget")
                remaining = []
                break

            queued = [name for name in remaining if dependencies_done(name)]
            queued.sort(key=lambda name: (
                0 if self._is_scale_in(name) else 1,
                self.services[name].priority,
                name,
            ))
            started_any = False
            for name in queued:
                fd = self.services[name].failure_domain
                if fd_active.get(fd, 0) >= self.fd_cap:
                    continue
                if self._domain_scale_in_pending(name, queued, fd):
                    continue
                policy = self.unified.get(name)
                if policy is not None and policy.tombstoned:
                    # active abandonment: recorded directly, consumes no budget
                    self._record_tombstone(name, fd)
                    remaining.remove(name)
                    completed.add(name)
                    continue
                status, detail = self.storage.claim_action(
                    self.round_no, name, self.version, fd, self.action_cost
                )
                if status == "duplicate":
                    remaining.remove(name)
                    completed.add(name)
                    continue
                if status in ("budget", "closed"):
                    budget_dead = True
                    break
                fd_active[fd] = fd_active.get(fd, 0) + 1
                inflight.append(Inflight(name, now_ms + self.action_cost, fd))
                remaining.remove(name)
                started_any = True

            if inflight:
                next_finish = min(item.finish_ms for item in inflight)
                now_ms = next_finish
                finishing = [
                    item for item in inflight if item.finish_ms == now_ms
                ]
                for item in finishing:
                    inflight.remove(item)
                    fd_active[item.failure_domain] -= 1
                    self._complete(item.service)
                    completed.add(item.service)
                continue

            if not inflight and remaining:
                # nothing is running and nothing could be started; with the
                # budget still alive this can only be a structural block
                # (unsatisfied dependency inside the owned subgraph)
                blocked = [
                    name for name in remaining
                    if not all(
                        up in completed or up not in self.services
                        for up in self.all_services[name].upstream
                    )
                ]
                reason = "dependency_blocked" if blocked else "fd_concurrency_cap"
                self._abandon_remaining(remaining, ordered, reason=reason)
                remaining = []

        self.storage.close_round(self.round_no, "closed")
        return self.results

    def _drain_inflight(self, inflight, fd_active, completed):
        while inflight:
            next_finish = min(item.finish_ms for item in inflight)
            finishing = [
                item for item in inflight if item.finish_ms == next_finish
            ]
            for item in finishing:
                inflight.remove(item)
                fd_active[item.failure_domain] -= 1
                self._complete(item.service)
                completed.add(item.service)

    def _record_tombstone(self, name, fd):
        self.storage.connection.execute(
            "INSERT INTO actions VALUES(?, ?, ?, ?, 'finished', "
            "'abandoned', ?)",
            (self.round_no, name, self.version, fd,
             '{"reason":"tombstone"}'),
        )
        conclusion, ledger, _ = self.engine.decide(
            name, self.round_no, self.version, self.clock_ms
        )
        conclusion.result = "abandoned"
        conclusion.reason = "policy tombstoned"
        with self.storage.lock:
            self.storage.connection.execute("BEGIN IMMEDIATE")
            try:
                self.storage.save_service_state(
                    name, conclusion.target_replicas,
                    self.version, self.round_no,
                )
                self.storage.save_ledger(
                    self.round_no, name, ledger.snapshot()
                )
                self.storage.save_precision(
                    self.round_no, name,
                    conclusion.sensitive, conclusion.error_chain,
                )
                self.storage.save_conclusion(conclusion)
                self.storage.connection.execute("COMMIT")
            except Exception:
                self.storage.connection.execute("ROLLBACK")
                raise
        self.results.append(
            {
                "service": name,
                "status": "finished",
                "outcome": "abandoned",
                "action": conclusion.action,
                "target": conclusion.target_replicas,
                "sensitive": conclusion.sensitive,
            }
        )

    def _is_scale_in(self, name):
        policy = self.unified.get(name)
        if policy is None or policy.tombstoned:
            return False
        conclusion, _, metrics = self.engine.decide(
            name, self.round_no, self.version, self.clock_ms
        )
        return conclusion.action == "scale_in"

    def _domain_scale_in_pending(self, name, queued, fd):
        if not self._is_scale_in(name):
            return any(
                other != name
                and self.services[other].failure_domain == fd
                and self._is_scale_in(other)
                for other in queued
            )
        return False

    def _abandon_remaining(self, remaining, queued, reason):
        for name in list(remaining):
            fd = self.services[name].failure_domain
            existing = self.storage.connection.execute(
                "SELECT outcome, detail FROM actions WHERE round_no=? AND service=?",
                (self.round_no, name),
            ).fetchone()
            if existing:
                if not any(item["service"] == name for item in self.results):
                    effective_reason = (
                        reason if existing["outcome"] != "budget_exhausted"
                        else "budget"
                    )
                    self.results.append(
                        {
                            "service": name,
                            "status": "not_started",
                            "outcome": existing["outcome"],
                            "reason": effective_reason,
                        }
                    )
                continue
            self.storage.connection.execute(
                "INSERT INTO actions VALUES(?, ?, ?, ?, 'not_started', "
                "'abandoned', ?)",
                (self.round_no, name, self.version, fd,
                 f'{{"reason":"{reason}"}}'),
            )
            self.results.append(
                {"service": name, "status": "not_started",
                 "outcome": "abandoned", "reason": reason}
            )

    def _complete(self, name):
        conclusion, ledger, metrics = self.engine.decide(
            name, self.round_no, self.version, self.clock_ms
        )
        policy = self.unified.get(name)
        if policy is not None and policy.tombstoned:
            conclusion.result = "abandoned"
            conclusion.reason = "policy tombstoned"
            self.storage.finish_action(
                self.round_no, name, "abandoned",
                {"reason": "tombstone"},
            )
        else:
            try:
                self.engine.apply_placement(conclusion, ledger, metrics, self.pool)
                outcome = conclusion.result
                self.storage.finish_action(
                    self.round_no, name, outcome,
                    {"placements": conclusion.placements,
                     "rejected": conclusion.rejected_candidates},
                )
            except ScalerError as exc:
                if exc.code != "E_PLACEMENT_UNSATISFIABLE":
                    raise
                self.storage.finish_action(
                    self.round_no, name, "failed", exc.details
                )
        # durable per service commit: state, occupancy, ledger, precision,
        # conclusion are all written in one atomic transaction
        with self.storage.lock:
            self.storage.connection.execute("BEGIN IMMEDIATE")
            try:
                self.storage.save_service_state(
                    name, conclusion.target_replicas, self.version, self.round_no
                )
                self.storage.replace_nodes(self.pool.snapshot())
                self.storage.save_ledger(
                    self.round_no, name, ledger.snapshot()
                )
                self.storage.save_precision(
                    self.round_no, name,
                    conclusion.sensitive, conclusion.error_chain,
                )
                self.storage.save_conclusion(conclusion)
                self.storage.set_meta(
                    "logical_clock_ms", conclusion.logical_clock
                )
                self.storage.connection.execute("COMMIT")
            except Exception:
                self.storage.connection.execute("ROLLBACK")
                raise
        self.results.append(
            {
                "service": name,
                "status": "finished",
                "outcome": conclusion.result,
                "action": conclusion.action,
                "target": conclusion.target_replicas,
                "sensitive": conclusion.sensitive,
            }
        )
