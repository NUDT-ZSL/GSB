"""Alignment resources: language-pair map, translation table, score weights.

Every resource file is an envelope {"kind", "version", "checksum", "payload"}
where checksum = sha256 of the canonical payload bytes. Version changes cause
dependent stage snapshots to be replaced wholesale (never patched in place).
"""

import os

from .errors import E_MAP_FORMAT, E_SNAPSHOT_CORRUPT, PipelineError
from .textnorm import normalize_term
from .util import MICRO, hash_obj, read_json

KIND_LANG_PAIRS = "lang_pairs"
KIND_TRANSLATIONS = "translations"
KIND_WEIGHTS = "weights"

RESOURCE_FILES = {
    KIND_LANG_PAIRS: "lang_pairs.json",
    KIND_TRANSLATIONS: "translations.json",
    KIND_WEIGHTS: "weights.json",
}


def _load_envelope(path, kind):
    obj = read_json(path)
    version = obj.get("version")
    checksum = obj.get("checksum")
    payload = obj.get("payload")
    if version is None or checksum is None or payload is None:
        raise PipelineError(
            E_SNAPSHOT_CORRUPT, f"resource envelope incomplete: {path}",
            resource=kind, version=version,
        )
    if hash_obj(payload) != checksum:
        raise PipelineError(
            E_SNAPSHOT_CORRUPT,
            f"checksum mismatch for resource {kind} v{version}",
            resource=kind, version=version,
        )
    return version, payload


def build_envelope(kind, version, payload):
    return {
        "kind": kind,
        "version": version,
        "checksum": hash_obj(payload),
        "payload": payload,
    }


class ResourceBundle:
    def __init__(self, lang_pairs_version, lang_pairs, translations_version,
                 translations, conflicts, weights_version, weights):
        self.lang_pairs_version = lang_pairs_version
        self.lang_pairs = lang_pairs            # set of "src|dst"
        self.translations_version = translations_version
        self.translations = translations        # (src, dst, term) -> [candidates]
        self.conflicts = conflicts              # list of dicts, traceable
        self.weights_version = weights_version
        self.weights = weights                  # micro-unit weights + thresholds

    @classmethod
    def load(cls, resources_dir):
        lp_ver, lp_payload = _load_envelope(
            os.path.join(resources_dir, RESOURCE_FILES[KIND_LANG_PAIRS]),
            KIND_LANG_PAIRS)
        tr_ver, tr_payload = _load_envelope(
            os.path.join(resources_dir, RESOURCE_FILES[KIND_TRANSLATIONS]),
            KIND_TRANSLATIONS)
        w_ver, w_payload = _load_envelope(
            os.path.join(resources_dir, RESOURCE_FILES[KIND_WEIGHTS]),
            KIND_WEIGHTS)

        lang_pairs = set()
        for idx, entry in enumerate(lp_payload.get("pairs", [])):
            src, dst = entry.get("src"), entry.get("dst")
            if not isinstance(src, str) or not isinstance(dst, str):
                raise PipelineError(
                    E_MAP_FORMAT, f"lang_pairs entry {idx} malformed",
                    resource=KIND_LANG_PAIRS, version=lp_ver)
            lang_pairs.add(f"{src}|{dst}")

        translations = {}
        conflicts = []
        entries = tr_payload.get("entries", [])
        for idx, entry in enumerate(entries):
            src = entry.get("src")
            dst = entry.get("dst")
            term = entry.get("term")
            cands = entry.get("candidates")
            if (not isinstance(src, str) or not isinstance(dst, str)
                    or not isinstance(term, str)
                    or not isinstance(cands, list)
                    or not cands
                    or not all(isinstance(c, str) for c in cands)):
                raise PipelineError(
                    E_MAP_FORMAT,
                    f"translations entry {idx} malformed",
                    resource=KIND_TRANSLATIONS, version=tr_ver)
            norm_term = normalize_term(term)
            if norm_term is None:
                raise PipelineError(
                    E_MAP_FORMAT,
                    f"translations entry {idx} term not a single token",
                    resource=KIND_TRANSLATIONS, version=tr_ver)
            norm_cands = []
            for cand in cands:
                norm_cand = normalize_term(cand)
                if norm_cand is None:
                    raise PipelineError(
                        E_MAP_FORMAT,
                        f"translations entry {idx} candidate not a single token",
                        resource=KIND_TRANSLATIONS, version=tr_ver)
                if norm_cand not in norm_cands:
                    norm_cands.append(norm_cand)
            key = (src, dst, norm_term)
            if key in translations:
                if translations[key] != norm_cands:
                    conflicts.append({
                        "src": src, "dst": dst, "term": norm_term,
                        "kept": translations[key], "discarded": norm_cands,
                        "entry_index": idx,
                    })
                continue  # first entry wins, conflict stays traceable
            translations[key] = norm_cands

        weights = {
            "w_tfidf": int(w_payload["w_tfidf"]),
            "w_coverage": int(w_payload["w_coverage"]),
            "grade_thresholds": {
                str(k): int(v)
                for k, v in w_payload["grade_thresholds"].items()
            },
        }
        if weights["w_tfidf"] + weights["w_coverage"] != MICRO:
            raise PipelineError(
                E_MAP_FORMAT, "score weights must sum to 1000000 micro-units",
                resource=KIND_WEIGHTS, version=w_ver)

        return cls(lp_ver, lang_pairs, tr_ver, translations, conflicts,
                   w_ver, weights)

    def versions(self):
        return {
            KIND_LANG_PAIRS: self.lang_pairs_version,
            KIND_TRANSLATIONS: self.translations_version,
            KIND_WEIGHTS: self.weights_version,
        }
