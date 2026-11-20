"""Independent result verifier and cross-partition consistency self-check."""

import json
from math import fsum

from .bridge import BridgeResource
from .canon import canonical_bytes
from .corpus import load_documents, load_queries
from .engine import Engine
from .errors import E_INCONSISTENT, XError
from .index import build_partition
from .scoring import bm25_raw
from .store import StateStore


def _fail(category, message, **context):
    raise XError(E_INCONSISTENT, message, category=category, **context)


class Verifier:
    def __init__(self, state_dir):
        self.store = StateStore(state_dir)
        self.engine = Engine(state_dir)

    # -- partition reducibility --------------------------------------------

    def verify_partitions(self, docs_path):
        """Rebuild every committed partition from the source documents and
        require byte-identical content; also check the postings can be
        reduced back to the original per-document term frequencies."""
        current = self.store.load_current()
        docs = load_documents(docs_path)
        by_lang = {}
        for doc in docs:
            by_lang.setdefault(doc["doc_lang"], []).append(doc)
        checked = []
        for lang, ref in sorted(current["partitions"].items()):
            content = self.store.load_snapshot(
                ref["snapshot_file"], ref["checksum"], f"partition:{lang}",
                ref["partition_version"])
            expected = build_partition(
                lang, by_lang.get(lang, []), content["partition_version"],
                content["bridge_resource_version"],
                max_postings=10**12)
            if canonical_bytes(expected) != canonical_bytes(content):
                _fail("PARTITION_NOT_REDUCIBLE",
                      f"partition {lang} v{content['partition_version']} does "
                      f"not rebuild to identical content",
                      partition=lang, version=content["partition_version"])
            # reduce postings back to per-doc term frequencies
            reduced = {}
            for term, plist in content["postings"].items():
                for doc_id, tf in plist:
                    reduced.setdefault(doc_id, {})[term] = tf
            for doc in by_lang.get(lang, []):
                want = {t["term"]: t.get("tf", 1) for t in doc["terms"]}
                if reduced.get(doc["doc_id"], {}) != want:
                    _fail("POSTINGS_NOT_REDUCIBLE",
                          f"postings of partition {lang} do not reduce to "
                          f"doc {doc['doc_id']}",
                          partition=lang, doc_id=doc["doc_id"],
                          version=content["partition_version"])
            checked.append(lang)
        return {"check": "partitions", "ok": True, "partitions": checked}

    # -- query result recomputation ----------------------------------------

    def verify_results(self, queries_path, results_path):
        current, bridge_content, resource, partitions = \
            self.engine._load_consistent_view()
        queries = {q["query_id"]: q for q in load_queries(queries_path)}
        results = {}
        with open(results_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    results[row["query_id"]] = row
        if set(results) != set(queries):
            _fail("RESULT_SET_MISMATCH",
                  "result file does not cover exactly the query set",
                  version=current["commit_seq"])
        checked = 0
        for qid, result in sorted(results.items()):
            query = queries[qid]
            if result["bridge"]["resource_version"] != \
                    current["bridge"]["resource_version"]:
                _fail("BRIDGE_VERSION_MISMATCH",
                      f"query {qid} used bridge resource v"
                      f"{result['bridge']['resource_version']}, committed is v"
                      f"{current['bridge']['resource_version']}",
                      query_id=qid,
                      version=result["bridge"]["resource_version"])
            # expansion must be reproducible from the committed resource
            terms = [t["term"] for t in query["terms"]]
            expansion, _ = resource.expand_query(query["query_lang"], terms,
                                                 partitions.keys())
            if expansion != result["expansion"]:
                _fail("EXPANSION_NOT_REPRODUCIBLE",
                      f"query {qid} expansion differs from committed resource",
                      query_id=qid,
                      version=result["bridge"]["resource_version"])
            # every detail must recompute from partition statistics
            cand_df = {}
            for lang, partition in partitions.items():
                for term, plist in partition["postings"].items():
                    cand_df[(lang, term)] = (len(plist),
                                             {d: tf for d, tf in plist})
            doc_lang = {}
            for lang, partition in partitions.items():
                for doc_id in partition["doc_lens"]:
                    doc_lang[doc_id] = lang
            for hit in result["results"]:
                doc_id = hit["doc_id"]
                lang = doc_lang[doc_id]
                partition = partitions[lang]
                weighted_sum = []
                for detail in hit["details"]:
                    key = (lang, detail["candidate"])
                    if key not in cand_df:
                        _fail("DETAIL_NOT_IN_INDEX",
                              f"candidate {detail['candidate']!r} not in "
                              f"partition {lang}",
                              query_id=qid, partition=lang)
                    doc_freq, tf_map = cand_df[key]
                    raw = bm25_raw(tf_map[doc_id],
                                   partition["doc_lens"][doc_id],
                                   partition["avg_doc_len"],
                                   partition["doc_count"], doc_freq,
                                   resource.params["k1"],
                                   resource.params["b"])
                    if raw != detail["raw_score"]:
                        _fail("RAW_SCORE_MISMATCH",
                              f"detail raw_score mismatch for {doc_id}",
                              query_id=qid, doc_id=doc_id)
                    if detail["confidence"] * raw != detail["weighted"]:
                        _fail("WEIGHTED_MISMATCH",
                              f"weighted contribution mismatch for {doc_id}",
                              query_id=qid, doc_id=doc_id)
                    weighted_sum.append(detail["weighted"])
                if fsum(weighted_sum) != hit["score"]:
                    _fail("FINAL_SCORE_MISMATCH",
                          f"final score of {doc_id} does not recompute from "
                          f"details", query_id=qid, doc_id=doc_id)
            # top-k set and order must match an independent re-ranking
            fresh = self.engine.run_query(query)
            got = [(h["doc_id"], h["score"]) for h in result["results"]]
            want = [(h["doc_id"], h["score"]) for h in fresh["results"]]
            if got != want:
                _fail("TOPK_ORDER_INVALID",
                      f"query {qid} top-k set/order differs from independent "
                      f"re-ranking", query_id=qid,
                      version=current["commit_seq"])
            if len(result["results"]) > query["top_k"]:
                _fail("TOPK_ORDER_INVALID",
                      f"query {qid} returned more than top_k results",
                      query_id=qid)
            scores = [h["score"] for h in result["results"]]
            ids = [h["doc_id"] for h in result["results"]]
            if sorted(zip(scores, ids), key=lambda x: (-x[0], x[1])) != \
                    list(zip(scores, ids)):
                _fail("TOPK_ORDER_INVALID",
                      f"query {qid} results not ordered by (-score, doc_id)",
                      query_id=qid)
            checked += 1
        return {"check": "results", "ok": True, "queries_checked": checked}

    # -- cross-partition consistency self-check -----------------------------

    def selfcheck(self, docs_path):
        current = self.store.load_current()
        commits = self.store.load_commits()
        batch_commits = [c for c in commits if c["kind"] == "batch"]
        if not batch_commits:
            _fail("COMMIT_RECORD_MISMATCH", "no batch commit record found")
        last_batch = batch_commits[-1]
        # 1. CURRENT must be cross-checkable against the batch commit record
        if last_batch["bridge"] != current["bridge"] or \
                last_batch["partitions"] != current["partitions"]:
            _fail("COMMIT_RECORD_MISMATCH",
                  "CURRENT.json does not match the latest batch commit record",
                  version=last_batch["commit_seq"])
        # 2. every reused partition must be legitimately reusable: its
        #    recorded bridge resource version must not differ from the
        #    committed one in any entry involving its language
        bridge_content = self.store.load_snapshot(
            current["bridge"]["snapshot_file"], current["bridge"]["checksum"],
            "bridge", current["bridge"]["snapshot_version"])
        resource = BridgeResource.from_dict(bridge_content["resource"])
        bridge_commits = {}
        for record in commits:
            if record["kind"] == "bridge":
                bridge_commits[record["resource_version"]] = record
        for lang, ref in sorted(current["partitions"].items()):
            old_version = ref["bridge_resource_version"]
            if old_version == resource.resource_version:
                continue
            old_record = bridge_commits.get(old_version)
            if old_record is None:
                _fail("REBUILD_SET_MISMATCH",
                      f"partition {lang} references unknown bridge resource "
                      f"v{old_version}",
                      partition=lang, version=old_version)
            old_content = self.store.load_snapshot(
                old_record["snapshot_file"], old_record["checksum"], "bridge",
                old_record["snapshot_version"])
            old_resource = BridgeResource.from_dict(old_content["resource"])
            if lang in old_resource.affected_languages(resource):
                _fail("REBUILD_SET_MISMATCH",
                      f"partition {lang} was reused although affected by the "
                      f"resource diff v{old_version} -> "
                      f"v{resource.resource_version}",
                      partition=lang, version=resource.resource_version)
        # 3. build report: marked-rebuild set must equal actual rebuilt set
        report_path = self.store.root / "last_build_report.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if sorted(report["affected"]) != sorted(report["rebuilt"]):
                _fail("REBUILD_SET_MISMATCH",
                      "marked-rebuild set differs from actually rebuilt set",
                      version=report["batch"])
            overlap = set(report["reused"]) & set(report["rebuilt"])
            if overlap:
                _fail("REBUILD_SET_MISMATCH",
                      f"partitions both reused and rebuilt: {sorted(overlap)}",
                      version=report["batch"])
        # 4. no doc_id may appear in two partitions
        docs = load_documents(docs_path)
        owner = {}
        for doc in docs:
            if doc["doc_id"] in owner:
                _fail("DUP_DOC_ACROSS_PARTITIONS",
                      f"doc {doc['doc_id']} appears in partitions "
                      f"{owner[doc['doc_id']]} and {doc['doc_lang']}",
                      doc_id=doc["doc_id"])
            owner[doc["doc_id"]] = doc["doc_lang"]
        seen = {}
        for lang, ref in sorted(current["partitions"].items()):
            content = self.store.load_snapshot(
                ref["snapshot_file"], ref["checksum"], f"partition:{lang}",
                ref["partition_version"])
            for doc_id in content["doc_lens"]:
                if doc_id in seen:
                    _fail("DUP_DOC_ACROSS_PARTITIONS",
                          f"doc {doc_id} in partitions {seen[doc_id]} and "
                          f"{lang}", doc_id=doc_id, partition=lang)
                seen[doc_id] = lang
        return {"check": "selfcheck", "ok": True,
                "commit_seq": current["commit_seq"],
                "bridge_resource_version": resource.resource_version}
