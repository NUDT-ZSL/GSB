"""Orchestration: sample loading, batch commit, scheduling, state reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .batcher import BatchFormer, merge_batches, merge_events_one_shot
from .merge import merge_topologies
from .models import Batch, Event, Node, Subscription
from .persistence import GENESIS_CHAIN, Ledger, chain_hash
from .statemachine import LeaseStateMachine, decide_leader
from .validation import (
    parse_events,
    parse_manifest,
    parse_subscriptions,
    parse_topo_v1,
    parse_topo_v2,
)

DEFAULT_INJECTED_CLOCK_MS = 120_000


class Samples:
    def __init__(self, sample_dir: str | Path, injected_clock_ms: int = DEFAULT_INJECTED_CLOCK_MS) -> None:
        self.dir = Path(sample_dir)
        self.nodes: List[Node] = parse_manifest(self.dir / "nodes.json")
        self.events: List[Event] = parse_events(self.dir / "events.jsonl")
        self.subs: List[Subscription] = parse_subscriptions(self.dir / "subscriptions.json", self.nodes)
        v1 = parse_topo_v1(self.dir / "topo_v1.json")
        v2, self.v2_epoch = parse_topo_v2(self.dir / "topo_v2")
        self.topo_index = merge_topologies(v1, v2, injected_clock_ms)
        self.nodes_by_name = {n.name: n for n in self.nodes}
        self._scope_by_node = {node: v["scope"] for node, v in self.topo_index["views"].items()}

    def domain_of(self, node: str) -> str:
        return self._scope_by_node.get(node, "domain-default")


def form_batches(samples: Samples, arrival: Optional[List[Event]] = None) -> "tuple[BatchFormer, List[Batch]]":
    former = BatchFormer(samples.domain_of)
    for ev in arrival if arrival is not None else samples.events:
        former.arrive(ev)
    return former, former.flush()


# ---------------- batch commit (req 4, 5, 7) ----------------


def commit_batch(
    ledger: Ledger,
    batch: Batch,
    nodes_by_name: Dict[str, Node],
    pending_json: str = "[]",
    crash_hook: Optional[Callable[[], None]] = None,
) -> int:
    """Commit one batch; returns the actual logical cost in ms.

    Two phases: the journal STARTED row is written in its own
    transaction, then votes + domain state + journal COMMITTED are
    written in a single atomic transaction.  A crash between the
    phases leaves a detectable half-committed journal entry.
    """
    started_clock = max((e.clock_ms for e in batch.events), default=0)
    with ledger.file_lock():
        conn = ledger.connect()
        try:
            with conn:  # phase 1: journal STARTED (own transaction)
                ledger.journal_start(conn, batch.batch_id, batch.domain, batch.topo_epoch, started_clock)
        finally:
            conn.close()

        if crash_hook is not None:
            crash_hook()

        conn = ledger.connect()
        try:
            with conn:  # phase 2: single atomic state transaction
                counted = 0
                for ev in sorted(batch.events, key=lambda e: e.seq):
                    cur = conn.execute(
                        "INSERT INTO votes VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(domain, candidate_term, node, seq) DO NOTHING",
                        (
                            batch.domain,
                            ev.candidate_term,
                            ev.node,
                            ev.seq,
                            ev.clock_ms,
                            json.dumps(ev.vote, sort_keys=True),
                            1 if ev.revoked else 0,
                            batch.batch_id,
                        ),
                    )
                    counted += cur.rowcount

                state_row = conn.execute(
                    "SELECT * FROM domain_state WHERE domain=?", (batch.domain,)
                ).fetchone()
                applied = set(json.loads(state_row["applied_seqs"])) if state_row else set()
                applied.update(e.seq for e in batch.events)
                seq_wm = state_row["seq_watermark"] if state_row else 0
                if batch.events:
                    seq_wm = max(seq_wm, *(e.seq for e in batch.events))
                clk_wm = max(
                    [state_row["clock_watermark"] if state_row else 0]
                    + [e.clock_ms for e in batch.events]
                )

                # leader decision from persisted votes of the highest term
                rows = conn.execute(
                    "SELECT node, vote_json, revoked, candidate_term, clock_ms FROM votes "
                    "WHERE domain=? ORDER BY candidate_term, node",
                    (batch.domain,),
                ).fetchall()
                by_term: Dict[int, Dict[str, Any]] = {}
                for r in rows:
                    t = by_term.setdefault(r["candidate_term"], {"votes": {}, "revoked": set(), "last_clock": 0})
                    vote = json.loads(r["vote_json"])
                    if r["revoked"]:
                        t["revoked"].add(r["node"])
                        t["votes"].pop(r["node"], None)
                    else:
                        t["votes"][r["node"]] = vote
                    t["last_clock"] = max(t["last_clock"], r["clock_ms"])
                highest = max(by_term) if by_term else None
                new_leader = None
                eff_clock = clk_wm
                if highest is not None:
                    t = by_term[highest]
                    new_leader = decide_leader(t["votes"], sorted(t["revoked"]), nodes_by_name)
                    eff_clock = t["last_clock"]

                sm = LeaseStateMachine(
                    batch.domain,
                    generation=state_row["lease_generation"] if state_row else 0,
                    leader=state_row["leader"] if state_row else None,
                    version=state_row["leader_version"] if state_row else 0,
                )
                transition = sm.transition(new_leader, eff_clock)

                prev_chain = state_row["chain_hash"] if state_row else GENESIS_CHAIN
                new_chain = prev_chain
                if transition["changed"]:
                    new_chain = chain_hash(
                        prev_chain,
                        {
                            "domain": batch.domain,
                            "lease_generation": sm.generation,
                            "leader": sm.leader,
                            "effective_clock": eff_clock,
                        },
                    )
                    conn.execute(
                        "INSERT INTO leader_history VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            batch.domain,
                            sm.generation,
                            sm.leader,
                            eff_clock,
                            json.dumps(transition["tombstone"], sort_keys=True)
                            if transition["tombstone"]
                            else None,
                            new_chain,
                        ),
                    )

                conn.execute(
                    "INSERT INTO domain_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(domain) DO UPDATE SET "
                    "seq_watermark=excluded.seq_watermark, clock_watermark=excluded.clock_watermark, "
                    "lease_generation=excluded.lease_generation, leader=excluded.leader, "
                    "leader_version=excluded.leader_version, pending_json=excluded.pending_json, "
                    "applied_seqs=excluded.applied_seqs, chain_hash=excluded.chain_hash, "
                    "updated_clock=excluded.updated_clock",
                    (
                        batch.domain,
                        seq_wm,
                        clk_wm,
                        sm.generation,
                        sm.leader,
                        sm.version,
                        pending_json,
                        json.dumps(sorted(applied)),
                        new_chain,
                        clk_wm,
                    ),
                )
                conn.execute(
                    "UPDATE batch_journal SET status='COMMITTED', committed_clock=? "
                    "WHERE batch_id=? AND topo_epoch=?",
                    (clk_wm, batch.batch_id, batch.topo_epoch),
                )
        finally:
            conn.close()
    return batch.est_cost_ms


# ---------------- scheduling (req 4) ----------------


def run_scheduler(
    ledger: Ledger,
    batches: List[Batch],
    nodes_by_name: Dict[str, Node],
    max_concurrency: int,
    time_budget_ms: int,
    crash_after_batch: Optional[int] = None,
) -> List[Dict[str, Any]]:
    from .scheduler import MergeScheduler

    scheduler = MergeScheduler(max_concurrency=max_concurrency, time_budget_ms=time_budget_ms)
    committed = {"n": 0}

    def _commit(batch: Batch) -> int:
        hook = None
        if crash_after_batch is not None and committed["n"] + 1 == crash_after_batch:
            hook = lambda: os._exit(137)  # simulate SIGKILL mid-commit
        cost = commit_batch(ledger, batch, nodes_by_name, crash_hook=hook)
        committed["n"] += 1
        return cost

    return scheduler.run(batches, _commit)


# ---------------- reports (req 8) ----------------


def build_state_report(ledger: Ledger, samples: Samples, former: Optional[BatchFormer] = None) -> Dict[str, Any]:
    domains = {}
    for st in ledger.list_domain_states():
        domains[st["domain"]] = {
            "leader": st["leader"],
            "lease_generation": st["lease_generation"],
            "leader_version": st["leader_version"],
            "seq_watermark": st["seq_watermark"],
            "clock_watermark": st["clock_watermark"],
            "applied_seqs": json.loads(st["applied_seqs"]),
            "pending": json.loads(st["pending_json"]),
            "chain_hash": st["chain_hash"],
        }
    votes = {}
    for domain in domains:
        votes[domain] = [
            {"node": v["node"], "term": v["candidate_term"], "seq": v["seq"], "revoked": bool(v["revoked"])}
            for v in ledger.get_votes(domain)
        ]
    stats = {
        "nodes": len(samples.nodes),
        "events": len(samples.events),
        "subscriptions": len(samples.subs),
        "topology_views": len(samples.topo_index["views"]),
        "topology_tombstones": len(samples.topo_index["tombstones"]),
        "domains": len(domains),
        "half_committed_batches": len(ledger.half_committed()),
    }
    return {
        "leaders": {d: domains[d]["leader"] for d in sorted(domains)},
        "lease_generations": {d: domains[d]["lease_generation"] for d in sorted(domains)},
        "domains": {d: domains[d] for d in sorted(domains)},
        "votes": {d: votes[d] for d in sorted(votes)},
        "field_stats": stats,
        "buffer_status": former.buffer_status() if former else None,
    }


def verify_replay_consistency(samples: Samples) -> Dict[str, Any]:
    """Batched merge vs one-shot replay must be item-for-item identical."""
    _, batches = form_batches(samples)
    batched = merge_batches(batches, samples.domain_of)
    one_shot = merge_events_one_shot(samples.events, samples.domain_of)
    return {
        "consistent": batched == one_shot,
        "batches": len(batches),
        "events": len(samples.events),
        "batched_state": batched,
        "one_shot_state": one_shot,
    }
