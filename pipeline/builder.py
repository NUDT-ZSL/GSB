"""Deterministic three-stage index builder with crash-recovery.

Stages (committed in order, each with monotonically increasing version):
    ingest   -> validate docs, sort by doc_id, record doc stats
    compress -> build per-term posting lists, chunk + varint compress
    stats    -> corpus statistics (doc_count, avgdl, df, doc_len map)

Recovery: on open, committed snapshots are loaded; any uncommitted wip
state is discarded and the interrupted stage restarts from its origin.
"""

import json
import os
import threading
import hashlib

from .encoding import chunk_postings, encode_block
from .errors import ConfigInvalidError, ResourceLimitError
from .snapshot import STAGES, SnapshotStore, _atomic_write, canonical_bytes

KILL_ENV = "INDEX_PIPELINE_KILL_AT"


class KillSwitch:
    """Deterministic crash simulator: kills the process at named points."""

    def __init__(self):
        raw = os.environ.get(KILL_ENV, "")
        self._points = {p.strip() for p in raw.split(",") if p.strip()}

    def maybe_kill(self, point):
        if point in self._points:
            os._exit(99)  # simulate SIGKILL: no cleanup, no exit report


class BuildConfig:
    def __init__(self, block_size=8, max_postings=1_000_000,
                 max_resident_bytes=64 * 1024 * 1024,
                 max_cache_blocks=1024, k1=1.2, b=0.75,
                 score_tolerance=1e-9):
        if not isinstance(block_size, int) or block_size <= 0:
            raise ConfigInvalidError("block_size must be a positive integer",
                                     block_size=block_size)
        if max_postings <= 0 or max_resident_bytes <= 0 or max_cache_blocks < 0:
            raise ConfigInvalidError("resource limits must be positive")
        self.block_size = block_size
        self.max_postings = max_postings
        self.max_resident_bytes = max_resident_bytes
        self.max_cache_blocks = max_cache_blocks
        self.k1 = float(k1)
        self.b = float(b)
        self.score_tolerance = float(score_tolerance)

    def to_dict(self):
        return {
            "block_size": self.block_size,
            "max_postings": self.max_postings,
            "max_resident_bytes": self.max_resident_bytes,
            "max_cache_blocks": self.max_cache_blocks,
            "k1": self.k1,
            "b": self.b,
            "score_tolerance": self.score_tolerance,
        }


class IndexBuilder:
    def __init__(self, store_dir, config=None, kill_switch=None):
        self.store = SnapshotStore(store_dir)
        self.config = config or BuildConfig()
        self.kill = kill_switch or KillSwitch()
        self._build_lock = threading.Lock()
        self._committed = self.store.load_all_committed()

    # ---------- stage helpers ----------

    def _stage_done(self, stage):
        return stage in self._committed

    def _commit(self, stage, committed_docs, payload):
        version = self.store.committed_version(stage) + 1
        header = self.store.commit(stage, version, committed_docs, payload)
        self._committed[stage] = {"header": header, "payload": payload}
        return header

    # ---------- public entry ----------

    def build(self, docs_path):
        """Run remaining stages; safe to call again after a crash."""
        if not self._build_lock.acquire(blocking=False):
            from .errors import BuildBusyError
            raise BuildBusyError("a build is already running in this process")
        try:
            self.store.discard_wip()  # uncommitted intermediates never trusted
            self._check_source_changed(docs_path)
            ingest = self._run_ingest(docs_path)
            compress = self._run_compress(ingest)
            stats = self._run_stats(ingest, compress)
            self._write_index(ingest, compress, stats)
            return self._build_summary(ingest, compress, stats)
        finally:
            self._build_lock.release()

    def _check_source_changed(self, docs_path):
        """If the input or config changed vs the committed ingest snapshot,
        drop all committed stages so the whole index is rebuilt (versions
        keep increasing via the store manifest -> whole replacement, never
        incremental patching)."""
        if not self._stage_done("ingest"):
            return
        digest = hashlib.sha256()
        with open(docs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        payload = self._committed["ingest"]["payload"]
        if (payload.get("source_checksum") != digest.hexdigest()
                or payload.get("config") != self.config.to_dict()):
            self._committed = {}

    # ---------- stage 1: ingest ----------

    def _run_ingest(self, docs_path):
        if self._stage_done("ingest"):
            return self._committed["ingest"]["payload"]
        docs = []
        seen = set()
        with open(docs_path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                doc_id = rec["doc_id"]
                if not isinstance(doc_id, int) or doc_id in seen:
                    raise ConfigInvalidError("bad or duplicate doc_id",
                                             line=line_no, doc_id=doc_id)
                seen.add(doc_id)
                terms = {}
                for entry in rec["terms"]:
                    term, tf = entry["term"], int(entry["tf"])
                    if tf <= 0:
                        raise ConfigInvalidError("tf must be positive",
                                                 line=line_no, term=term)
                    terms[term] = terms.get(term, 0) + tf
                doc_len = int(rec["doc_len"])
                if doc_len != sum(terms.values()):
                    raise ConfigInvalidError("doc_len mismatch",
                                             line=line_no, doc_id=doc_id)
                docs.append({"doc_id": doc_id, "terms": terms,
                             "doc_len": doc_len})
                self.kill.maybe_kill("ingest:doc")
        docs.sort(key=lambda d: d["doc_id"])
        digest = hashlib.sha256()
        with open(docs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        payload = {"docs": docs,
                   "doc_ids": [d["doc_id"] for d in docs],
                   "source_checksum": digest.hexdigest(),
                   "config": self.config.to_dict()}
        self.kill.maybe_kill("ingest:pre_commit")
        self._commit("ingest", [d["doc_id"] for d in docs], payload)
        return payload

    # ---------- stage 2: compress ----------

    def _run_compress(self, ingest):
        if self._stage_done("compress"):
            return self._committed["compress"]["payload"]
        term_postings = {}
        for doc in ingest["docs"]:  # doc_id ascending
            doc_id = doc["doc_id"]
            for term in sorted(doc["terms"]):  # lexicographic term order
                term_postings.setdefault(term, []).append(
                    (doc_id, doc["terms"][term]))
            self.kill.maybe_kill("compress:doc")
        total_postings = sum(len(v) for v in term_postings.values())
        if total_postings > self.config.max_postings:
            raise ResourceLimitError("posting count exceeds limit",
                                     total=total_postings,
                                     limit=self.config.max_postings)
        terms_payload = {}
        for term in sorted(term_postings):
            postings = term_postings[term]
            blocks = []
            for chunk in chunk_postings(postings, self.config.block_size):
                blob = encode_block(chunk)
                blocks.append({
                    "count": len(chunk),
                    "first_doc_id": chunk[0][0],
                    "byte_len": len(blob),
                    "data": blob.hex(),
                })
            terms_payload[term] = {"df": len(postings), "blocks": blocks}
        payload = {"terms": terms_payload, "total_postings": total_postings,
                   "block_size": self.config.block_size}
        self.kill.maybe_kill("compress:pre_commit")
        self._commit("compress", ingest["doc_ids"], payload)
        return payload

    # ---------- stage 3: stats ----------

    def _run_stats(self, ingest, compress):
        if self._stage_done("stats"):
            return self._committed["stats"]["payload"]
        doc_len = {str(d["doc_id"]): d["doc_len"] for d in ingest["docs"]}
        doc_count = len(ingest["docs"])
        avgdl = (sum(doc_len.values()) / doc_count) if doc_count else 0.0
        df = {term: compress["terms"][term]["df"]
              for term in sorted(compress["terms"])}
        payload = {
            "doc_count": doc_count,
            "avgdl": avgdl,
            "doc_len": doc_len,
            "df": df,
            "total_postings": compress["total_postings"],
            "k1": self.config.k1,
            "b": self.config.b,
        }
        self.kill.maybe_kill("stats:pre_commit")
        self._commit("stats", ingest["doc_ids"], payload)
        return payload

    # ---------- final index artifact ----------

    def _index_payload(self, ingest, compress, stats):
        return {
            "format": "uid2-index-v1",
            "config": self.config.to_dict(),
            "terms": compress["terms"],
            "stats": stats,
            "doc_ids": ingest["doc_ids"],
        }

    def _write_index(self, ingest, compress, stats):
        payload = self._index_payload(ingest, compress, stats)
        index_path = os.path.join(self.store.root, "index.json")
        _atomic_write(index_path, canonical_bytes(payload))
        return index_path

    def _build_summary(self, ingest, compress, stats):
        return {
            "doc_count": stats["doc_count"],
            "term_count": len(compress["terms"]),
            "total_postings": compress["total_postings"],
            "block_size": self.config.block_size,
            "avgdl": stats["avgdl"],
            "stage_versions": {
                s: self._committed[s]["header"]["version"] for s in STAGES
            },
        }
