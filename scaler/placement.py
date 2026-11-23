"""Replica placement under CPU and memory double budgets.

Placement is all or nothing: if no full set of replicas can be placed the
function rejects the whole expansion and records every rejected candidate node
together with the first constraint that disqualified it.  Rejected attempts
never mutate node capacity, so a failure leaves the pool exactly as it was.
"""

from dataclasses import dataclass

import numpy as np

from .errors import placement_unsatisfiable


@dataclass
class Placement:
    node: str
    replicas: int
    cpu_used: int
    mem_used: int

    def to_dict(self):
        return {
            "node": self.node,
            "replicas": self.replicas,
            "cpu_used": self.cpu_used,
            "mem_used": self.mem_used,
        }


class NodePool:
    """Mutable, persistable view of node capacity and per service occupancy."""

    def __init__(self, nodes):
        self.nodes = {node.name: node for node in nodes}
        self.order = [node.name for node in nodes]
        self.occupancy = {name: {} for name in self.order}

    # ------------------------------------------------------------ inspection
    def remaining_snapshot(self):
        return [
            {
                "node": name,
                "cpu_free": self.nodes[name].cpu_free,
                "mem_free": self.nodes[name].mem_free,
                "topology_domain": self.nodes[name].topology_domain,
            }
            for name in self.order
        ]

    def deficit_vector(self, cpu_need, mem_need):
        """numpy derived aggregate shortfall across the whole pool."""
        cpu_free = np.array([self.nodes[n].cpu_free for n in self.order], dtype=np.int64)
        mem_free = np.array([self.nodes[n].mem_free for n in self.order], dtype=np.int64)
        cpu_deficit = max(0, int(cpu_need - int(cpu_free.sum())))
        mem_deficit = max(0, int(mem_need - int(mem_free.sum())))
        return {"cpu_deficit": cpu_deficit, "mem_deficit": mem_deficit}

    # ------------------------------------------------------------- mutations
    def release(self, service, deltas):
        """Return resources held by ``service`` after a scale in."""
        released = []
        for name in self.order:
            count = self.occupancy[name].pop(service, 0)
            if not count:
                continue
            node = self.nodes[name]
            cpu_back = deltas["cpu_per_replica"] * count
            mem_back = deltas["mem_per_replica"] * count
            self.nodes[name] = type(node)(
                name=node.name,
                cpu_total=node.cpu_total,
                mem_total=node.mem_total,
                cpu_free=node.cpu_free + cpu_back,
                mem_free=node.mem_free + mem_back,
                topology_domain=node.topology_domain,
            )
            released.append(Placement(name, -count, -cpu_back, -mem_back).to_dict())
        return released

    def place(self, service, replicas, cpu_per, mem_per, topology_domain):
        """Greedy first fit; the entire request fails atomically on shortfall."""
        if replicas <= 0:
            return []
        candidates = [
            name for name in self.order
            if self.nodes[name].topology_domain == topology_domain
        ]
        rejected = []
        tentative = {name: self.nodes[name] for name in self.order}
        placements = []
        remaining = replicas
        for name in candidates:
            if remaining == 0:
                break
            node = tentative[name]
            fit_cpu = node.cpu_free // cpu_per
            fit_mem = node.mem_free // mem_per
            fit_here = int(min(remaining, fit_cpu, fit_mem))
            if fit_here == 0:
                if node.cpu_free < cpu_per:
                    constraint = "cpu"
                elif node.mem_free < mem_per:
                    constraint = "mem"
                else:  # pragma: no cover - fit_here cannot otherwise be zero
                    constraint = "topology"
                rejected.append(
                    {
                        "node": name,
                        "cpu_free": node.cpu_free,
                        "mem_free": node.mem_free,
                        "first_constraint": constraint,
                    }
                )
                continue
            cpu_used = fit_here * cpu_per
            mem_used = fit_here * mem_per
            placements.append(Placement(name, fit_here, cpu_used, mem_used))
            tentative[name] = type(node)(
                name=node.name,
                cpu_total=node.cpu_total,
                mem_total=node.mem_total,
                cpu_free=node.cpu_free - cpu_used,
                mem_free=node.mem_free - mem_used,
                topology_domain=node.topology_domain,
            )
            remaining -= fit_here

        if remaining > 0:
            if not candidates:
                first_constraint = "topology"
            elif rejected:
                first_constraint = rejected[0]["first_constraint"]
            else:
                first_constraint = "cpu"
            raise placement_unsatisfiable(
                "not enough capacity for all requested replicas",
                service=service,
                replicas_requested=replicas,
                replicas_unplaced=remaining,
                first_constraint=first_constraint,
                rejected_candidates=rejected,
                pool_deficit=self.deficit_vector(
                    replicas * cpu_per, replicas * mem_per
                ),
            )

        # commit only after the full request fits
        for placement in placements:
            name = placement.node
            node = tentative[name]
            self.nodes[name] = node
            self.occupancy[name][service] = (
                self.occupancy[name].get(service, 0) + placement.replicas
            )
        return [placement.to_dict() for placement in placements]

    # ----------------------------------------------------------- persistence
    def snapshot(self):
        return {
            "nodes": [self.nodes[name].to_dict() for name in self.order],
            "occupancy": {
                name: dict(services) for name, services in self.occupancy.items()
            },
        }

    def load_snapshot(self, snapshot):
        from .models import Node

        normalized = []
        for row in snapshot["nodes"]:
            if "name" not in row:
                row = dict(
                    row,
                    name=row.get("node", row.get("name")),
                )
                row.pop("node", None)
            normalized.append(row)
        self.nodes = {
            row["name"]: Node(**row) for row in normalized
        }
        self.order = [row["name"] for row in normalized]
        self.occupancy = {name: {} for name in self.order}
        for name, services in snapshot["occupancy"].items():
            self.occupancy[name] = dict(services)
