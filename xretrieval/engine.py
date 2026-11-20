"""Build scheduling, recovery and query execution."""

import os
from concurrent.futures import ThreadPoolExecutor

from .bridge import BridgeResource
from .canon import canonical_text, short_checksum
from .corpus import load_documents, load_queries
from .errors import E_CONFIG_INVALID, E_RESOURCE_LIMIT, XError
from .index import build_partition
from .scoring import bm25_raw, rank
from .store import BuildLock, StateStore, _write_atomic


class CrashSimulated(Exception):
    """Raised by crash hooks to simulate a forced build termination."""


class Engine:
    def __init__(self, state_dir):
        self.store = StateStore(state_dir)

    # ------------------------------------------------------------------ build

    def build(self, resource_path, docs_path, crash_hook=None,
              simulate_crash_after=None, recover=False):
        """Run one build batch. Returns the build report.

        Only partitions affected by the bridge-resource diff (or missing a
        committed snapshot) are rebuilt; every other partition reuses its
        committed snapshot. Partition snapshots are journaled as they
        complete, so a forced termination can resume from the last
        committed partition.
        """
        if recover:
            try:
                os.unlink(self.store.root / "BUILD.lock")
            except FileNotFoundError:
                pass
        resource = BridgeResource.load(resource_path)
        docs = load_documents(docs_path)
        docs_by_lang = {}
        for doc in docs:
            if doc["doc_lang"] not in resource.languages:
                raise XError(E_CONFIG_INVALID,
                             f"doc_lang {doc['doc_lang']!r} not known to the "
                             f"bridge resource",
                             doc_id=doc["doc_id"], field="doc_lang")
            docs_by_lang.setdefault(doc["doc_lang"], []).append(doc)

        batch_id = f"build-{short_checksum({'r': resource.content_checksum()})[:10]}"
        lock = BuildLock(self.store.root)
        self.store.ensure_dirs()
        lock.acquire(batch_id)
        try:
            return self._build_locked(resource, docs_by_lang, crash_hook,
                                      simulate_crash_after)
        finally:
            lock.release()

    def _build_locked(self, resource, docs_by_lang, crash_hook,
                      simulate_crash_after):
        store = self.store
        bridges, partitions = store.committed_view()
        bridge_by_resource_version = {}
        latest_bridge = None
        for record in bridges.values():
            bridge_by_resource_version[record["resource_version"]] = record
            if latest_bridge is None or (record["snapshot_version"] >
                                         latest_bridge["snapshot_version"]):
                latest_bridge = record

        # 1. commit the bridge snapshot on any version/checksum change
        #    (whole replacement, never incremental patching)
        new_checksum = resource.content_checksum()
        if (latest_bridge is None or
                latest_bridge["resource_version"] != resource.resource_version or
                latest_bridge.get("resource_checksum") != new_checksum):
            snapshot_version = (latest_bridge["snapshot_version"] + 1
                                if latest_bridge else 1)
            content = {
                "snapshot": "bridge",
                "snapshot_version": snapshot_version,
                "resource_version": resource.resource_version,
                "resource": resource.to_dict(),
            }
            from .canon import checksum
            content["checksum"] = checksum(content)
            path = store.commit_bridge_snapshot(content)
            bridge_ref = {
                "snapshot_version": snapshot_version,
                "resource_version": resource.resource_version,
                "checksum": content["checksum"],
                "snapshot_file": str(path.relative_to(store.root)),
            }
        else:
            bridge_ref = {
                "snapshot_version": latest_bridge["snapshot_version"],
                "resource_version": latest_bridge["resource_version"],
                "checksum": latest_bridge["checksum"],
                "snapshot_file": latest_bridge["snapshot_file"],
            }

        # 2. determine affected partitions from the resource diff
        affected = set()
        old_resources = {}
        for lang in resource.languages:
            committed = partitions.get(lang)
            if committed is None:
                affected.add(lang)
                continue
            old_version = committed["bridge_resource_version"]
            if old_version == resource.resource_version:
                continue
            if old_version not in old_resources:
                old_record = bridge_by_resource_version.get(old_version)
                if old_record is None:
                    affected.add(lang)
                    continue
                old_content = store.load_snapshot(
                    old_record["snapshot_file"], old_record["checksum"],
                    "bridge", old_record["snapshot_version"])
                old_resources[old_version] = BridgeResource.from_dict(
                    old_content["resource"])
            old_resource = old_resources[old_version]
            if lang in old_resource.affected_languages(resource):
                affected.add(lang)

        # 3. rebuild affected partitions, reuse the rest
        reused, rebuilt = [], []
        partition_refs = {}
        committed_count = 0
        for lang in sorted(resource.languages):
            committed = partitions.get(lang)
            if lang not in affected and committed is not None:
                store.load_snapshot(committed["snapshot_file"],
                                    committed["checksum"],
                                    f"partition:{lang}",
                                    committed["partition_version"])
                reused.append(lang)
                partition_refs[lang] = {
                    "partition_version": committed["partition_version"],
                    "bridge_resource_version":
                        committed["bridge_resource_version"],
                    "checksum": committed["checksum"],
                    "snapshot_file": committed["snapshot_file"],
                }
                continue
            version = (committed["partition_version"] + 1
                       if committed else 1)
            content = build_partition(
                lang, docs_by_lang.get(lang, []), version,
                resource.resource_version,
                resource.params["max_postings_per_partition"])
            path = store.commit_partition_snapshot(content)
            rebuilt.append(lang)
            partition_refs[lang] = {
                "partition_version": version,
                "bridge_resource_version": resource.resource_version,
                "checksum": content["checksum"],
                "snapshot_file": str(path.relative_to(store.root)),
            }
            committed_count += 1
            if crash_hook is not None:
                crash_hook(lang, committed_count)
            if simulate_crash_after is not None and \
                    committed_count >= simulate_crash_after:
                os._exit(134)  # hard kill: no cleanup, lock file left behind

        # 4. atomically publish the batch
        current = store.commit_batch(bridge_ref, partition_refs)
        report = {
            "batch": current["commit_seq"],
            "bridge": bridge_ref,
            "affected": sorted(affected),
            "reused": sorted(reused),
            "rebuilt": sorted(rebuilt),
            "partitions": partition_refs,
        }
        _write_atomic(store.root / "last_build_report.json",
                      canonical_text(report))
        return report

    # ------------------------------------------------------------------ query

    def _load_consistent_view(self):
        current = self.store.load_current()
        if current is None:
            raise XError(E_CONFIG_INVALID,
                         "no committed build batch in state directory",
                         field="state")
        bridge_content = self.store.load_snapshot(
            current["bridge"]["snapshot_file"], current["bridge"]["checksum"],
            "bridge", current["bridge"]["snapshot_version"])
        resource = BridgeResource.from_dict(bridge_content["resource"])
        partitions = {}
        for lang, ref in sorted(current["partitions"].items()):
            partitions[lang] = self.store.load_snapshot(
                ref["snapshot_file"], ref["checksum"], f"partition:{lang}",
                ref["partition_version"])
        return current, bridge_content, resource, partitions

    def run_query(self, query):
        """Execute one query against the currently committed view."""
        from .corpus import _validate_terms
        qid = query.get("query_id")
        top_k = query.get("top_k")
        if not isinstance(top_k, int) or top_k <= 0:
            raise XError(E_CONFIG_INVALID,
                         f"query {qid} has non-positive top_k {top_k!r}",
                         query_id=qid, field="top_k")
        _validate_terms(query.get("terms"), qid, "query_id")
        query_lang = query.get("query_lang")

        current, bridge_content, resource, partitions = \
            self._load_consistent_view()
        terms = [t["term"] for t in query["terms"]]
        expansion, degraded = resource.expand_query(
            query_lang, terms, partitions.keys())

        params = resource.params
        budget = params["max_scoring_entries"]
        policy = params["resource_policy"]
        doc_hits = {}
        used = 0
        dropped = 0
        for entry in expansion:
            partition = partitions[entry["tgt_lang"]]
            plist = partition["postings"].get(entry["candidate"])
            if not plist:
                continue
            if used + len(plist) > budget:
                if policy == "error":
                    raise XError(
                        E_RESOURCE_LIMIT,
                        f"scoring intermediate table would exceed "
                        f"{budget} entries",
                        limit="max_scoring_entries", query_id=qid)
                dropped += 1
                continue
            doc_freq = len(plist)
            for doc_id, tf in plist:
                raw = bm25_raw(tf, partition["doc_lens"][doc_id],
                               partition["avg_doc_len"],
                               partition["doc_count"], doc_freq,
                               params["k1"], params["b"])
                weighted = entry["confidence"] * raw
                doc_hits.setdefault(doc_id, []).append({
                    "query_term": entry["query_term"],
                    "candidate": entry["candidate"],
                    "confidence": entry["confidence"],
                    "raw_score": raw,
                    "weighted": weighted,
                })
            used += len(plist)
        if dropped:
            degraded.append({"category": "SCORING_CANDIDATES_DROPPED",
                             "dropped": dropped})

        doc_lang = {}
        for lang, partition in partitions.items():
            for doc_id in partition["doc_lens"]:
                doc_lang[doc_id] = lang
        ranked = rank(doc_hits, params["combine_rule"], top_k)
        results = [{
            "doc_id": doc_id,
            "lang": doc_lang[doc_id],
            "score": score,
            "details": details,
        } for doc_id, score, details in ranked]
        return {
            "query_id": qid,
            "query_lang": query_lang,
            "top_k": top_k,
            "bridge": {
                "snapshot_version": current["bridge"]["snapshot_version"],
                "resource_version": current["bridge"]["resource_version"],
                "checksum": current["bridge"]["checksum"],
            },
            "commit_seq": current["commit_seq"],
            "expansion": expansion,
            "degraded": degraded,
            "results": results,
        }

    def run_query_file(self, queries_path, out_path=None, workers=1):
        queries = load_queries(queries_path)
        if workers and workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(self.run_query, queries))
        else:
            results = [self.run_query(q) for q in queries]
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                for result in results:
                    fh.write(canonical_text(result) + "\n")
        return results
