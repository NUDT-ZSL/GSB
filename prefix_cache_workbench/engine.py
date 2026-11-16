"""Prefix-cache engine: matching, residency, eviction and recompute."""

import threading

from .cache import CacheBlock, CacheStore
from .chunker import assemble_context, build_block_sequence
from .errors import (
    E_CHAIN_BROKEN,
    E_EVICT_POLICY_INVALID,
    E_SESSION_LIMIT_EXCEEDED,
    WorkbenchError,
)
from .hashing import (
    config_fingerprint,
    vocab_fingerprint,
)


class Engine:
    def __init__(self, config, vocab, encoder, snapshot_store=None, clock=None):
        policy = config.get("eviction_policy")
        if policy not in ("LRU", "LFU"):
            raise WorkbenchError(
                E_EVICT_POLICY_INVALID,
                "startup",
                f"unsupported eviction policy: {policy}",
            )
        self.config = config
        self.vocab = vocab
        self.encoder = encoder
        self.store = CacheStore(policy)
        self.snapshot_store = snapshot_store
        self.clock = clock
        self.lock = threading.RLock()
        self.sequence = 0
        self.events = []
        self.failures = []
        self.request_results = []
        self.cursor = 0
        self.snapshot_version = 0
        self.recovery = {
            "recovered": False,
            "stale": False,
            "reason": None,
            "version": None,
        }
        self.config_fp = config_fingerprint(config)
        self.vocab_fp = vocab_fingerprint(vocab.rules)

    def next_sequence(self):
        self.sequence += 1
        return self.sequence

    def prepare_request(self, request):
        """Pure stage: validate, tokenize and compute the full block plan.

        This stage never touches shared cache state so it is safe to run on a
        worker thread; failures here cannot pollute residency statistics.
        """
        token_ids = assemble_context(request, self.vocab)
        blocks = build_block_sequence(token_ids, self.config, self.vocab)
        return blocks

    def stable_sort_key(self, request):
        return (
            str(request.get("session", "")),
            int(request.get("turn", 0)),
            int(request.get("stream_index", 0)),
        )

    def _record_event(self, event_type, **payload):
        event = {"seq": self.next_sequence(), "type": event_type}
        event.update(payload)
        self.events.append(event)
        return event

    def _match_prefix(self, blocks, request):
        """Fingerprint-only prefix match against the block registry."""
        declared_end = 0
        resident_end = 0
        chain_broken = False
        resident_continuous = True
        parent_fp = None
        for block in blocks:
            chain_fp = block["chain_fp"]
            known = self.store.registry.get(chain_fp)
            if known is None:
                break
            if known.parent_fp != parent_fp:
                if block["index"] > 0:
                    chain_broken = True
                    self._record_event(
                        E_CHAIN_BROKEN,
                        session=request["session"],
                        turn=request["turn"],
                        block_index=block["index"],
                        chain_fp=chain_fp,
                        expected_parent_fp=parent_fp,
                        stored_parent_fp=known.parent_fp,
                        injection_id=request.get("injection_id"),
                    )
                break
            declared_end += 1
            if known.resident and not known.invalidated and resident_continuous:
                resident_end += 1
            else:
                if block["index"] > 0:
                    chain_broken = True
                    self._record_event(
                        E_CHAIN_BROKEN,
                        session=request["session"],
                        turn=request["turn"],
                        block_index=block["index"],
                        chain_fp=chain_fp,
                        expected_parent_fp=parent_fp,
                        stored_parent_fp=known.parent_fp,
                        resident=known.resident,
                        invalidated=known.invalidated,
                        injection_id=request.get("injection_id"),
                    )
                resident_continuous = False
                resident_end = 0
            parent_fp = chain_fp
        min_reuse = int(self.config["min_reusable_blocks"])
        reusable = declared_end if declared_end >= min_reuse else 0
        resident_reusable = (
            resident_end if resident_end >= min_reuse and reusable else 0
        )
        return {
            "declared_reusable_blocks": reusable,
            "resident_reusable_blocks": resident_reusable,
            "reusable_range": [0, reusable],
            "new_range": [reusable, len(blocks)],
            "chain_broken": chain_broken,
            "resident_chain_broken": chain_broken,
        }

    def _invalidate_downstream(self, chain_fp, reason, session, turn):
        block = self.store.registry[chain_fp]
        affected = self.store.downstream(chain_fp)
        for child_fp in affected:
            child = self.store.registry.get(child_fp)
            if child is None:
                continue
            child.resident = False
            child.invalidated = True
            child.invalid_reason = reason
        self._record_event(
            "INVALIDATE",
            session=session,
            turn=turn,
            origin_chain_fp=chain_fp,
            reason=reason,
            affected_chain_fps=affected,
        )
        return affected

    def _evict_one(
        self,
        reason,
        session,
        turn,
        injection_id=None,
        chain_broken_block=None,
        victim=None,
    ):
        if victim is None:
            victim = self.store.eviction_candidate()
        if victim is None:
            return None
        victim.resident = False
        victim.invalidated = True
        victim.invalid_reason = reason
        downstream_reason = (
            "chain_dependency_broken"
            if reason == "budget_eviction"
            else reason
        )
        affected = self._invalidate_downstream(
            victim.chain_fp, downstream_reason, session, turn
        )
        self._record_event(
            "EVICT",
            session=session,
            turn=turn,
            reason=reason,
            eviction_policy=self.store.policy,
            evicted_chain_fp=victim.chain_fp,
            evicted_content_fp=victim.content_fp,
            owners=list(victim.owners),
            ref_count=victim.ref_count,
            last_used=victim.last_used,
            affected_chain_fps=affected,
            injection_id=injection_id,
            chain_broken_block=chain_broken_block,
        )
        return victim

    def _enforce_session_cap(self, session, turn, injection_id):
        cap = int(self.config["session_retention_cap"])
        session_fps = self.store.session_blocks.get(session, [])
        block_count_before = len(session_fps)
        exceeded = block_count_before > cap
        while len(session_fps) > cap:
            fps = self.store.session_blocks[session]
            victim = self.store.session_eviction_candidate(session)
            if victim is not None:
                self._evict_one(
                    "budget_eviction",
                    session,
                    turn,
                    injection_id,
                    chain_broken_block=victim.chain_fp,
                    victim=victim,
                )
                fps.remove(victim.chain_fp)
            else:
                fps.pop(0)
        if exceeded:
            self._record_event(
                E_SESSION_LIMIT_EXCEEDED,
                session=session,
                turn=turn,
                session_retention_cap=cap,
                injection_id=injection_id,
                session_block_count_before_trim=block_count_before,
            )

    def _enforce_budget(self, session, turn, injection_id):
        budget = int(self.config["residency_budget_blocks"])
        while self.store.resident_count() > budget:
            self._evict_one(
                "budget_eviction", session, turn, injection_id
            )
        count = self.store.resident_count()
        if count > self.store.peak_resident:
            self.store.peak_resident = count
        self.store.budget_utilization_events.append(
            {
                "seq": self.sequence,
                "resident": count,
                "budget": budget,
                "utilization": round(count / budget, 6) if budget else None,
            }
        )

    def _recompute_if_needed(self, block, session, turn):
        known = self.store.registry.get(block["chain_fp"])
        if known is None or not known.invalidated:
            return False, known
        if known is None:
            known = CacheBlock(
                index=block["index"],
                token_ids=block["token_ids"],
                start=block["start"],
                end=block["end"],
                content_fp=block["content_fp"],
                parent_fp=block["parent_fp"],
                chain_fp=block["chain_fp"],
                embedding_digest=self.encoder.block_digest(block["token_ids"]),
                owners=[session],
            )
            self.store.register(known, session)
        else:
            known.resident = True
        known.invalidated = False
        reason = known.invalid_reason
        known.invalid_reason = None
        self._record_event(
            "RECOMPUTE",
            session=session,
            turn=turn,
            block_index=block["index"],
            chain_fp=block["chain_fp"],
            content_fp=block["content_fp"],
            origin=reason,
            field_match=True,
        )
        return True, known

    def commit_request(self, request, blocks):
        """Ordered commit stage; executes under the engine lock."""
        with self.lock:
            match = self._match_prefix(blocks, request)
            session = request["session"]
            turn = request["turn"]
            injection_id = request.get("injection_id")
            recomputed = []
            for block in blocks:
                chain_fp = block["chain_fp"]
                known = self.store.registry.get(chain_fp)
                if known is not None and known.resident and not known.invalidated:
                    self.store.touch(chain_fp, self.next_sequence())
                    self.store.add_owner(chain_fp, session)
                    continue
                was_recomputed, known = self._recompute_if_needed(
                    block, session, turn
                )
                if was_recomputed:
                    recomputed.append(chain_fp)
                    self.store.touch(chain_fp, self.next_sequence())
                    self.store.add_owner(chain_fp, session)
                    continue
                cache_block = CacheBlock(
                    index=block["index"],
                    token_ids=block["token_ids"],
                    start=block["start"],
                    end=block["end"],
                    content_fp=block["content_fp"],
                    parent_fp=block["parent_fp"],
                    chain_fp=chain_fp,
                    embedding_digest=self.encoder.block_digest(block["token_ids"]),
                    owners=[session],
                )
                self.store.register(cache_block, session)
                self.store.touch(chain_fp, self.next_sequence())
            self._enforce_session_cap(session, turn, injection_id)
            self._enforce_budget(session, turn, injection_id)
            result = {
                "stream_index": request.get("stream_index"),
                "sort_key": list(self.stable_sort_key(request)),
                "session": session,
                "turn": turn,
                "block_count": len(blocks),
                "chain_fps": [b["chain_fp"] for b in blocks],
                "content_fps": [b["content_fp"] for b in blocks],
                "reusable_range": match["reusable_range"],
                "new_range": match["new_range"],
                "declared_reusable_blocks": match["declared_reusable_blocks"],
                "resident_reusable_blocks": match["resident_reusable_blocks"],
                "recomputed_chain_fps": recomputed,
                "chain_broken": match["chain_broken"],
                "injection_id": injection_id,
                "boundaries": [
                    {"index": b["index"], "start": b["start"], "end": b["end"]}
                    for b in blocks
                ],
            }
            self.request_results.append(result)
            self.cursor += 1
            return result

    def _fail(self, request, stage, exc):
        record = {
            "stream_index": request.get("stream_index"),
            "sort_key": list(self.stable_sort_key(request)),
            "session": request.get("session"),
            "turn": request.get("turn"),
            "stage": stage,
            "code": getattr(exc, "code", type(exc).__name__),
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "injection_id": request.get("injection_id"),
        }
        with self.lock:
            self.failures.append(record)
            self.request_results.append(
                {
                    "stream_index": request.get("stream_index"),
                    "sort_key": record["sort_key"],
                    "session": request.get("session"),
                    "turn": request.get("turn"),
                    "status": "failed",
                    "code": record["code"],
                    "injection_id": request.get("injection_id"),
                }
            )
            self.cursor += 1
        return record

    def run_serial(self, requests, stop_event=None):
        for request in requests:
            if stop_event is not None and stop_event.is_set():
                break
            self._run_one(request, stop_event)
        return self

    def run_concurrent(self, requests, max_workers, stop_event=None):
        """Prepare in a thread pool, commit strictly in stream order.

        Worker threads only run the pure prepare stage; the main thread waits
        for each future in stream-index order before touching shared cache
        state, so completion timing can never affect observable results.
        """
        from concurrent.futures import ThreadPoolExecutor

        def work(item):
            index, request = item
            try:
                return index, request, self.prepare_request(request), None
            except Exception as exc:  # failure isolation
                return index, request, None, exc

        ordered = list(enumerate(requests))
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futures = [pool.submit(work, item) for item in ordered]
            for index, future in enumerate(futures):
                if stop_event is not None and stop_event.is_set():
                    break
                future_index, request, blocks, exc = future.result()
                if future_index != index:
                    raise RuntimeError("commit order drift detected")
                if exc is not None:
                    self._fail(
                        request, getattr(exc, "stage", "prepare"), exc
                    )
                    continue
                self._run_one(request, stop_event, prebuilt=blocks)
        return self

    def _run_one(self, request, stop_event=None, prebuilt=None):
        try:
            blocks = prebuilt or self.prepare_request(request)
        except Exception as exc:
            return self._fail(request, getattr(exc, "stage", "prepare"), exc)
        try:
            result = self.commit_request(request, blocks)
        except Exception as exc:
            return self._fail(request, getattr(exc, "stage", "commit"), exc)
        interval = int(self.config.get("snapshot_commit_interval", 1))
        if self.snapshot_store is not None and self.cursor % interval == 0:
            self.snapshot_store.write(self._snapshot_payload())
            self.snapshot_version = self.snapshot_store.version
        return result

    def shutdown(self):
        if self.snapshot_store is not None:
            self.snapshot_store.write(self._snapshot_payload())
            self.snapshot_version = self.snapshot_store.version

    def _snapshot_payload(self):
        return {
            "version": self.snapshot_store.version if self.snapshot_store else 0,
            "config_fp": self.config_fp,
            "vocab_fp": self.vocab_fp,
            "cursor": self.cursor,
            "sequence": self.sequence,
            "store": self.store.snapshot_state(),
            "events": self.events,
            "failures": self.failures,
            "request_results": self.request_results,
        }

    def restore_payload(self, payload):
        if (
            payload.get("config_fp") != self.config_fp
            or payload.get("vocab_fp") != self.vocab_fp
        ):
            return False
        self.cursor = int(payload["cursor"])
        self.sequence = int(payload["sequence"])
        self.store.restore_state(payload["store"])
        self.events = list(payload["events"])
        self.failures = list(payload["failures"])
        self.request_results = list(payload["request_results"])
        self.snapshot_version = int(payload["version"])
        self.recovery = {
            "recovered": True,
            "stale": False,
            "reason": None,
            "version": self.snapshot_version,
        }
        return True

    def mark_stale(self, reason):
        self.recovery = {
            "recovered": False,
            "stale": True,
            "reason": reason,
            "version": None,
        }

    def chain_dependency_view(self, chain_fp):
        block = self.store.registry.get(chain_fp)
        if block is None:
            return None
        chain = []
        current = block
        while current is not None:
            chain.append(
                {
                    "chain_fp": current.chain_fp,
                    "parent_fp": current.parent_fp,
                    "content_fp": current.content_fp,
                    "index": current.index,
                    "owners": list(current.owners),
                    "resident": current.resident,
                    "invalidated": current.invalidated,
                    "invalid_reason": current.invalid_reason,
                    "ref_count": current.ref_count,
                    "last_used": current.last_used,
                }
            )
            current = (
                self.store.registry.get(current.parent_fp)
                if current.parent_fp
                else None
            )
        return {
            "chain_fp": chain_fp,
            "ancestors_first": list(reversed(chain)),
            "downstream": self.store.downstream(chain_fp),
        }
