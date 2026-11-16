"""Full / incremental NER engine with precise dependency invalidation."""

import json
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor

from .core import build_features, extract_entities, prepare_model, viterbi
from .errors import (
    E_CONFIG_MISMATCH,
    E_CORPUS_INVALID,
    failure,
)
from .fingerprint import (
    dict_fingerprint,
    entity_cache_key,
    entry_fingerprint,
    fingerprint,
    model_fingerprint,
    sent_fingerprint,
)
from .loaders import (
    load_config,
    load_corpus,
    load_dict,
    load_edits,
    load_model,
    validate_entries,
)
from .snapshot import SnapshotStore


def sort_key(sent_id):
    return sent_id


def decode_sentence(sent, model, lexicon, model_fp):
    """Decode one sentence; never raises, returns (result, failure)."""
    stage = "feature"
    try:
        feats = build_features(sent, lexicon, model)
        stage = "decode"
        labels, path_score, candidates = viterbi(feats, model)
        stage = "post_validate"
        entities, corrections = extract_entities(sent, labels, candidates, lexicon)
    except Exception as exc:
        return None, failure(sent["sent_id"], stage, E_CORPUS_INVALID,
                             type(exc).__name__, exc)
    joined = "".join(sent["chars"])
    entry_fps_map = {e["text"]: entry_fingerprint(e) for e in lexicon}
    hit_entries = sorted(t for t in entry_fps_map if t in joined)
    hit_fps = [entry_fps_map[t] for t in hit_entries]
    s_fp = sent_fingerprint(sent)
    for ent in entities:
        ent["cache_key"] = entity_cache_key(ent, hit_fps, model_fp, s_fp)
    result = {
        "sent_id": sent["sent_id"],
        "labels": labels,
        "path_score": path_score,
        "candidates": candidates,
        "entities": entities,
        "corrections": corrections,
        "hit_entries": hit_entries,
        "model_hash": model_fp,
    }
    return result, None


def decode_sentences(sents, model, lexicon, concurrency, model_fp):
    """Concurrent decode with deterministic, sent_id-ordered output."""
    ordered = sorted(sents, key=lambda s: sort_key(s["sent_id"]))
    results = {}
    failures = []

    def work(sent):
        return decode_sentence(sent, model, lexicon, model_fp)

    if concurrency > 1 and len(ordered) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            outputs = list(pool.map(work, ordered))
    else:
        outputs = [work(s) for s in ordered]
    for (result, fail), sent in zip(outputs, ordered):
        if fail is not None:
            failures.append(fail)
        else:
            results[sent["sent_id"]] = result
    return results, failures


def _diff_texts(old_entries, new_entries):
    old_map = {e["text"]: entry_fingerprint(e) for e in old_entries}
    new_map = {e["text"]: entry_fingerprint(e) for e in new_entries}
    return {t for t in set(old_map) | set(new_map)
            if old_map.get(t) != new_map.get(t)}


def apply_edit(model, lexicon, edit, sample_dir):
    """Apply one edit; invalid edits are recorded and skipped.

    Returns (model, lexicon, changed_texts, model_changed, failures).
    """
    op = edit.get("op")
    failures = []
    if op == "add":
        valid, fails = validate_entries([edit.get("entry", {})], "edit:add")
        failures.extend(fails)
        if valid:
            new_lex = [e for e in lexicon if e["text"] != valid[0]["text"]]
            new_lex = sorted(new_lex + valid, key=lambda e: e["text"])
            changed = _diff_texts(lexicon, new_lex)
            return model, new_lex, changed, False, failures
        return model, lexicon, set(), False, failures
    if op == "delete":
        text = edit.get("text", "")
        new_lex = [e for e in lexicon if e["text"] != text]
        changed = _diff_texts(lexicon, new_lex)
        return model, new_lex, changed, False, failures
    if op == "replace":
        path = edit.get("entries_path", "")
        if not os.path.isabs(path):
            path = os.path.join(sample_dir, path)
        new_lex, fails = load_dict(path)
        failures.extend(fails)
        changed = _diff_texts(lexicon, new_lex)
        return model, new_lex, changed, False, failures
    if op == "model":
        path = edit.get("model_path", "")
        if not os.path.isabs(path):
            path = os.path.join(sample_dir, path)
        new_model, fails = load_model(path)
        failures.extend(fails)
        if new_model is not None:
            return prepare_model(new_model), lexicon, set(), True, failures
        return model, lexicon, set(), False, failures
    failures.append(failure("edit", "apply_edit", E_CORPUS_INVALID,
                            "UnknownOp", "unknown edit op: %r" % (op,)))
    return model, lexicon, set(), False, failures


def _chunked(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


class Engine:
    def __init__(self, sample_dir, report_path, snapshot_dir=None,
                 concurrency=None, edits_name="edits.json",
                 batch_sleep_ms=None):
        self.sample_dir = sample_dir
        self.report_path = report_path
        self.cfg = load_config(os.path.join(sample_dir, "config.json"))
        if snapshot_dir:
            self.cfg["snapshot_dir"] = snapshot_dir
        if concurrency:
            self.cfg["concurrency"] = concurrency
        if batch_sleep_ms is not None:
            self.cfg["batch_sleep_ms"] = batch_sleep_ms
        self.edits_name = edits_name
        self.stop_requested = False
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, signum, frame):
        self.stop_requested = True

    def _path(self, name):
        return os.path.join(self.sample_dir, name)

    def _snapshot_dir(self):
        snap = self.cfg["snapshot_dir"]
        return snap if os.path.isabs(snap) else self._path(snap)

    def load_inputs(self):
        corpus, failures = load_corpus(self._path("corpus.jsonl"))
        model, model_failures = load_model(self._path("model.json"))
        failures.extend(model_failures)
        if model is not None:
            model = prepare_model(model)
        lexicon, dict_failures = load_dict(self._path("dict.json"))
        failures.extend(dict_failures)
        edits = load_edits(self._path(self.edits_name))
        return corpus, model, lexicon, edits, failures

    def _manifest(self, version, base_m, base_d, model, lexicon, applied,
                  results, failed, failures):
        return {
            "global_version": version,
            "base_model_hash": base_m,
            "base_dict_hash": base_d,
            "model_hash": model_fingerprint(model) if model else "",
            "dict_hash": dict_fingerprint(lexicon),
            "applied_edits": applied,
            "completed_sents": sorted(results),
            "failed_sents": sorted(failed),
            "sent_versions": {sid: version for sid in sorted(results)},
            "failures": failures,
        }

    def _write_report(self, version, model, lexicon, results, failures,
                      recomputed, reused):
        sentences = [results[sid] for sid in sorted(results)]
        corrections = []
        for sent in sentences:
            corrections.extend(sent["corrections"])
        corrections.sort(key=lambda c: (c["sent_id"], c["dict_span"][0],
                                        c["dict_span"][1], c["dict_text"]))
        report = {
            "version": version,
            "config_hash": fingerprint(self.cfg),
            "model_hash": model_fingerprint(model) if model else "",
            "dict_hash": dict_fingerprint(lexicon),
            "sentences": sentences,
            "corrections": corrections,
            "stats": {
                "recomputed": len(recomputed),
                "reused": len(reused),
                "recomputed_sents": sorted(recomputed),
                "reused_sents": sorted(reused),
            },
            "failures": failures,
        }
        tmp = self.report_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, sort_keys=True, indent=2)
        os.replace(tmp, self.report_path)

    def run_full(self):
        """One-shot full run over the final (post-edit) parameters."""
        corpus, model, lexicon, edits, failures = self.load_inputs()
        store = SnapshotStore(self._snapshot_dir())
        store.clear()
        base_m = model_fingerprint(model) if model is not None else ""
        base_d = dict_fingerprint(lexicon)
        for edit in edits:
            model, lexicon, _, _, edit_failures = apply_edit(
                model, lexicon, edit, self.sample_dir)
            failures.extend(edit_failures)
        results = {}
        failed = {}
        recomputed = []
        if model is not None:
            model_fp = model_fingerprint(model)
            results, decode_failures = decode_sentences(
                corpus, model, lexicon, self.cfg["concurrency"], model_fp)
            for f in decode_failures:
                failed[f["id"]] = f
            failures.extend(decode_failures)
            recomputed = sorted(results)
        version = 1
        manifest = self._manifest(version, base_m, base_d, model, lexicon,
                                  len(edits), results, failed, failures)
        store.write(manifest, results)
        self._write_report(version, model, lexicon, results, failures,
                           recomputed, [])
        return 0

    def run_incremental(self):
        corpus, model, lexicon, edits, load_failures = self.load_inputs()
        store = SnapshotStore(self._snapshot_dir())
        base_model, base_lexicon = model, lexicon
        base_m = model_fingerprint(model) if model is not None else ""
        base_d = dict_fingerprint(lexicon)
        strategy = self.cfg["version_policy"].get("strategy", "resume")

        state = store.load()
        results, failed, failures = {}, {}, list(load_failures)
        version, applied = 0, 0
        if state is not None:
            m = state["manifest"]
            hit = (strategy == "resume"
                   and m.get("base_model_hash") == base_m
                   and m.get("base_dict_hash") == base_d
                   and m.get("applied_edits", 0) <= len(edits))
            if hit:
                results = state["results"]
                failures = m.get("failures", list(load_failures))
                failed = {f["id"]: f for f in failures
                          if f.get("stage") in ("feature", "decode",
                                                "post_validate")}
                version = m.get("global_version", 0)
                applied = m.get("applied_edits", 0)
                for edit in edits[:applied]:
                    model, lexicon, _, _, _ = apply_edit(
                        model, lexicon, edit, self.sample_dir)
                # integrity: replayed params must match manifest hashes
                if (model_fingerprint(model) != m.get("model_hash")
                        or dict_fingerprint(lexicon) != m.get("dict_hash")):
                    hit = False
            if not hit:
                store.clear()
                failures = list(load_failures)
                failures.append(failure(
                    "snapshot", "recover", E_CONFIG_MISMATCH,
                    "SnapshotMismatch",
                    "manifest version/model/dict hash mismatch"))
                results, failed, version, applied = {}, {}, 0, 0
                model, lexicon = base_model, base_lexicon

        joined = {s["sent_id"]: "".join(s["chars"]) for s in corpus}
        metrics = set(self.cfg["invalidation_metrics"])
        recomputed_all = set()

        def recompute(todo_sents):
            nonlocal failures
            if model is None or not todo_sents:
                return []
            model_fp = model_fingerprint(model)
            new_results, decode_failures = decode_sentences(
                todo_sents, model, lexicon, self.cfg["concurrency"], model_fp)
            done = []
            for sid, res in new_results.items():
                results[sid] = res
                done.append(sid)
            for f in decode_failures:
                failed[f["id"]] = f
                failures.append(f)
                results.pop(f["id"], None)
            return done

        # initial pass: compute whatever has no cached result yet
        todo = [s for s in corpus
                if s["sent_id"] not in results and s["sent_id"] not in failed]
        if todo:
            recomputed_all.update(recompute(todo))
            version += 1
            manifest = self._manifest(version, base_m, base_d, model, lexicon,
                                      applied, results, failed, failures)
            store.write(manifest, results)
            if self.stop_requested:
                self._write_report(version, model, lexicon, results, failures,
                                   sorted(recomputed_all), [])
                return 0

        for batch in _chunked(edits[applied:], self.cfg["max_edit_batch_size"]):
            changed_texts = set()
            model_changed = False
            for edit in batch:
                model, lexicon, ctexts, mchanged, edit_failures = apply_edit(
                    model, lexicon, edit, self.sample_dir)
                changed_texts |= ctexts
                model_changed = model_changed or mchanged
                failures.extend(edit_failures)
            affected = set()
            if model_changed and "model_fp" in metrics:
                affected = set(joined)
            elif changed_texts and "entry_fp" in metrics:
                affected = {sid for sid, text in joined.items()
                            if any(t in text for t in changed_texts)}
            affected -= set(failed)
            todo = [s for s in corpus if s["sent_id"] in affected]
            recomputed_all.update(recompute(todo))
            applied += len(batch)
            version += 1
            manifest = self._manifest(version, base_m, base_d, model, lexicon,
                                      applied, results, failed, failures)
            store.write(manifest, results)
            if self.cfg.get("batch_sleep_ms"):
                time.sleep(self.cfg["batch_sleep_ms"] / 1000.0)
            if self.stop_requested:
                break

        reused_all = [sid for sid in sorted(results)
                      if sid not in recomputed_all]
        self._write_report(version, model, lexicon, results, failures,
                           sorted(recomputed_all), reused_all)
        return 0
