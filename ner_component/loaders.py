"""Loading and validation of corpus, model, lexicon, config and edits."""

import json
import os

from .errors import (
    E_CORPUS_INVALID,
    E_DICT_INVALID,
    E_MODEL_SHAPE_MISMATCH,
    failure,
)

ENTITY_TYPES = ("PER", "LOC", "ORG")
REQUIRED_LABELS = {"O"} | {
    "%s-%s" % (p, t) for t in ENTITY_TYPES for p in ("B", "I", "E", "S")
}
VALID_ENTRY_TYPES = set(ENTITY_TYPES)

DEFAULT_CONFIG = {
    "concurrency": 4,
    "max_edit_batch_size": 2,
    "dict_change_mode": "incremental",
    "invalidation_metrics": ["entry_fp", "model_fp", "sent_fp"],
    "snapshot_dir": "snapshots",
    "version_policy": {"strategy": "resume"},
    "batch_sleep_ms": 0,
}


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_corpus(path):
    """Parse corpus JSONL. Returns (sentences, failures)."""
    sents = []
    failures = []
    seen = set()
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                sid = obj["sent_id"]
                if not isinstance(sid, str) or not sid:
                    raise ValueError("sent_id must be a non-empty string")
                if sid in seen:
                    raise ValueError("duplicate sent_id: %s" % sid)
                chars = obj["chars"].split(" ")
                if not chars or any(c == "" for c in chars):
                    raise ValueError("char sequence is empty")
                positions = obj["positions"]
                if positions != list(range(len(chars))):
                    raise ValueError("positions are not continuous from 0")
                seen.add(sid)
                sents.append({"sent_id": sid, "chars": chars, "positions": positions})
            except Exception as exc:
                failures.append(
                    failure("line:%d" % lineno, "load_corpus", E_CORPUS_INVALID,
                            type(exc).__name__, exc)
                )
    sents.sort(key=lambda s: s["sent_id"])
    return sents, failures


def validate_model(model):
    dim = model["feature_dim"]
    buckets = model["hash_buckets"]
    if not isinstance(dim, int) or dim <= 0:
        raise ValueError("feature_dim must be a positive int")
    if not isinstance(buckets, int) or buckets <= 0:
        raise ValueError("hash_buckets must be a positive int")
    labels = model["labels"]
    missing = REQUIRED_LABELS - set(labels)
    if missing:
        raise ValueError("label set incomplete, missing: %s" % sorted(missing))
    for ch, vec in model["char_embedding"].items():
        if len(vec) != dim:
            raise ValueError("embedding dim mismatch for char %r" % ch)
    for lab, vec in model["emission"].items():
        if lab not in labels:
            raise ValueError("emission for unknown label %r" % lab)
        if len(vec) != buckets:
            raise ValueError("emission buckets mismatch for label %r" % lab)
    for src, row in model["transitions"].items():
        if src != "<START>" and src not in labels:
            raise ValueError("transition from unknown label %r" % src)
        for dst in row:
            if dst not in labels:
                raise ValueError("transition to unknown label %r" % dst)


def load_model(path):
    """Returns (model_or_None, failures)."""
    try:
        model = _read_json(path)
        validate_model(model)
        return model, []
    except Exception as exc:
        return None, [
            failure(os.path.basename(path), "load_model",
                    E_MODEL_SHAPE_MISMATCH, type(exc).__name__, exc)
        ]


def validate_entries(entries, source):
    """Returns (valid_entries, failures)."""
    valid = {}
    failures = []
    for idx, entry in enumerate(entries):
        try:
            text = entry["text"]
            etype = entry["type"]
            freq = entry["freq"]
            if not isinstance(text, str) or not text:
                raise ValueError("entry text must be a non-empty string")
            if etype not in VALID_ENTRY_TYPES:
                raise ValueError("illegal entry type: %r" % etype)
            if not isinstance(freq, (int, float)) or freq < 0:
                raise ValueError("entry freq must be a non-negative number")
            item = {"text": text, "type": etype, "freq": freq}
            if text not in valid or valid[text]["freq"] < freq:
                valid[text] = item
        except Exception as exc:
            failures.append(
                failure("%s#%d" % (source, idx), "load_dict", E_DICT_INVALID,
                        type(exc).__name__, exc)
            )
    return [valid[t] for t in sorted(valid)], failures


def load_dict(path):
    """Returns (entries, failures)."""
    try:
        obj = _read_json(path)
        entries = obj["entries"]
        if not isinstance(entries, list):
            raise ValueError("entries must be a list")
    except Exception as exc:
        return [], [
            failure(os.path.basename(path), "load_dict", E_DICT_INVALID,
                    type(exc).__name__, exc)
        ]
    return validate_entries(entries, os.path.basename(path))


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    try:
        obj = _read_json(path)
        if isinstance(obj, dict):
            cfg.update(obj)
    except FileNotFoundError:
        pass
    if not isinstance(cfg.get("concurrency"), int) or cfg["concurrency"] <= 0:
        cfg["concurrency"] = DEFAULT_CONFIG["concurrency"]
    if (not isinstance(cfg.get("max_edit_batch_size"), int)
            or cfg["max_edit_batch_size"] <= 0):
        cfg["max_edit_batch_size"] = DEFAULT_CONFIG["max_edit_batch_size"]
    if not isinstance(cfg.get("invalidation_metrics"), list):
        cfg["invalidation_metrics"] = list(DEFAULT_CONFIG["invalidation_metrics"])
    if not isinstance(cfg.get("version_policy"), dict):
        cfg["version_policy"] = dict(DEFAULT_CONFIG["version_policy"])
    cfg.setdefault("batch_sleep_ms", 0)
    return cfg


def load_edits(path):
    if not path or not os.path.exists(path):
        return []
    obj = _read_json(path)
    edits = obj.get("edits", [])
    return edits if isinstance(edits, list) else []
