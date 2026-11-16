"""Resident block store, reference counts and eviction scheduler."""

from .errors import E_EVICT_POLICY_INVALID, WorkbenchError


class CacheBlock:
    """One cached prefix block plus its residency metadata."""

    def __init__(
        self,
        index,
        token_ids,
        start,
        end,
        content_fp,
        parent_fp,
        chain_fp,
        embedding_digest,
        owners,
        ref_count=0,
        last_used=-1,
        resident=True,
        invalidated=False,
        invalid_reason=None,
    ):
        self.index = index
        self.token_ids = list(token_ids)
        self.start = start
        self.end = end
        self.content_fp = content_fp
        self.parent_fp = parent_fp
        self.chain_fp = chain_fp
        self.embedding_digest = list(embedding_digest)
        self.owners = list(owners)
        self.ref_count = ref_count
        self.last_used = last_used
        self.resident = resident
        self.invalidated = invalidated
        self.invalid_reason = invalid_reason

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


class CacheStore:
    """In-memory block registry with snapshot serialization support."""

    def __init__(self, policy):
        if policy not in ("LRU", "LFU"):
            raise WorkbenchError(
                E_EVICT_POLICY_INVALID,
                "startup",
                f"unsupported eviction policy: {policy}",
            )
        self.policy = policy
        self.registry = {}
        self.children = {}
        self.session_blocks = {}
        self.peak_resident = 0
        self.budget_utilization_events = []

    def resident_count(self):
        return sum(1 for b in self.registry.values() if b.resident)

    def add_owner(self, chain_fp, session):
        block = self.registry[chain_fp]
        if session not in block.owners:
            block.owners.append(session)
            block.owners.sort()
        self.session_blocks.setdefault(session, [])
        if chain_fp not in self.session_blocks[session]:
            self.session_blocks[session].append(chain_fp)

    def register(self, block, session):
        self.registry[block.chain_fp] = block
        self.children.setdefault(block.parent_fp, set()).add(block.chain_fp)
        self.add_owner(block.chain_fp, session)

    def touch(self, chain_fp, sequence):
        block = self.registry[chain_fp]
        block.ref_count += 1
        block.last_used = sequence

    def downstream(self, chain_fp):
        """Transitive successors following the parent->child graph."""
        result = []
        stack = list(self.children.get(chain_fp, ()))
        seen = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            stack.extend(self.children.get(current, ()))
        return result

    def eviction_candidate(self):
        """Select a resident block deterministically by policy.

        LRU: smallest last_used, LFU: smallest ref_count; ties in both
        policies break by chain fingerprint lexicographic order.
        """
        residents = [b for b in self.registry.values() if b.resident]
        if not residents:
            return None
        if self.policy == "LRU":
            return min(
                residents, key=lambda b: (b.last_used, b.chain_fp)
            )
        return min(
            residents, key=lambda b: (b.ref_count, b.chain_fp)
        )

    def session_eviction_candidate(self, session):
        """Policy-consistent victim restricted to one session's blocks."""
        residents = [
            self.registry[fp]
            for fp in self.session_blocks.get(session, ())
            if fp in self.registry and self.registry[fp].resident
        ]
        if not residents:
            return None
        if self.policy == "LRU":
            return min(residents, key=lambda b: (b.last_used, b.chain_fp))
        return min(residents, key=lambda b: (b.ref_count, b.chain_fp))

    def snapshot_state(self):
        return {
            "policy": self.policy,
            "registry": {fp: b.to_dict() for fp, b in self.registry.items()},
            "children": {
                ("ROOT" if fp is None else fp): sorted(children)
                for fp, children in self.children.items()
            },
            "session_blocks": {
                session: list(fps)
                for session, fps in sorted(self.session_blocks.items())
            },
            "peak_resident": self.peak_resident,
            "budget_utilization_events": list(self.budget_utilization_events),
        }

    def restore_state(self, state):
        self.policy = state["policy"]
        self.registry = {
            fp: CacheBlock.from_dict(data)
            for fp, data in state["registry"].items()
        }
        self.children = {
            (None if fp == "ROOT" else fp): set(children)
            for fp, children in state["children"].items()
        }
        self.session_blocks = {
            session: list(fps)
            for session, fps in state["session_blocks"].items()
        }
        self.peak_resident = state["peak_resident"]
        self.budget_utilization_events = list(
            state.get("budget_utilization_events", [])
        )
