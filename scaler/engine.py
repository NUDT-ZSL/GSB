"""Per service scaling decision engine with the fixed point ledger."""

import hashlib
import json
from decimal import Decimal, ROUND_CEILING

from .fixedpoint import FixedLedger, ROUND_UP
from .models import ServiceConclusion
from .errors import ScalerError


def load_at(load_trace, service, clock_ms):
    """Latest load sample at or before ``clock_ms`` (earliest sample fallback)."""
    candidates = [
        (at_ms, Decimal(value))
        for (svc, at_ms), value in load_trace.items()
        if svc == service and at_ms <= clock_ms
    ]
    if not candidates:
        all_for_service = sorted(
            (at_ms for (svc, at_ms) in load_trace if svc == service)
        )
        if not all_for_service:
            return Decimal("0")
        at_ms = all_for_service[0]
        return Decimal(load_trace[(service, at_ms)])
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def choose_gear(unified_policy, clock_ms):
    """Highest ratio whose absolute_ms is already effective; else lowest gear."""
    effective = [g for g in unified_policy.gears if g.absolute_ms <= clock_ms]
    if not effective:
        return min(unified_policy.gears, key=lambda g: (Decimal(g.ratio), g.absolute_ms))
    return max(effective, key=lambda g: (Decimal(g.ratio), g.absolute_ms))


def _ceil(dec):
    return int(dec.to_integral_value(rounding=ROUND_CEILING))


class DecisionEngine:
    def __init__(self, services, unified, load_trace, config, cost_budget_q):
        self.services = {s.name: s for s in services}
        self.unified = unified
        self.load_trace = load_trace
        self.config = config
        self.cost_budget_q = cost_budget_q
        self.cost_per = Decimal(config["cost_per_replica"])

    def fingerprint(self, service_name, clock_ms):
        service = self.services[service_name]
        policy = self.unified.get(service_name)
        load = load_at(self.load_trace, service_name, clock_ms)
        basis = {
            "service": service.to_dict(),
            "policy": policy.to_dict() if policy else None,
            "load": str(load),
            "clock_ms": clock_ms,
            "config": {
                "fixed_scale": self.config["fixed_scale"],
                "tolerance_ticks": self.config["tolerance_ticks"],
                "cost_per_replica": self.config["cost_per_replica"],
            },
        }
        blob = json.dumps(basis, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def decide(self, service_name, round_no, version, clock_ms, existing_ledger=None):
        service = self.services[service_name]
        ledger = existing_ledger or FixedLedger(
            self.config["fixed_scale"], self.config["tolerance_ticks"]
        )
        policy = self.unified.get(service_name)
        load = load_at(self.load_trace, service_name, clock_ms)

        base_q = ledger.exact_int(service_name, service.replicas, "request.base_replicas")
        if policy is None or policy.tombstoned or service.replicas == 0:
            target = service.replicas
            gear_ratio = Decimal("1")
        else:
            gear = choose_gear(policy, clock_ms)
            gear_ratio = Decimal(gear.ratio)
            raw_target = Decimal(service.replicas) * gear_ratio
            target = max(1, _ceil(raw_target)) if service.replicas > 0 else 0

        target_q = ledger.convert(service_name, target, "request.target_replicas")
        delta_q = ledger.sub(
            service_name, target_q, base_q, "request.replica_delta"
        )
        cpu_q = ledger.exact_int(service_name, service.cpu_per_replica, "unit.cpu")
        mem_q = ledger.exact_int(service_name, service.mem_per_replica, "unit.mem")
        effective_cpu = ledger.mul(
            service_name, target_q, cpu_q, "effective.cpu", rounding=ROUND_UP
        )
        effective_mem = ledger.mul(
            service_name, target_q, mem_q, "effective.mem", rounding=ROUND_UP
        )
        cost_per_q = ledger.convert(
            service_name, self.cost_per, "cost.per_replica"
        )
        cost_delta = ledger.mul(
            service_name, delta_q, cost_per_q, "cost.delta", rounding=ROUND_UP
        )
        headroom = ledger.sub(
            service_name, self.cost_budget_q, cost_delta, "budget.headroom"
        )

        # precision sensitivity: target difference vs load scaled demand, and
        # effective cpu vs effective mem (both feed placement decisions).
        demand_q = ledger.convert(
            service_name, gear_ratio, "load.gear_demand"
        )
        sensitive = ledger.is_sensitive(target_q, demand_q) or \
            ledger.is_sensitive(effective_cpu, effective_mem)
        chain = []
        if sensitive:
            seq = ledger.chain_for(target_q, demand_q, effective_cpu, effective_mem)
            chain = [
                {
                    "service": service_name,
                    "ledger_seq": entry.seq,
                    "path": entry.path,
                    "op": entry.op,
                    "round_dir": entry.round_dir,
                    "err_bound": entry.err_bound,
                }
                for entry in ledger.entries
                if entry.seq in seq
            ]

        action = "scale_out" if target > service.replicas else (
            "scale_in" if target < service.replicas else "noop"
        )
        conclusion = ServiceConclusion(
            service=service_name,
            round_no=round_no,
            policy_version=version,
            logical_clock=clock_ms,
            current_replicas=service.replicas,
            target_replicas=target,
            action=action,
            failure_domain=service.failure_domain,
            priority=service.priority,
            sensitive=sensitive,
            error_chain=chain,
            input_fingerprint=self.fingerprint(service_name, clock_ms),
            ledger_snapshot=ledger.snapshot(),
        )
        return conclusion, ledger, {
            "target": target,
            "delta": target - service.replicas,
            "effective_cpu": effective_cpu.ticks,
            "effective_mem": effective_mem.ticks,
            "cost_delta": cost_delta.ticks,
            "headroom": headroom.ticks,
        }

    def apply_placement(self, conclusion, ledger, metrics, pool):
        """Execute the resource side of a decided action against the pool."""
        service = self.services[conclusion.service]
        if conclusion.action == "scale_in":
            released = pool.release(
                conclusion.service,
                {"cpu_per_replica": service.cpu_per_replica,
                 "mem_per_replica": service.mem_per_replica},
            )
            conclusion.placements = released
            conclusion.result = "scaled_in"
            conclusion.reason = "resources released before same domain expansion"
            return conclusion
        if conclusion.action == "noop":
            conclusion.result = "noop"
            conclusion.reason = "target equals current replicas"
            return conclusion

        try:
            placements = pool.place(
                conclusion.service,
                metrics["delta"],
                service.cpu_per_replica,
                service.mem_per_replica,
                service.failure_domain,
            )
        except ScalerError as exc:
            if exc.code != "E_PLACEMENT_UNSATISFIABLE":
                raise
            conclusion.result = "failed"
            conclusion.reason = exc.message
            conclusion.rejected_candidates = exc.details.get(
                "rejected_candidates", []
            )
            conclusion.first_constraint = exc.details.get(
                "first_constraint", ""
            )
            raise
        conclusion.placements = placements
        conclusion.result = "scaled_out"
        conclusion.reason = "all replicas placed under cpu and mem budgets"
        return conclusion
