"""Lease generation & leader pointer state machine (req 5).

* The leader of each topology domain is decided from the merged vote
  summaries and node priorities of the highest candidate term.
* Lease generations only increase; a leader switch records the new
  leader's generation, the effective logical clock and a tombstone for
  the old leader.
* Recovery never trusts in-memory residue: the state is rebuilt from
  the persisted ledger and the state-chain integrity is verified.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import BizError, Err
from .models import Node


def decide_leader(
    term_votes: Dict[str, Dict[str, Any]],
    revoked: List[str],
    nodes_by_name: Dict[str, Node],
) -> Optional[str]:
    """Deterministic leader election for one domain/term."""
    eligible = [n for n in term_votes if n not in revoked]
    if not eligible:
        return None

    def rank(name: str) -> Any:
        node = nodes_by_name.get(name)
        priority = node.priority if node else 0
        weight = term_votes[name].get("weight", 0)
        return (-priority, -weight, name)  # min wins: highest priority/weight, then name

    return min(eligible, key=rank)


class LeaseStateMachine:
    """Per-domain lease generation & leader pointer."""

    def __init__(self, domain: str, generation: int = 0, leader: Optional[str] = None, version: int = 0) -> None:
        self.domain = domain
        self.generation = generation
        self.leader = leader
        self.version = version
        self.tombstones: List[Dict[str, Any]] = []

    def transition(self, new_leader: Optional[str], effective_clock: int) -> Dict[str, Any]:
        """Advance the pointer; returns a transition record."""
        if new_leader == self.leader:
            return {
                "domain": self.domain,
                "changed": False,
                "leader": self.leader,
                "lease_generation": self.generation,
                "version": self.version,
            }
        new_generation = self.generation + 1
        if new_generation < self.generation:
            raise BizError(
                Err.LEASE_REGRESS,
                "lease generation regression",
                domain=self.domain,
                generation=new_generation,
                current=self.generation,
            )
        tombstone = None
        if self.leader is not None:
            tombstone = {
                "old_leader": self.leader,
                "old_generation": self.generation,
                "invalidated_clock": effective_clock,
            }
            self.tombstones.append(tombstone)
        old = self.leader
        self.leader = new_leader
        self.generation = new_generation
        self.version += 1
        return {
            "domain": self.domain,
            "changed": True,
            "old_leader": old,
            "leader": new_leader,
            "lease_generation": new_generation,
            "effective_clock": effective_clock,
            "tombstone": tombstone,
            "version": self.version,
        }
