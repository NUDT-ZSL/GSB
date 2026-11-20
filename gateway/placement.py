"""Replica placement under per-card VRAM and per-node concurrency budgets.

Placement is computed per round (window) over a fresh view of the node
pool. For each service the required replica count is derived from the
offered demand; replicas are then placed deterministically (nodes sorted
by name, cards by index, first-fit). Placement is all-or-nothing per
service: if any replica cannot be placed, the whole service placement is
rolled back and no VRAM/concurrency is consumed. Every attempt records
rejected candidates and the first constraint that fired.
"""
import math

from . import errors


class NodeState:
    __slots__ = ("name", "domain", "max_instances", "cards", "instances")

    def __init__(self, node):
        self.name = node.name
        self.domain = node.topology_domain
        self.max_instances = node.max_instances
        self.cards = [node.vram_per_card_mib] * node.gpu_cards
        self.instances = 0

    def snapshot(self):
        return {
            "node": self.name,
            "topology_domain": self.domain,
            "cards_remaining_mib": list(self.cards),
            "instances_used": self.instances,
            "max_instances": self.max_instances,
        }


def fresh_node_states(nodes):
    return {name: NodeState(node) for name, node in sorted(nodes.items())}


def plan_replica_count(service, demand, window_ms):
    capacity = service.compute_units * window_ms
    needed = math.ceil(demand / capacity) if demand > 0 else service.min_replicas
    return max(service.min_replicas, min(service.max_replicas, needed))


def place_service(service, replica_count, node_states, previous_nodes):
    """Try to place `replica_count` replicas of `service`.

    `previous_nodes` is the set of node names used by this service in the
    previous committed round (for cold-start detection). Returns a dict
    describing the outcome; on failure nothing is consumed
    (all-or-nothing).
    """
    placements = []
    rejected = []
    first_constraint = None
    tentative = []

    def note(reason, node, card):
        nonlocal first_constraint
        rejected.append({"node": node, "card": card, "reason": reason})
        if first_constraint is None:
            first_constraint = reason

    for replica_idx in range(replica_count):
        placed = False
        for name in sorted(node_states):
            state = node_states[name]
            if (service.topology_domain != "any"
                    and state.domain != service.topology_domain):
                note(errors.E_PLACEMENT_DOMAIN, name, -1)
                continue
            if state.instances >= state.max_instances:
                note(errors.E_PLACEMENT_CONCURRENCY, name, -1)
                continue
            chosen_card = -1
            for card_idx, remaining in enumerate(state.cards):
                if remaining >= service.vram_mib:
                    chosen_card = card_idx
                    break
                note(errors.E_PLACEMENT_VRAM, name, card_idx)
            if chosen_card < 0:
                continue
            state.cards[chosen_card] -= service.vram_mib
            state.instances += 1
            tentative.append((state, chosen_card))
            placements.append({
                "replica": replica_idx,
                "node": name,
                "card": chosen_card,
                "vram_mib": service.vram_mib,
                "cold_start": name not in previous_nodes,
            })
            placed = True
            break
        if not placed:
            # all-or-nothing: roll back tentative consumption
            for state, card_idx in tentative:
                state.cards[card_idx] += service.vram_mib
                state.instances -= 1
            return {
                "service": service.name,
                "status": "failed",
                "replicas": [],
                "rejected_candidates": rejected,
                "first_constraint": first_constraint,
            }

    return {
        "service": service.name,
        "status": "placed",
        "replicas": placements,
        "rejected_candidates": rejected,
        "first_constraint": first_constraint,
    }
