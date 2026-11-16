"""Distribution session orchestration: prepare, distribute, converge,
commit, and snapshot-based recovery."""

from .converge import check_convergence
from .errors import E_CONVERGE, E_NODE
from .manifest import load_manifest
from .planner import compile_plan
from .registry import load_registry
from .scheduler import Cluster
from .statemachine import DISTRIBUTING, FAILED, PREPARED, StateMachine


class SimulatedInterrupt(Exception):
    """Raised to simulate a platform crash mid-session (demo/testing)."""


class Session:
    def __init__(self, store, registry_path):
        self.store = store
        self.registry_path = registry_path
        self.registry_ids = load_registry(registry_path)
        self.sm = StateMachine(store)
        self.manifest = None
        self.plan = None

    def prepare(self, manifest_path):
        """Load + validate the manifest, compile the plan, persist the
        snapshot. If the snapshot version differs from the incoming
        manifest version, the whole snapshot is replaced (never merged)."""
        manifest = load_manifest(
            manifest_path, self.registry_ids,
            min_version=self.store.committed_version(),
        )
        plan = compile_plan(manifest)
        replaced = False
        if self.store.snapshot_version() not in (0, manifest.version):
            self.store.replace_snapshot(manifest.to_dict(), plan)
            self.sm = StateMachine(self.store)
            replaced = True
        else:
            self.store.save_manifest(manifest.to_dict())
            self.store.save_plan(plan)
            self.store.set_meta("snapshot_version", str(manifest.version))
        if self.sm.state == "init":
            self.sm.transition(PREPARED, manifest.version, [])
        self.manifest = manifest
        self.plan = plan
        return {"ok": True, "version": manifest.version,
                "snapshot_replaced": replaced, "plan": plan}

    def distribute(self, fail_modes=None, concurrency=4, timeout=2.0,
                   max_retries=2, interrupt_after_dispatch=False):
        """Run the parallel dispatch, converge receipts and commit/fail."""
        if self.plan is None:
            snap_version = self.store.snapshot_version()
            self.plan = self.store.get_plan(snap_version)
            if self.plan is None:
                return {"ok": False, "code": "E_STATE",
                        "reason": "no prepared snapshot to distribute"}
        version = self.plan["version"]
        if self.sm.state == PREPARED:
            self.sm.transition(DISTRIBUTING, version, [])

        done_nodes = {r["node"] for r in self.store.receipts(version)
                      if r["kind"] in ("ack", "retry")}
        cluster = Cluster(self.store, list(self.plan["assignments"]),
                          fail_modes=fail_modes, concurrency=concurrency,
                          timeout=timeout, max_retries=max_retries)
        cluster.start()
        try:
            _, node_errors = cluster.dispatch_all(self.plan,
                                                  skip_nodes=done_nodes)
        finally:
            cluster.shutdown()

        if interrupt_after_dispatch:
            raise SimulatedInterrupt(
                "platform interrupted mid-session; snapshot persisted")

        return self._finish(version, node_errors)

    def _finish(self, version, node_errors):
        convergence = check_convergence(self.plan, self.store.receipts(version))
        blockers = []
        if node_errors:
            blockers.append("node errors: " + ", ".join(
                "%s(%s)" % (n, e["code"]) for n, e in sorted(node_errors.items())))
        if convergence["missing"]:
            blockers.append("missing receipts: "
                            + ", ".join(convergence["missing"]))
        if convergence["mismatched"]:
            blockers.append("digest mismatch: "
                            + ", ".join(convergence["mismatched"]))

        commit = self.sm.commit(version, convergence["acked"],
                                convergence["ok"] and not node_errors, blockers)
        if not commit["ok"]:
            if self.sm.state == DISTRIBUTING:
                self.sm.transition(FAILED, version, convergence["acked"],
                                   reason="; ".join(blockers))
            if convergence["missing"] or convergence["mismatched"]:
                code = E_CONVERGE
            else:
                code = E_NODE
            return {"ok": False, "code": code, "version": version,
                    "commit": commit, "convergence": convergence,
                    "node_errors": node_errors, "blockers": blockers}
        return {"ok": True, "version": version, "commit": commit,
                "convergence": convergence, "node_errors": node_errors}

    def recover(self):
        """Rebuild a consistent view from the persisted snapshot."""
        snap_version = self.store.snapshot_version()
        manifest = self.store.get_manifest(snap_version)
        plan = self.store.get_plan(snap_version)
        transitions = self.store.transitions()
        state = transitions[-1]["to"] if transitions else "init"
        receipts = self.store.receipts(snap_version)
        convergence = None
        if plan is not None:
            convergence = check_convergence(plan, receipts)
        return {
            "snapshot_version": snap_version,
            "committed_version": self.store.committed_version(),
            "state": state,
            "manifest_present": manifest is not None,
            "plan_present": plan is not None,
            "receipts": len(receipts),
            "node_status": self.store.node_statuses(),
            "convergence": convergence,
            "transitions": transitions,
        }

    def resume(self, fail_modes=None, concurrency=4, timeout=2.0,
               max_retries=2):
        """Resume an interrupted distributing session from the snapshot.
        Nodes that already acknowledged are not re-dispatched; degraded
        nodes are skipped and reported."""
        view = self.recover()
        if view["state"] != DISTRIBUTING:
            return {"ok": False, "code": "E_STATE",
                    "reason": "cannot resume from state %s" % view["state"],
                    "view": view}
        self.plan = self.store.get_plan(view["snapshot_version"])
        degraded = {n for n, s in view["node_status"].items() if s == "degraded"}
        version = self.plan["version"]
        done_nodes = {r["node"] for r in self.store.receipts(version)
                      if r["kind"] in ("ack", "retry")}
        skip = done_nodes | degraded
        spawn_nodes = [n for n in self.plan["assignments"] if n not in degraded]
        cluster = Cluster(self.store, spawn_nodes,
                          fail_modes=fail_modes, concurrency=concurrency,
                          timeout=timeout, max_retries=max_retries)
        cluster.start()
        try:
            _, node_errors = cluster.dispatch_all(self.plan, skip_nodes=skip)
        finally:
            cluster.shutdown()
        for node in degraded:
            node_errors.setdefault(
                node, {"code": E_NODE, "reason": "node degraded before resume"})
        return self._finish(version, node_errors)
