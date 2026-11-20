"""Delayed-queue engine combining storage, schedulers and retry semantics."""

import json
import uuid

from .backoff import next_fire_ms
from .clock import LogicalClock
from .errors import DQError, ErrorCode
from .manifest import payload_digest
from .scheduler import HeapScheduler, WheelScheduler, choose_mode
from .storage import Store

STAGE_SCHEDULED = "SCHEDULED"
STAGE_RUNNING = "RUNNING"
STAGE_SUCCEEDED = "SUCCEEDED"
STAGE_DEAD = "DEAD"

PULL_OPEN = "OPEN"
PULL_COMMITTED = "COMMITTED"


class DelayedQueue:
    def __init__(self, db_path, config=None, crash_hook=None):
        self.config = config
        from .config import QueueConfig
        self.config = config or QueueConfig()
        # crash_hook(point) raising os._exit simulates SIGKILL at a durable
        # boundary; production callers never pass it.
        self.crash_hook = crash_hook
        self.store = Store(db_path, self.config)
        self.clock = LogicalClock(
            self.store.high_water(),
            self.config.rollback_tolerance_ms)
        if self.store.current_mode_version() is None:
            self.store.install_mode("heap", 0, "initial_mode")

    def close(self):
        self.store.close()

    def _maybe_crash(self, point):
        if self.crash_hook is not None:
            self.crash_hook(point)

    # ------------------------------------------------------------------
    def _tick(self, now_ms):
        self.clock.advance(now_ms)
        self.store.save_high_water(self.clock.high_water)

    # ---- enqueue -------------------------------------------------------
    def enqueue_manifest(self, tasks, policies, now_ms):
        """Atomically persist a validated manifest; nothing is half-written."""
        with self.store.process_lock():
            self._tick(now_ms)
            existing = {
                row["task_id"] for row in
                self.store.conn.execute("SELECT task_id FROM tasks")}
            dupes = sorted(existing & {t["task_id"] for t in tasks})
            if dupes:
                raise DQError(ErrorCode.E_TASK_DUPLICATE,
                              "task_id already exists in the database",
                              task_ids=dupes)
            with self.store.transaction():
                for label, policy in policies.items():
                    self.store.conn.execute(
                        "INSERT OR IGNORE INTO policies(label, doc) "
                        "VALUES(?, ?)",
                        (label, json.dumps(policy, sort_keys=True)))
                for ordinal, task in enumerate(tasks):
                    self._maybe_crash("enqueue.mid")
                    self.store.conn.execute(
                        "INSERT INTO tasks(task_id, topic, fire_ms, "
                        "policy_label, max_retries, payload, payload_digest, "
                        "stage, attempts, next_fire_ms, version, updated_ms) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
                        (task["task_id"], task["topic"], task["fire_ms"],
                         task["policy_label"], task["max_retries"],
                         json.dumps(task["payload"], sort_keys=True),
                         task["payload_digest"], STAGE_SCHEDULED, 0,
                         task["fire_ms"], now_ms))
                    self.store.conn.execute(
                        "INSERT INTO manifest_snapshot(task_id, ord, digest, "
                        "fire_ms) VALUES(?,?,?,?)",
                        (task["task_id"], ordinal, task["payload_digest"],
                         task["fire_ms"]))
        return {"enqueued": len(tasks)}

    def enqueue_files(self, manifest_path, policies_path, now_ms):
        from .manifest import load_files
        tasks, policies = load_files(manifest_path, policies_path)
        return self.enqueue_manifest(tasks, policies, now_ms)

    @staticmethod
    def validate_files(manifest_path, policies_path):
        """Pure validation: runs before any database file is created."""
        from .manifest import load_files
        return load_files(manifest_path, policies_path)

    def _policy(self, label):
        row = self.store.conn.execute(
            "SELECT doc FROM policies WHERE label=?", (label,)).fetchone()
        if row is None:
            raise DQError(ErrorCode.E_POLICY_NOT_FOUND,
                          "retry policy label is missing from storage",
                          label=label)
        return json.loads(row["doc"])

    def _task_row(self, task_id):
        row = self.store.conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise DQError(ErrorCode.E_TASK_NOT_FOUND,
                          "task does not exist", task_id=task_id)
        return row

    # ---- lease reaper --------------------------------------------------
    def _reap_expired_leases(self, now_ms):
        """Requeue OPEN pulls whose lease elapsed, purely from persisted state."""
        rows = self.store.conn.execute(
            "SELECT * FROM pulls WHERE status=? AND lease_deadline_ms<=?",
            (PULL_OPEN, now_ms)).fetchall()
        requeued = []
        for pull in rows:
            with self.store.transaction():
                cur = self.store.conn.execute(
                    "SELECT stage FROM tasks WHERE task_id=?",
                    (pull["task_id"],)).fetchone()
                if cur is None or cur["stage"] != STAGE_RUNNING:
                    continue
                self.store.conn.execute(
                    "UPDATE pulls SET status='TIMEOUT' WHERE pull_id=? "
                    "AND status=?", (pull["pull_id"], PULL_OPEN))
                self.store.conn.execute(
                    "UPDATE tasks SET stage=?, updated_ms=? WHERE task_id=? "
                    "AND stage=?",
                    (STAGE_SCHEDULED, now_ms, pull["task_id"],
                     STAGE_RUNNING))
                requeued.append(pull["task_id"])
        return requeued

    # ---- adaptive scan -------------------------------------------------
    def _observe_and_switch(self, now_ms, pending_rows):
        n = len(pending_rows)
        fires = [row["next_fire_ms"] for row in pending_rows]
        span_ms = max(fires) - min(fires) if fires else 0
        current = self.store.current_mode_version()
        mode, reason, costs = choose_mode(
            n, span_ms, self.config.wheel, self.config.adaptive,
            current["mode"])
        if mode != current["mode"]:
            version = self.store.install_mode(mode, now_ms, reason)
            self.store.log_switch({
                "version": version, "from_mode": current["mode"],
                "to_mode": mode, "now_ms": now_ms,
                "before_n": n, "after_n": n, "span_ms": span_ms,
                "costs": costs, "reason": reason,
            })
        current = self.store.current_mode_version()
        return current, {"n": n, "span_ms": span_ms, "costs": costs,
                        "reason": reason}

    def scan(self, now_ms):
        """Advance the logical clock and trigger all due tasks exactly once."""
        with self.store.process_lock():
            self._tick(now_ms)
            requeued = self._reap_expired_leases(now_ms)
            pending = self.store.conn.execute(
                "SELECT task_id, next_fire_ms FROM tasks "
                "WHERE stage=? AND next_fire_ms<=? ORDER BY task_id",
                (STAGE_SCHEDULED, now_ms)).fetchall()
            all_pending = self.store.conn.execute(
                "SELECT task_id, next_fire_ms FROM tasks "
                "WHERE stage=?", (STAGE_SCHEDULED,)).fetchall()
            mode_info, observation = self._observe_and_switch(
                now_ms, all_pending)

            if mode_info["mode"] == "wheel":
                scheduler = WheelScheduler(self.config.wheel,
                                           self.clock.high_water)
            else:
                scheduler = HeapScheduler()
            for row in pending:
                scheduler.add(row["next_fire_ms"], row["task_id"],
                              mode_info["version"])
            due = scheduler.advance(now_ms)

            triggered = []
            for fire_ms, task_id, version in due:
                pull_id = uuid.uuid4().hex
                journal_id = "fire:" + pull_id
                with self.store.transaction():
                    cur = self.store.conn.execute(
                        "SELECT stage, next_fire_ms FROM tasks "
                        "WHERE task_id=?", (task_id,)).fetchone()
                    if cur is None or cur["stage"] != STAGE_SCHEDULED:
                        continue
                    self.store.conn.execute(
                        "INSERT INTO op_journal(journal_id, op_type, status, "
                        "detail, created_ms) VALUES(?, 'FIRE','PENDING',?,?)",
                        (journal_id, json.dumps(
                            {"task_id": task_id, "pull_id": pull_id,
                             "fire_ms": fire_ms}, sort_keys=True), now_ms))
                    self.store.conn.execute(
                        "INSERT INTO pulls(pull_id, task_id, consumer_id, "
                        "mode_version, incarnation, fire_ms, "
                        "lease_deadline_ms, status, created_ms) "
                        "VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                        (pull_id, task_id, version,
                         self._incarnation(task_id), fire_ms,
                         now_ms + self.config.lease_timeout_ms,
                         PULL_OPEN, now_ms))
                    self._maybe_crash("scan.after_pull_insert")
                    updated = self.store.conn.execute(
                        "UPDATE tasks SET stage=?, updated_ms=? "
                        "WHERE task_id=? AND stage=?",
                        (STAGE_RUNNING, now_ms, task_id,
                         STAGE_SCHEDULED)).rowcount
                    if updated != 1:
                        raise DQError(ErrorCode.E_STATE_CORRUPT,
                                      "trigger lost the scheduling race",
                                      task_id=task_id)
                    self._maybe_crash("scan.after_task_update")
                    self.store.conn.execute(
                        "UPDATE op_journal SET status='DONE', done_ms=? "
                        "WHERE journal_id=?", (now_ms, journal_id))
                    triggered.append(task_id)
            self._maybe_crash("scan.after_commit")
            return {
                "now_ms": now_ms, "mode": mode_info["mode"],
                "mode_version": mode_info["version"],
                "observation": observation,
                "requeued_timeouts": requeued,
                "triggered": triggered,
            }

    def _incarnation(self, task_id):
        row = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM pulls WHERE task_id=?",
            (task_id,)).fetchone()
        return row["c"] + 1

    # ---- poll ----------------------------------------------------------
    def poll(self, topic, consumer_id, now_ms, max_items=100):
        """Lease due triggered tasks of one topic for a consumer."""
        with self.store.process_lock():
            self._tick(now_ms)
            self._reap_expired_leases(now_ms)
            rows = self.store.conn.execute(
                "SELECT p.*, t.topic FROM pulls p JOIN tasks t "
                "ON t.task_id=p.task_id WHERE p.status=? AND t.topic=? "
                "AND p.lease_deadline_ms>? ORDER BY p.fire_ms, p.task_id "
                "LIMIT ?",
                (PULL_OPEN, topic, now_ms, max_items)).fetchall()
            leased = []
            for row in rows:
                with self.store.transaction():
                    changed = self.store.conn.execute(
                        "UPDATE pulls SET consumer_id=?, lease_deadline_ms=? "
                        "WHERE pull_id=? AND status=? AND consumer_id IS NULL",
                        (consumer_id,
                         now_ms + self.config.lease_timeout_ms,
                         row["pull_id"], PULL_OPEN)).rowcount
                    if changed != 1:
                        continue
                    task = self.store.conn.execute(
                        "SELECT payload, payload_digest, attempts, "
                        "next_fire_ms FROM tasks WHERE task_id=?",
                        (row["task_id"],)).fetchone()
                    leased.append({
                        "pull_id": row["pull_id"],
                        "task_id": row["task_id"],
                        "topic": row["topic"],
                        "fire_ms": row["fire_ms"],
                        "incarnation": row["incarnation"],
                        "mode_version": row["mode_version"],
                        "lease_deadline_ms":
                            now_ms + self.config.lease_timeout_ms,
                        "attempts": task["attempts"],
                        "payload": json.loads(task["payload"]),
                        "payload_digest": task["payload_digest"],
                    })
            return {"topic": topic, "consumer_id": consumer_id,
                    "now_ms": now_ms, "tasks": leased}

    # ---- ack -----------------------------------------------------------
    def ack(self, pull_id, consumer_id, success, now_ms, error_code=None):
        """Commit a consumer outcome; idempotent on pull_id."""
        with self.store.process_lock():
            self._tick(now_ms)
            pull = self.store.conn.execute(
                "SELECT * FROM pulls WHERE pull_id=?",
                (pull_id,)).fetchone()
            if pull is None:
                raise DQError(ErrorCode.E_PULL_NOT_FOUND,
                              "ack references a pull record that does not "
                              "exist", pull_id=pull_id)
            if pull["status"] == PULL_COMMITTED:
                return {"pull_id": pull_id, "idempotent": True,
                        "outcome": pull["outcome"],
                        "task_id": pull["task_id"]}
            if pull["status"] != PULL_OPEN:
                # TIMEOUT pulls are late acks: the lease was already requeued,
                # so the outcome cannot change persisted state.
                return {"pull_id": pull_id, "idempotent": True,
                        "ignored_late": True, "status": pull["status"],
                        "task_id": pull["task_id"]}
            if pull["consumer_id"] is not None \
                    and pull["consumer_id"] != consumer_id:
                raise DQError(
                    ErrorCode.E_FOREIGN_CONSUMER,
                    "pull is leased to another consumer",
                    pull_id=pull_id, owner=pull["consumer_id"],
                    caller=consumer_id)

            journal_id = "ack:" + pull_id
            detail = {"pull_id": pull_id, "task_id": pull["task_id"],
                      "success": bool(success), "error_code": error_code}
            with self.store.transaction():
                self.store.conn.execute(
                    "INSERT OR IGNORE INTO op_journal(journal_id, op_type, "
                    "status, detail, created_ms) VALUES(?, 'ACK','PENDING',"
                    "?,?)",
                    (journal_id, json.dumps(detail, sort_keys=True), now_ms))
                task = self._task_row(pull["task_id"])
                if success:
                    self._maybe_crash("ack.before_commit")
                    self.store.conn.execute(
                        "UPDATE tasks SET stage=?, last_error_code=?, "
                        "updated_ms=? WHERE task_id=?",
                        (STAGE_SUCCEEDED, error_code, now_ms,
                         pull["task_id"]))
                    self.store.conn.execute(
                        "UPDATE pulls SET status=?, outcome='SUCCESS', "
                        "error_code=?, committed_ms=? WHERE pull_id=? "
                        "AND status=?",
                        (PULL_COMMITTED, error_code, now_ms, pull_id,
                         PULL_OPEN))
                else:
                    attempts = task["attempts"] + 1
                    self._maybe_crash("ack.before_commit")
                    if attempts > task["max_retries"]:
                        self.store.conn.execute(
                            "UPDATE tasks SET stage=?, attempts=?, "
                            "last_error_code=?, dead_reason=?, "
                            "updated_ms=? WHERE task_id=?",
                            (STAGE_DEAD, attempts, error_code,
                             "RETRY_EXHAUSTED", now_ms, pull["task_id"]))
                        self.store.conn.execute(
                            "UPDATE pulls SET status=?, outcome='DEAD', "
                            "error_code=?, committed_ms=? WHERE pull_id=? "
                            "AND status=?",
                            (PULL_COMMITTED, error_code, now_ms, pull_id,
                             PULL_OPEN))
                    else:
                        policy = self._policy(task["policy_label"])
                        next_fire = next_fire_ms(
                            pull["task_id"], policy, attempts, now_ms)
                        self.store.conn.execute(
                            "UPDATE tasks SET stage=?, attempts=?, "
                            "next_fire_ms=?, last_error_code=?, "
                            "updated_ms=? WHERE task_id=?",
                            (STAGE_SCHEDULED, attempts, next_fire,
                             error_code, now_ms, pull["task_id"]))
                        self.store.conn.execute(
                            "UPDATE pulls SET status=?, outcome='RETRY', "
                            "error_code=?, committed_ms=? WHERE pull_id=? "
                            "AND status=?",
                            (PULL_COMMITTED, error_code, now_ms, pull_id,
                             PULL_OPEN))
                self.store.journal_done(journal_id, now_ms)
            return {"pull_id": pull_id, "idempotent": False,
                    "task_id": pull["task_id"],
                    "success": bool(success)}

    # ---- resume / crash recovery ---------------------------------------
    def resume(self, now_ms):
        """Replay only unfinished work recorded in the operation journal."""
        with self.store.process_lock():
            self._tick(now_ms)
            pending = self.store.pending_journals()
            replayed = []
            for item in pending:
                detail = item["detail"]
                with self.store.transaction():
                    if item["op_type"] == "FIRE":
                        pull = self.store.conn.execute(
                            "SELECT * FROM pulls WHERE pull_id=?",
                            (detail["pull_id"],)).fetchone()
                        task = self.store.conn.execute(
                            "SELECT stage FROM tasks WHERE task_id=?",
                            (detail["task_id"],)).fetchone()
                        action = "verified_fire"
                        if pull is not None and task is not None \
                                and task["stage"] == STAGE_RUNNING:
                            pass  # trigger already durable; lease simply runs
                        elif task is not None \
                                and task["stage"] == STAGE_SCHEDULED:
                            # The triggering transaction rolled back before
                            # COMMIT: clean any dangling pull so the next scan
                            # re-triggers the still-SCHEDULED task once.
                            self.store.conn.execute(
                                "DELETE FROM pulls WHERE pull_id=?",
                                (detail["pull_id"],))
                            action = "rolled_back_fire_requeued"
                        self.store.journal_done(item["journal_id"], now_ms)
                        replayed.append({"journal_id": item["journal_id"],
                                         "action": action})
                    elif item["op_type"] == "ACK":
                        pull = self.store.conn.execute(
                            "SELECT status FROM pulls WHERE pull_id=?",
                            (detail["pull_id"],)).fetchone()
                        if pull is not None and pull["status"] == \
                                PULL_COMMITTED:
                            self.store.journal_done(item["journal_id"], now_ms)
                            replayed.append(
                                {"journal_id": item["journal_id"],
                                 "action": "verified_ack"})
                        else:
                            # Ack transaction rolled back: leave OPEN for the
                            # consumer/lease path; close the dangling journal.
                            self.store.journal_done(item["journal_id"], now_ms)
                            replayed.append(
                                {"journal_id": item["journal_id"],
                                 "action": "rollback_ack_reopened"})
            requeued = self._reap_expired_leases(now_ms)
            return {"now_ms": now_ms, "replayed": replayed,
                    "requeued_timeouts": requeued}

    # ---- status --------------------------------------------------------
    def status(self):
        mode = self.store.current_mode_version()
        rows = self.store.conn.execute(
            "SELECT task_id, topic, stage, attempts, next_fire_ms, "
            "fire_ms, policy_label, last_error_code, dead_reason, version "
            "FROM tasks ORDER BY task_id").fetchall()
        return {
            "current_mode": mode["mode"],
            "mode_version": mode["version"],
            "high_water_ms": self.store.high_water(),
            "tasks": [dict(row) for row in rows],
            "switch_log": self.store.switch_log(),
        }

    # ---- verify --------------------------------------------------------
    def verify(self):
        """Cross-check persisted state against the manifest snapshot/digests."""
        problems = []
        snap = self.store.conn.execute(
            "SELECT * FROM manifest_snapshot ORDER BY ord").fetchall()
        for entry in snap:
            task = self.store.conn.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (entry["task_id"],)).fetchone()
            if task is None:
                problems.append({"task_id": entry["task_id"],
                                 "problem": "missing_task_row"})
                continue
            if task["payload_digest"] != entry["digest"]:
                problems.append({"task_id": entry["task_id"],
                                 "problem": "digest_drift"})
            if task["fire_ms"] != entry["fire_ms"]:
                problems.append({"task_id": entry["task_id"],
                                 "problem": "initial_fire_drift"})
            recomputed = payload_digest(json.loads(task["payload"]))
            if recomputed != task["payload_digest"]:
                problems.append({"task_id": entry["task_id"],
                                 "problem": "payload_digest_recheck_failed"})
            if task["stage"] == STAGE_DEAD and not task["dead_reason"]:
                problems.append({"task_id": entry["task_id"],
                                 "problem": "dead_letter_without_reason"})
        extra = self.store.conn.execute(
            "SELECT task_id FROM tasks WHERE task_id NOT IN "
            "(SELECT task_id FROM manifest_snapshot)").fetchall()
        for row in extra:
            problems.append({"task_id": row["task_id"],
                             "problem": "task_without_manifest_entry"})
        if problems:
            raise DQError(ErrorCode.E_VERIFY,
                          "persisted state is inconsistent with manifest",
                          problems=problems)
        return {"ok": True, "checked_tasks": len(snap),
                "high_water_ms": self.store.high_water()}

    # ---- mode management -----------------------------------------------
    def set_mode(self, mode, now_ms, version=None):
        if mode not in ("heap", "wheel"):
            raise DQError(ErrorCode.E_PARSE, "mode must be heap or wheel",
                          got=mode)
        with self.store.process_lock():
            self._tick(now_ms)
            current = self.store.current_mode_version()
            new_version = self.store.install_mode(
                mode, now_ms, "explicit_mode_change",
                explicit_version=version)
            pending = self.store.conn.execute(
                "SELECT task_id, next_fire_ms FROM tasks "
                "WHERE stage=?", (STAGE_SCHEDULED,)).fetchall()
            n = len(pending)
            fires = [r["next_fire_ms"] for r in pending]
            span = max(fires) - min(fires) if fires else 0
            from .scheduler import estimate_cost
            costs = {
                "heap": estimate_cost("heap", n, span, self.config.wheel),
                "wheel": estimate_cost("wheel", n, span,
                                       self.config.wheel)}
            self.store.log_switch({
                "version": new_version, "from_mode": current["mode"],
                "to_mode": mode, "now_ms": now_ms,
                "before_n": n, "after_n": n, "span_ms": span,
                "costs": costs, "reason": "explicit_mode_change"})
            return {"mode": mode, "version": new_version}
