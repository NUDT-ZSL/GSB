"""Session orchestration: manifest -> policy -> plan -> loopback run ->
reconciliation -> diagnostic report."""

import time

from .client import ClientEngine
from .errors import DiagError, E_PROTO, E_WINDOW
from .fairness import diagnose_fairness
from .ledger import reconcile_grants
from .manifest import load_manifest
from .peer import LoopbackPeer
from .planner import Plan, simulate
from .policy import select_strategy


class Session:
    """One diagnostic session bound to a declaration manifest."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.policy_decision = None
        self.plan = None
        self.report = None

    @classmethod
    def from_file(cls, path):
        return cls(load_manifest(path))

    # ------------------------------------------------------------------
    def build_plan(self, forced_strategy=None):
        """Select the strategy (or honour ``forced_strategy``) and build
        the deterministic send plan."""
        decision, plans = select_strategy(self.manifest, forced_strategy)
        self.policy_decision = decision
        self.plan = plans[decision["selected"]]
        return decision, self.plan

    # ------------------------------------------------------------------
    def run(self, forced_strategy=None):
        """Execute the full diagnostic session on the loopback peer."""
        if self.plan is None or forced_strategy is not None:
            self.build_plan(forced_strategy)
        plan = self.plan
        decision = self.policy_decision

        peer = LoopbackPeer(self.manifest)
        peer.start()
        port = peer.wait_ready()

        started = time.monotonic()
        engine = ClientEngine(self.manifest, plan, port)
        run = engine.run()
        peer.join()
        elapsed_ms = int((time.monotonic() - started) * 1000)

        diagnostics = []

        # 1. Protocol state-machine violations (client and peer sides).
        for error in run.proto_errors + peer.result.proto_errors:
            diagnostics.append(error)

        # 2. Plan vs observed send progress (frame-sequence consistency).
        planned_sig = Plan.frame_signature(plan.frames)
        observed_sig = Plan.frame_signature(
            [{"type": f["type"], "stream_id": f["stream_id"],
              "bytes": f["bytes"], "end_stream": f["end_stream"]}
             for f in run.frames])
        progress_consistent = planned_sig == observed_sig
        if not progress_consistent:
            index = next(
                (i for i, (a, b) in enumerate(zip(planned_sig,
                                                  observed_sig)) if a != b),
                min(len(planned_sig), len(observed_sig)))
            diagnostics.append({
                "code": E_WINDOW,
                "frame_type": "DATA",
                "stream_id": planned_sig[index][1]
                if index < len(planned_sig) else None,
                "detail": "send plan diverged from observed progress at "
                          f"frame {index}: planned="
                          f"{planned_sig[index] if index < len(planned_sig) else None} "
                          f"observed="
                          f"{observed_sig[index] if index < len(observed_sig) else None}",
            })

        # 3. Window ledger vs peer-emitted increments, item by item.
        deviation = reconcile_grants(plan.predicted_grants, run.grants)
        if deviation is None:
            deviation = reconcile_grants(plan.predicted_grants,
                                         peer.result.grants)
        if deviation is not None:
            diagnostics.append({
                "code": E_WINDOW,
                "frame_type": "WINDOW_UPDATE",
                "stream_id": deviation["stream_id"],
                "detail": "first ledger deviation at grant "
                          f"{deviation['index']}: stream "
                          f"{deviation['stream_id']} expected increment "
                          f"{deviation['expected_increment']} observed "
                          f"{deviation['observed_increment']} "
                          f"(delta {deviation['deviation_bytes']} bytes)",
                "deviation": deviation,
            })

        fairness = diagnose_fairness(self.manifest, plan, run.actual_sent)

        self.report = {
            "manifest_version": self.manifest["version"],
            "manifest_name": self.manifest["name"],
            "policy": decision,
            "plan": plan.to_dict(),
            "runtime": {
                "elapsed_ms": elapsed_ms,
                "frames_sent": len(run.frames),
                "frames_received_by_peer": peer.result.frames_received,
                "responses_sent_by_peer": peer.result.responses_sent,
                "observed_grants": [list(g) for g in run.grants],
                "stalls": run.stalls,
                "ledger": run.ledger.to_dict(),
            },
            "progress_consistent": progress_consistent,
            "window_reconciliation": "ok" if deviation is None else "failed",
            "fairness": fairness,
            "diagnostics": diagnostics,
        }
        return self.report


def summarize(report):
    """One-line-per-section deterministic diagnostic summary."""
    policy = report["policy"]
    stalls = report["runtime"]["stalls"]
    fairness = report["fairness"]
    lines = [
        f"manifest={report['manifest_name']} "
        f"v{report['manifest_version']}",
        f"policy={policy['selected']} frames={report['runtime']['frames_sent']} "
        f"data_bytes={report['plan']['total_data_bytes']} "
        f"elapsed_ms={report['runtime']['elapsed_ms']}",
        f"stall_segments={len(stalls)} "
        f"stall_ms={sum(s['end_ms'] - s['start_ms'] for s in stalls)}",
        f"fairness_max_deviation={fairness['max_abs_deviation_bytes']} "
        f"starved_streams={fairness['starved_streams']}",
        f"progress_consistent={report['progress_consistent']} "
        f"window_reconciliation={report['window_reconciliation']}",
    ]
    if report["diagnostics"]:
        lines.append("diagnostics:")
        for diag in report["diagnostics"]:
            lines.append(f"  {diag['code']} stream={diag.get('stream_id')} "
                         f"{diag.get('detail') or diag.get('message')}")
    else:
        lines.append("diagnostics: none")
    return "\n".join(lines)
