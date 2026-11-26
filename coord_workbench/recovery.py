"""Crash recovery (req 7).

After a SIGKILL in the middle of a merge or policy commit, recovery:
  1. verifies database page alignment;
  2. finds half-committed batches via the persisted journal
     (STARTED but not COMMITTED);
  3. replays only the missing events of those batches (events whose
     seq is not yet in the persisted applied-seq set) -- vote counts
     are never accumulated twice;
  4. validates the leader state-chain integrity per domain.

Recovery reads only the persisted ledger; in-memory residue is never
restored directly.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .engine import commit_batch
from .errors import BizError, Err
from .models import Batch, Node
from .persistence import GENESIS_CHAIN, Ledger, chain_hash


def validate_chain(ledger: Ledger, domain: str) -> Dict[str, Any]:
    history = ledger.leader_history(domain)
    prev = GENESIS_CHAIN
    for row in history:
        expect = chain_hash(
            prev,
            {
                "domain": row["domain"],
                "lease_generation": row["lease_generation"],
                "leader": row["leader"],
                "effective_clock": row["effective_clock"],
            },
        )
        if expect != row["chain_hash"]:
            raise BizError(
                Err.CHAIN_BROKEN,
                "leader state chain integrity check failed",
                domain=domain,
                lease_generation=row["lease_generation"],
                expected=expect,
                found=row["chain_hash"],
            )
        prev = expect
    state = ledger.get_domain_state(domain)
    if state is not None and state["chain_hash"] != prev:
        raise BizError(
            Err.CHAIN_BROKEN,
            "domain state chain head mismatch",
            domain=domain,
            expected=prev,
            found=state["chain_hash"],
        )
    return {"domain": domain, "links": len(history), "head": prev, "ok": True}


def recover(
    ledger: Ledger,
    batches: List[Batch],
    nodes_by_name: Dict[str, Node],
) -> Dict[str, Any]:
    page = ledger.check_page_alignment()
    half = {h["batch_id"]: h for h in ledger.half_committed()}
    replayed: List[Dict[str, Any]] = []
    for batch in batches:
        if batch.batch_id not in half:
            continue
        state = ledger.get_domain_state(batch.domain)
        applied = set(json.loads(state["applied_seqs"])) if state else set()
        missing = [e for e in batch.events if e.seq not in applied]
        if missing:
            replay_batch = Batch(
                batch_id=batch.batch_id,
                domain=batch.domain,
                topo_epoch=batch.topo_epoch,
                events=missing,
                est_cost_ms=batch.est_cost_ms,
            )
            commit_batch(ledger, replay_batch, nodes_by_name)
        else:
            # all events already applied; just close the journal entry
            conn = ledger.connect()
            try:
                with conn:
                    conn.execute(
                        "UPDATE batch_journal SET status='COMMITTED', committed_clock=? "
                        "WHERE batch_id=? AND topo_epoch=?",
                        (state["clock_watermark"] if state else 0, batch.batch_id, batch.topo_epoch),
                    )
            finally:
                conn.close()
        replayed.append(
            {
                "batch_id": batch.batch_id,
                "domain": batch.domain,
                "missing_events": len(missing),
                "total_events": len(batch.events),
            }
        )
    domains = sorted({b.domain for b in batches})
    chains = [validate_chain(ledger, d) for d in domains]
    return {
        "page_alignment": page,
        "half_committed_found": sorted(half),
        "replayed": replayed,
        "chains": chains,
        "remaining_half_committed": len(ledger.half_committed()),
    }
