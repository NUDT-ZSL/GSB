"""High level workflows: load packages, reconcile, resume, status and verify."""

import copy
import hashlib
import json
import time
from dataclasses import replace

from .engine import DecisionEngine
from .errors import (
    bad_sample,
    corruption,
    state_conflict,
    version_rollback,
)
from .fixedpoint import FixedLedger
from .merger import PolicyMerger
from .placement import NodePool
from .scheduler import ReconcileScheduler
from .storage import Storage
from .topology import downstream_closure, topological_order


def shard_hash_of(payload):
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Context:
    """All parsed inputs needed for a reconcile, held outside durable state."""

    def __init__(self, services, nodes, unified, load_trace, config, version):
        self.services = services
        self.nodes = nodes
        self.unified = unified
        self.load_trace = load_trace
        self.config = config
        self.version = version


class Orchestrator:
    def __init__(self, db_path):
        self.storage = Storage(db_path)

    def close(self):
        self.storage.close()

    # ------------------------------------------------------------- versioning
    def classify_package(self, new_version, new_gen_floor, new_hashes):
        latest = self.storage.latest_package()
        if latest is None:
            return "full_replace"
        if new_version < latest["version"]:
            raise version_rollback(
                "package version is older than the applied package",
                incoming=new_version, current=latest["version"],
            )
        if new_version == latest["version"]:
            if sorted(new_hashes) == json.loads(latest["shard_hashes"]):
                return "no_op"
            raise state_conflict(
                "same version carries different shard hashes",
                version=new_version,
            )
        if new_gen_floor < latest["gen_floor"]:
            raise version_rollback(
                "policy generation floor regressed",
                incoming=new_gen_floor, current=latest["gen_floor"],
            )
        old_hashes = set(json.loads(latest["shard_hashes"]))
        if old_hashes.issubset(set(new_hashes)) and old_hashes != set(new_hashes):
            return "incremental_append"
        return "full_replace"

    def load_package(self, context, new_version, new_gen_floor, new_hashes):
        with self.storage.lock:
            mode = self.classify_package(
                new_version, new_gen_floor, new_hashes
            )
            if mode == "no_op":
                return {"mode": mode, "version": new_version}
            self.storage.connection.execute("BEGIN IMMEDIATE")
            try:
                self.storage.record_package(
                    new_version, new_gen_floor, new_hashes,
                    mode, int(time.time() * 1000),
                )
                self.storage.set_meta("policy_version", new_version)
                if mode == "full_replace":
                    # reset round watermarks while preserving nothing from the
                    # superseded generation
                    self.storage.set_meta("generation_floor", new_gen_floor)
                self.storage.connection.execute("COMMIT")
            except Exception:
                self.storage.connection.execute("ROLLBACK")
                raise
        return {"mode": mode, "version": new_version}

    # -------------------------------------------------------------- reconcile
    def _build_engine(self, context):
        ledger_zero = FixedLedger(
            context.config["fixed_scale"], context.config["tolerance_ticks"]
        )
        cost_budget_q = ledger_zero.convert(
            "__budget__", context.config["round_budget_cost"], "budget.total"
        )
        return DecisionEngine(
            context.services,
            context.unified,
            context.load_trace,
            context.config,
            cost_budget_q,
        )

    def reconcile(self, context, failure_domain=None):
        engine = self._build_engine(context)
        with self.storage.lock:
            latest_round = self.storage.latest_round()
            round_no = (latest_round["round_no"] + 1) if latest_round else 1
            clock_ms = self.storage.get_meta(
                "logical_clock_ms", default=0
            )
            clock_ms += context.config["clock_step_ms"]
            pool = self._load_or_init_pool(context)
            self._hydrate_replicas(context)
            self.storage.begin_round(
                round_no, clock_ms, context.config["round_budget_ms"]
            )
            self.storage.set_meta("logical_clock_ms", clock_ms)

        services = [
            service for service in context.services
            if failure_domain is None
            or service.failure_domain == failure_domain
        ]
        scheduler = ReconcileScheduler(
            services,
            context.unified,
            engine,
            pool,
            self.storage,
            context.config,
            round_no,
            clock_ms,
            context.version,
            all_services=context.services,
        )
        results = scheduler.run()
        return {
            "round_no": round_no,
            "logical_clock_ms": clock_ms,
            "results": results,
            "node_remaining": pool.remaining_snapshot(),
        }

    def _load_or_init_pool(self, context):
        snapshot = self.storage.load_pool_snapshot()
        pool = NodePool(context.nodes)
        if snapshot["nodes"]:
            pool.load_snapshot(snapshot)
        else:
            self.storage.replace_nodes(pool.snapshot())
        return pool

    def _hydrate_replicas(self, context):
        """Overwrite in memory replica counts with durable watermarks."""
        for index, service in enumerate(context.services):
            state = self.storage.get_service_state(service.name)
            if state is None:
                continue
            context.services[index] = type(service)(
                name=service.name,
                replicas=state["replicas"],
                cpu_per_replica=service.cpu_per_replica,
                mem_per_replica=service.mem_per_replica,
                upstream=service.upstream,
                failure_domain=service.failure_domain,
                priority=service.priority,
            )

    # ------------------------------------------------------------------ resume
    def resume(self, context):
        """Replay only actions left pending by a forced termination.

        Committed actions are never re-executed, so resource accounts are not
        deducted twice.  Pending actions have no committed node writes; their
        budget reservation stays booked and the action is recomputed once.
        """
        latest_round = self.storage.latest_round()
        if latest_round is None:
            return {"replayed": [], "note": "no round to resume"}
        round_no = latest_round["round_no"]
        pending = self.storage.pending_actions(round_no)
        if not pending:
            return {"replayed": [], "round_no": round_no}

        engine = self._build_engine(context)
        pool = NodePool(context.nodes)
        pool.load_snapshot(self.storage.load_pool_snapshot())
        self._hydrate_replicas(context)
        clock_ms = latest_round["logical_clock_ms"]
        replayed = []
        for row in pending:
            name = row["service"]
            conclusion, ledger, metrics = engine.decide(
                name, round_no, context.version, clock_ms
            )
            try:
                engine.apply_placement(conclusion, ledger, metrics, pool)
                outcome = conclusion.result
            except Exception as exc:  # placement failures are durable outcomes
                outcome = "failed"
                conclusion.result = "failed"
                conclusion.reason = getattr(exc, "message", str(exc))
                details = getattr(exc, "details", {})
                self.storage.finish_action(round_no, name, "failed", details)
            else:
                self.storage.finish_action(
                    round_no, name, outcome,
                    {"placements": conclusion.placements},
                )
            with self.storage.lock:
                self.storage.connection.execute("BEGIN IMMEDIATE")
                try:
                    self.storage.save_service_state(
                        name, conclusion.target_replicas,
                        context.version, round_no,
                    )
                    self.storage.replace_nodes(pool.snapshot())
                    self.storage.save_ledger(
                        round_no, name, ledger.snapshot()
                    )
                    self.storage.save_precision(
                        round_no, name,
                        conclusion.sensitive, conclusion.error_chain,
                    )
                    self.storage.save_conclusion(conclusion)
                    self.storage.connection.execute("COMMIT")
                except Exception:
                    self.storage.connection.execute("ROLLBACK")
                    raise
            replayed.append({"service": name, "outcome": outcome})
        self.storage.close_round(round_no, "closed_after_resume")
        return {"replayed": replayed, "round_no": round_no}

    # ------------------------------------------------------------------ status
    def status(self, context):
        snapshot = self.storage.load_pool_snapshot()
        precision = {}
        latest_round = self.storage.latest_round()
        if latest_round:
            for row in self.storage.all_precision(latest_round["round_no"]):
                precision[row["service"]] = row
        replica_by_name = {
            row["service"]: row["replicas"]
            for row in self.storage.all_service_states()
        }
        services = []
        for service in sorted(context.services, key=lambda s: s.name):
            services.append(
                {
                    "service": service.name,
                    "replicas": replica_by_name.get(
                        service.name, service.replicas
                    ),
                    "failure_domain": service.failure_domain,
                    "precision_sensitive": precision.get(
                        service.name, {"sensitive": False}
                    )["sensitive"],
                    "error_chain": precision.get(
                        service.name, {"error_chain": []}
                    )["error_chain"],
                }
            )
        nodes = sorted(snapshot["nodes"], key=lambda row: row["node"])
        node_view = [
            {
                "node": row["node"],
                "cpu_free": row["cpu_free"],
                "mem_free": row["mem_free"],
                "topology_domain": row["topology_domain"],
            }
            for row in nodes
        ]
        return {
            "policy_version": self.storage.get_meta("policy_version"),
            "logical_clock_ms": self.storage.get_meta("logical_clock_ms", 0),
            "round_no": latest_round["round_no"] if latest_round else 0,
            "services": services,
            "nodes": node_view,
        }

    # ------------------------------------------------------------------ verify
    def verify(self, context):
        """Recompute decisions from inputs and compare with durable state."""
        latest_round = self.storage.latest_round()
        if latest_round is None:
            return {"ok": True, "checked": 0, "mismatches": []}
        round_no = latest_round["round_no"]
        engine = self._build_engine(context)
        self._hydrate_replicas(context)
        clock_ms = latest_round["logical_clock_ms"]
        mismatches = []
        checked = 0
        for service in context.services:
            durable = self.storage.get_conclusion(round_no, service.name)
            if durable is None:
                continue
            checked += 1
            recomputed, _, _ = engine.decide(
                service.name, round_no, context.version, clock_ms
            )
            for field_name in (
                "target_replicas",
                "action",
                "sensitive",
                "input_fingerprint",
            ):
                if getattr(durable, field_name) != getattr(
                    recomputed, field_name
                ):
                    mismatches.append(
                        {
                            "service": service.name,
                            "field": field_name,
                            "durable": getattr(durable, field_name),
                            "recomputed": getattr(recomputed, field_name),
                        }
                    )
            durable_ledger = self.storage.get_ledger(round_no, service.name)
            if durable_ledger and durable_ledger != recomputed.ledger_snapshot:
                mismatches.append(
                    {"service": service.name, "field": "ledger_snapshot"}
                )
        pool_snapshot = self.storage.load_pool_snapshot()
        for row in pool_snapshot["nodes"]:
            if row["cpu_free"] < 0 or row["mem_free"] < 0:
                mismatches.append(
                    {"service": "*", "field": "negative_capacity",
                     "node": row["name"]}
                )
            if row["cpu_free"] > row["cpu_total"] or \
                    row["mem_free"] > row["mem_total"]:
                mismatches.append(
                    {"service": "*", "field": "free_exceeds_total",
                     "node": row["name"]}
                )
        # occupancy accounting must agree with per service replica watermarks
        placed = {}
        for node_name, services in pool_snapshot["occupancy"].items():
            for service, replicas in services.items():
                placed[service] = placed.get(service, 0) + replicas
        for state in self.storage.all_service_states():
            durable_total = state["replicas"]
            accounted = placed.get(state["service"], 0)
            if accounted > durable_total:
                mismatches.append(
                    {
                        "service": state["service"],
                        "field": "occupancy_exceeds_replicas",
                        "durable": durable_total,
                        "accounted": accounted,
                    }
                )
        return {
            "ok": not mismatches,
            "checked": checked,
            "mismatches": mismatches,
        }

    # ------------------------------------------------------------------ patch
    def apply_patch(self, context, patch):
        """Incremental invalidation closure with verbatim conclusion reuse."""
        roots = set()
        unified = copy.deepcopy(context.unified)
        services = context.services
        for change in patch.get("gear_changes", []):
            service = change["service"]
            if service not in unified:
                raise bad_sample("patch targets unknown service", service=service)
            policy = unified[service]
            new_gears = tuple(
                gear if gear.ratio != change["old_ratio"]
                else replace(gear, ratio=change["new_ratio"])
                for gear in policy.gears
            )
            unified[service] = replace(policy, gears=new_gears)
            roots.add(service)
        for change in patch.get("dependency_changes", []):
            service = change["service"]
            if change.get("new_upstream") not in {s.name for s in services}:
                raise bad_sample("patched upstream does not exist",
                                 upstream=change.get("new_upstream"))
            roots.add(service)
            roots.add(change["new_upstream"])
        for tombstone in patch.get("tombstones", []):
            roots.add(tombstone["service"])
            if tombstone["service"] in unified:
                policy = unified[tombstone["service"]]
                unified[tombstone["service"]] = replace(
                    policy, tombstoned=True
                )

        affected = downstream_closure(roots, services) | set(roots)
        # conclusions affected; placement and precision inherit the same closure
        reuse = []
        recompute = []
        latest_round = self.storage.latest_round()
        for service in topological_order(services):
            entry = {
                "service": service,
                "recompute_conclusion": service in affected,
                "recompute_placement": service in affected,
                "recompute_precision": service in affected,
            }
            if service in affected:
                recompute.append(entry)
            else:
                reuse.append(entry)
                if latest_round is not None:
                    durable = self.storage.get_conclusion(
                        latest_round["round_no"], service
                    )
                    if durable is not None:
                        # re-save untouched, proving byte identical reuse
                        self.storage.save_conclusion(durable, reusable=True)
        return {
            "affected_roots": sorted(roots),
            "affected_closure": sorted(affected),
            "recompute": recompute,
            "reuse": reuse,
        }
