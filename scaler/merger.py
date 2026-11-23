"""Merge v1 flat policies and v2 sharded policies into one unified view.

Per service the winner is selected by:

1. the declared logical clock (higher wins);
2. on an equal clock, source version priority (v2 > v1);
3. within v2, the higher policy generation.

Every retained gear keeps its source version, raw time representation, decision
basis, conversion scale and rounding direction.  v1 relative seconds are
converted to absolute milliseconds against the injected logical clock.
"""

from decimal import Decimal, localcontext, ROUND_HALF_UP

from .errors import bad_sample
from .models import UnifiedGear, UnifiedPolicy

SECONDS_TO_MS = 1000


def _round_half_up(value):
    with localcontext() as ctx:
        ctx.prec = 50
        return int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))


def convert_v1_relative(relative_seconds, injected_clock_ms):
    """absolute_ms = injected_clock + relative_seconds * 1000 (half up)."""
    scaled = relative_seconds * SECONDS_TO_MS
    absolute = injected_clock_ms + scaled
    rounding = "exact"
    return absolute, rounding, scaled


class PolicyMerger:
    def __init__(self, services, v1_policies, v2_shards, injected_clock_ms):
        self.service_names = {service.name for service in services}
        self.v1 = {policy.service: policy for policy in v1_policies}
        self.v2_shards = v2_shards
        self.injected_clock_ms = injected_clock_ms
        self._collect_v2()

    def _collect_v2(self):
        """Index v2 gears/tombstones and reject duplicate ownership."""
        self.v2_gears = {}
        self.v2_clock = {}
        self.v2_generation = {}
        self.tombstones = {}
        owners = {}
        for shard in self.v2_shards:
            for service in shard.services:
                if service in owners:
                    raise bad_sample(
                        "service declared by multiple v2 shards",
                        service=service,
                        shards=[owners[service], shard.shard_id],
                    )
                owners[service] = shard.shard_id
                self.v2_clock[service] = shard.declared_clock_ms
            for service, generation, gear in shard.gears:
                self.v2_gears.setdefault(service, []).append((generation, gear))
                self.v2_generation[service] = max(
                    generation, self.v2_generation.get(service, -1)
                )
            for tombstone in shard.tombstones:
                service = tombstone.get("service")
                self.tombstones[service] = tombstone

    def _winner(self, service):
        has_v1 = service in self.v1
        has_v2 = service in self.v2_gears or service in self.v2_clock
        tombstoned = service in self.tombstones
        if not has_v1 and not has_v2:
            return None
        if has_v1 and service not in self.service_names:
            raise bad_sample("v1 policy references unknown service", service=service)
        if has_v2 and service not in self.service_names:
            raise bad_sample("v2 policy references unknown service", service=service)

        if not has_v2:
            return ("v1", 0, self.injected_clock_ms,
                    "only v1 policy present")
        if not has_v1:
            return ("v2", self.v2_generation.get(service, 0),
                    self.v2_clock.get(service, 0),
                    "only v2 policy present")

        v1_clock = self.injected_clock_ms
        v2_clock = self.v2_clock[service]
        if v2_clock > v1_clock:
            return ("v2", self.v2_generation[service], v2_clock,
                    f"v2 clock {v2_clock} > v1 clock {v1_clock}")
        if v2_clock < v1_clock:
            return ("v1", 0, v1_clock,
                    f"v1 clock {v1_clock} > v2 clock {v2_clock}")
        return ("v2", self.v2_generation[service], v2_clock,
                f"equal clock {v2_clock}; v2 source priority wins")

    def build_unified_gears(self, service):
        winner = self._winner(service)
        if winner is None:
            return None
        source, generation, clock, basis = winner
        gears = []
        if source == "v1":
            policy = self.v1[service]
            for gear in policy.gears:
                absolute, rounding, scaled = convert_v1_relative(
                    gear.relative_seconds, self.injected_clock_ms
                )
                gears.append(
                    UnifiedGear(
                        service=service,
                        source_version="v1",
                        generation=generation,
                        logical_clock=clock,
                        absolute_ms=absolute,
                        ratio=gear.ratio,
                        raw_time=f"{gear.relative_seconds}s",
                        decision_basis=basis,
                        conversion_scale=SECONDS_TO_MS,
                        rounding=rounding,
                    )
                )
        else:
            for gear_generation, gear in sorted(
                self.v2_gears.get(service, []),
                key=lambda item: (item[1].absolute_ms, item[0]),
            ):
                gears.append(
                    UnifiedGear(
                        service=service,
                        source_version="v2",
                        generation=gear_generation,
                        logical_clock=clock,
                        absolute_ms=gear.absolute_ms,
                        ratio=gear.ratio,
                        raw_time=f"{gear.absolute_ms}ms",
                        decision_basis=basis,
                        conversion_scale=1,
                        rounding="exact",
                    )
                )
        return UnifiedPolicy(
            service=service,
            source_version=source,
            generation=generation,
            logical_clock=clock,
            decision_basis=basis,
            tombstoned=service in self.tombstones,
            gears=tuple(gears),
        )

    def merge(self):
        unified = {}
        for service in sorted(self.service_names):
            policy = self.build_unified_gears(service)
            if policy is not None:
                unified[service] = policy
        return unified
