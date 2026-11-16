"""Char-level features, BIESO Viterbi decoding and entity post-validation.

Everything in this module is deterministic: no randomness, ties are broken
by label lexicographic order, and floating point results are rounded to 6
decimals at well-defined boundaries.
"""

import hashlib
import math

import numpy as np


def prepare_model(model):
    """Attach numpy-backed, sorted-label views used by the decoder."""
    labels = sorted(model["labels"])
    matrix = np.array([model["emission"][lab] for lab in labels],
                      dtype=np.float64)
    model["_labels_sorted"] = labels
    model["_emission_matrix"] = matrix
    return model


def _bucket(key, buckets):
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % buckets


def _dict_coverage(text, n, lexicon):
    """Longest dict entry covering each char position."""
    cover = [None] * n
    for entry in lexicon:
        word = entry["text"]
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            for j in range(idx, idx + len(word)):
                if cover[j] is None or len(word) > len(cover[j][0]):
                    cover[j] = (word, j - idx)
            start = idx + 1
    return cover


def build_features(sent, lexicon, model):
    """Hash features from current/prev/next char and dict-match length/pos."""
    chars = sent["chars"]
    n = len(chars)
    dim = model["feature_dim"]
    buckets = model["hash_buckets"]
    embedding = model["char_embedding"]
    cover = _dict_coverage("".join(chars), n, lexicon)
    feats = []
    for i, ch in enumerate(chars):
        if ord(ch) < 32:
            raise ValueError("control character at position %d" % i)
        feats_i = []
        for kind, value in (
            ("cur", ch),
            ("prev", chars[i - 1] if i > 0 else "<BOS>"),
            ("next", chars[i + 1] if i < n - 1 else "<EOS>"),
        ):
            vec = embedding.get(value)
            weight = (sum(vec) / dim) if vec else 0.0
            feats_i.append((_bucket("%s=%s" % (kind, value), buckets), weight))
        if cover[i] is not None:
            word, offset = cover[i]
            length = len(word)
            if length == 1:
                pos_tag = "S"
            elif offset == 0:
                pos_tag = "B"
            elif offset == length - 1:
                pos_tag = "E"
            else:
                pos_tag = "I"
            feats_i.append((_bucket("dlen=%d" % length, buckets), 1.0))
            feats_i.append((_bucket("dpos=%s" % pos_tag, buckets), 1.0))
        else:
            feats_i.append((_bucket("dlen=0", buckets), 1.0))
        feats.append(feats_i)
    return feats


def viterbi(feats, model):
    """Log-domain BIESO Viterbi. Ties resolved by label lexicographic order."""
    labels = model["_labels_sorted"]
    matrix = model["_emission_matrix"]
    transitions = model["transitions"]
    n = len(feats)
    candidates = []
    for feats_i in feats:
        idx = np.array([b for b, _ in feats_i], dtype=np.int64)
        weights = np.array([w for _, w in feats_i], dtype=np.float64)
        scores = matrix[:, idx] @ weights
        candidates.append(
            {lab: round(float(scores[k]), 6) for k, lab in enumerate(labels)}
        )
    start = transitions.get("<START>", {})
    prev = {lab: start.get(lab, 0.0) + candidates[0][lab] for lab in labels}
    backpointers = []
    for i in range(1, n):
        curr = {}
        back = {}
        for lab in labels:
            best = None
            best_prev = None
            for prev_lab in labels:
                value = prev[prev_lab] + transitions.get(prev_lab, {}).get(lab, 0.0)
                if best is None or value > best:
                    best = value
                    best_prev = prev_lab
            curr[lab] = best + candidates[i][lab]
            back[lab] = best_prev
        backpointers.append(back)
        prev = curr
    best = None
    last = None
    for lab in labels:
        if best is None or prev[lab] > best:
            best = prev[lab]
            last = lab
    sequence = [last]
    for back in reversed(backpointers):
        sequence.append(back[sequence[-1]])
    sequence.reverse()
    return sequence, round(best, 6), candidates


def _model_entities(labels_seq):
    """Cut entity spans from a BIESO label sequence."""
    entities = []
    i = 0
    n = len(labels_seq)
    while i < n:
        tag = labels_seq[i]
        if tag == "O":
            i += 1
            continue
        prefix, _, etype = tag.partition("-")
        if prefix == "S":
            entities.append((i, i, etype))
            i += 1
        elif prefix == "B":
            j = i
            while j + 1 < n and labels_seq[j + 1] in ("I-" + etype, "E-" + etype):
                j += 1
            entities.append((i, j, etype))
            i = j + 1
        else:  # stray I-/E- starts a fresh span deterministically
            entities.append((i, i, etype))
            i += 1
    return entities


def _dict_matches(text, lexicon):
    matches = []
    for entry in lexicon:
        word = entry["text"]
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            matches.append({
                "start": idx,
                "end": idx + len(word) - 1,
                "type": entry["type"],
                "freq": entry["freq"],
                "text": word,
            })
            start = idx + 1
    # conflict resolution: higher freq, then longer, then leftmost, then text
    matches.sort(key=lambda m: (-m["freq"], -(m["end"] - m["start"]),
                                m["start"], m["text"]))
    chosen = []
    used = [False] * len(text)
    for match in matches:
        if any(used[match["start"]:match["end"] + 1]):
            continue
        for k in range(match["start"], match["end"] + 1):
            used[k] = True
        chosen.append(match)
    return chosen


def extract_entities(sent, labels_seq, candidates, lexicon):
    """Model entities + dict longest-match boundary/type override."""
    chars = sent["chars"]
    text = "".join(chars)
    model_ents = _model_entities(labels_seq)
    chosen = _dict_matches(text, lexicon)
    corrections = []
    final = []
    for match in chosen:
        overlapped = [me for me in model_ents
                      if not (me[1] < match["start"] or me[0] > match["end"])]
        for me in overlapped:
            if (me[0], me[1], me[2]) != (match["start"], match["end"], match["type"]):
                corrections.append({
                    "sent_id": sent["sent_id"],
                    "model_span": [me[0], me[1]],
                    "model_type": me[2],
                    "dict_span": [match["start"], match["end"]],
                    "dict_type": match["type"],
                    "dict_text": match["text"],
                })
        final.append({
            "start": match["start"],
            "end": match["end"],
            "type": match["type"],
            "source": "dict",
            "text": match["text"],
        })
        model_ents = [me for me in model_ents if me not in overlapped]
    for me in model_ents:
        final.append({
            "start": me[0],
            "end": me[1],
            "type": me[2],
            "source": "model",
            "text": "".join(chars[me[0]:me[1] + 1]),
        })
    for ent in final:
        span_scores = [candidates[p][labels_seq[p]]
                       for p in range(ent["start"], ent["end"] + 1)]
        mean_score = sum(span_scores) / len(span_scores)
        ent["confidence"] = round(1.0 / (1.0 + math.exp(-mean_score)), 6)
    seen = set()
    unique = []
    for ent in sorted(final, key=lambda e: (e["start"], e["end"], e["type"])):
        key = (ent["start"], ent["end"], ent["type"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(ent)
    corrections.sort(key=lambda c: (c["dict_span"][0], c["dict_span"][1],
                                    c["dict_text"]))
    return unique, corrections
