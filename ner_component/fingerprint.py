"""Deterministic fingerprints for entries, model, sentences and entities."""

import hashlib
import json


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(obj):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def model_fingerprint(model):
    return fingerprint({
        "feature_dim": model["feature_dim"],
        "hash_buckets": model["hash_buckets"],
        "labels": sorted(model["labels"]),
        "char_embedding": model["char_embedding"],
        "transitions": model["transitions"],
        "emission": model["emission"],
    })


def entry_fingerprint(entry):
    return fingerprint({
        "text": entry["text"],
        "type": entry["type"],
        "freq": entry["freq"],
    })


def dict_fingerprint(entries):
    fps = [entry_fingerprint(e) for e in sorted(entries, key=lambda x: x["text"])]
    return fingerprint(fps)


def sent_fingerprint(sent):
    return fingerprint({
        "sent_id": sent["sent_id"],
        "chars": sent["chars"],
        "positions": sent["positions"],
    })


def entity_cache_key(entity, entry_fps, model_fp, sent_fp):
    return fingerprint({
        "entry_fps": sorted(entry_fps),
        "model_fp": model_fp,
        "sent_fp": sent_fp,
        "span": [entity["start"], entity["end"]],
        "type": entity["type"],
        "text": entity["text"],
    })
