"""Bridge resource model: direction table, term bridge table, scoring weights.

A bridge resource is a single JSON document with three sub-tables, each
carrying its own version and content checksum. The loader verifies every
checksum and validates the content against the business rules.
"""

import json

from .canon import checksum
from .errors import (
    E_BRIDGE_LANG,
    E_CONFIDENCE_RANGE,
    E_CONFIG_INVALID,
    XError,
)

VALID_COMBINE_RULES = ("weighted_sum",)
VALID_RESOURCE_POLICIES = ("error", "degrade")

WEIGHT_DEFAULTS = {
    "k1": 1.2,
    "b": 0.75,
    "combine_rule": "weighted_sum",
    "max_candidates_per_term": 3,
    "min_confidence": 0.0,
    "max_expansion_candidates": 256,
    "max_postings_per_partition": 1_000_000,
    "max_scoring_entries": 250_000,
    "resource_policy": "degrade",
}


def _table_checksum(entries):
    return checksum(entries)


def build_table(version, entries):
    return {"version": version, "checksum": _table_checksum(entries), "entries": entries}


def build_weights(version, params):
    merged = dict(WEIGHT_DEFAULTS)
    merged.update(params)
    return {"version": version, "checksum": checksum(merged), "params": merged}


class BridgeResource:
    """Validated, immutable view of a bridge resource."""

    def __init__(self, resource_id, resource_version, languages,
                 direction_table, term_bridge, scoring_weights):
        self.resource_id = resource_id
        self.resource_version = resource_version
        self.languages = tuple(sorted(languages))
        self.direction_table = direction_table
        self.term_bridge = term_bridge
        self.scoring_weights = scoring_weights
        self.params = dict(scoring_weights["params"])
        self._direction_index = {}
        for entry in direction_table["entries"]:
            self._direction_index[(entry["src_lang"], entry["tgt_lang"])] = entry
        self._term_index = {}
        for entry in term_bridge["entries"]:
            key = (entry["src_lang"], entry["src_term"], entry["tgt_lang"])
            self._term_index[key] = entry["candidates"]

    # -- loading / validation -------------------------------------------------

    @classmethod
    def from_dict(cls, data):
        for field in ("resource_id", "resource_version", "languages",
                      "direction_table", "term_bridge", "scoring_weights"):
            if field not in data:
                raise XError(E_CONFIG_INVALID,
                             f"bridge resource missing field {field!r}",
                             field=field)
        languages = sorted(data["languages"])
        if not languages:
            raise XError(E_CONFIG_INVALID, "bridge resource has no languages",
                         field="languages")
        lang_set = set(languages)

        direction = cls._check_table(data["direction_table"], "direction_table")
        term_bridge = cls._check_table(data["term_bridge"], "term_bridge")
        weights = data["scoring_weights"]
        if "params" not in weights or "version" not in weights or "checksum" not in weights:
            raise XError(E_CONFIG_INVALID,
                         "scoring_weights requires version/checksum/params",
                         field="scoring_weights")
        params = dict(WEIGHT_DEFAULTS)
        params.update(weights["params"])
        if checksum(params) != weights["checksum"]:
            raise XError(E_CONFIG_INVALID,
                         "scoring_weights checksum mismatch",
                         field="scoring_weights.checksum")
        weights = {"version": weights["version"], "checksum": weights["checksum"],
                   "params": params}
        cls._validate_weights(params)

        for entry in direction["entries"]:
            for field in ("src_lang", "tgt_lang"):
                if entry.get(field) not in lang_set:
                    raise XError(E_BRIDGE_LANG,
                                 f"direction entry references unknown language "
                                 f"{entry.get(field)!r}",
                                 entry=entry, field=field)

        for entry in term_bridge["entries"]:
            for field in ("src_lang", "tgt_lang"):
                if entry.get(field) not in lang_set:
                    raise XError(E_BRIDGE_LANG,
                                 f"term bridge entry points at unknown language "
                                 f"{entry.get(field)!r}",
                                 entry={"src_lang": entry.get("src_lang"),
                                        "src_term": entry.get("src_term"),
                                        "tgt_lang": entry.get("tgt_lang")},
                                 field=field)
            if not entry.get("src_term"):
                raise XError(E_CONFIG_INVALID,
                             "term bridge entry has empty src_term",
                             entry=entry, field="src_term")
            for cand in entry.get("candidates", []):
                conf = cand.get("confidence")
                if not isinstance(conf, (int, float)) or not (0.0 < conf <= 1.0):
                    raise XError(E_CONFIDENCE_RANGE,
                                 f"confidence {conf!r} out of range (0, 1]",
                                 entry={"src_lang": entry.get("src_lang"),
                                        "src_term": entry.get("src_term"),
                                        "tgt_lang": entry.get("tgt_lang"),
                                        "candidate": cand.get("term")},
                                 field="confidence")
                if not cand.get("term"):
                    raise XError(E_CONFIG_INVALID,
                                 "bridge candidate has empty term",
                                 entry=entry, field="candidates.term")

        return cls(data["resource_id"], data["resource_version"], languages,
                   direction, term_bridge, weights)

    @classmethod
    def _check_table(cls, table, name):
        if not isinstance(table, dict) or "entries" not in table:
            raise XError(E_CONFIG_INVALID, f"{name} missing entries",
                         field=f"{name}.entries")
        if checksum(table["entries"]) != table.get("checksum"):
            raise XError(E_CONFIG_INVALID, f"{name} checksum mismatch",
                         field=f"{name}.checksum")
        return table

    @staticmethod
    def _validate_weights(params):
        def bad(field, msg):
            raise XError(E_CONFIG_INVALID, msg, field=f"scoring_weights.{field}")

        if params["combine_rule"] not in VALID_COMBINE_RULES:
            bad("combine_rule",
                f"combine_rule {params['combine_rule']!r} not in {VALID_COMBINE_RULES}")
        if not (isinstance(params["k1"], (int, float)) and params["k1"] > 0):
            bad("k1", "k1 must be positive")
        if not (isinstance(params["b"], (int, float)) and 0.0 <= params["b"] <= 1.0):
            bad("b", "b must be in [0, 1]")
        for field in ("max_candidates_per_term", "max_expansion_candidates",
                      "max_postings_per_partition", "max_scoring_entries"):
            if not (isinstance(params[field], int) and params[field] > 0):
                bad(field, f"{field} must be a positive integer")
        if not (0.0 <= params["min_confidence"] <= 1.0):
            bad("min_confidence", "min_confidence must be in [0, 1]")
        if params["resource_policy"] not in VALID_RESOURCE_POLICIES:
            bad("resource_policy",
                f"resource_policy must be one of {VALID_RESOURCE_POLICIES}")

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def to_dict(self):
        return {
            "resource_id": self.resource_id,
            "resource_version": self.resource_version,
            "languages": list(self.languages),
            "direction_table": self.direction_table,
            "term_bridge": self.term_bridge,
            "scoring_weights": self.scoring_weights,
        }

    def content_checksum(self):
        return checksum(self.to_dict())

    # -- expansion ------------------------------------------------------------

    def direction_enabled(self, src_lang, tgt_lang):
        entry = self._direction_index.get((src_lang, tgt_lang))
        return bool(entry and entry.get("enabled"))

    def expand_term(self, src_lang, src_term, tgt_lang):
        """Deterministic candidate list for one term pair.

        Candidates are filtered by min_confidence, sorted by confidence
        descending then target term ascending, and truncated to
        max_candidates_per_term.
        """
        if src_lang == tgt_lang:
            return [{"candidate": src_term, "confidence": 1.0}]
        if not self.direction_enabled(src_lang, tgt_lang):
            return []
        raw = self._term_index.get((src_lang, src_term, tgt_lang), [])
        min_conf = self.params["min_confidence"]
        kept = [c for c in raw if c["confidence"] >= min_conf]
        kept.sort(key=lambda c: (-c["confidence"], c["term"]))
        kept = kept[: self.params["max_candidates_per_term"]]
        return [{"candidate": c["term"], "confidence": c["confidence"]} for c in kept]

    def expand_query(self, query_lang, query_terms, target_langs):
        """Expand a full query. Returns (entries, degraded).

        entries: deterministically ordered list of dicts with keys
        tgt_lang, query_term, candidate, confidence.
        degraded: list of degradation markers (may be empty).
        """
        entries = []
        for tgt_lang in sorted(target_langs):
            for term in query_terms:
                for cand in self.expand_term(query_lang, term, tgt_lang):
                    entries.append({
                        "tgt_lang": tgt_lang,
                        "query_term": term,
                        "candidate": cand["candidate"],
                        "confidence": cand["confidence"],
                    })
        entries.sort(key=lambda e: (e["tgt_lang"], e["query_term"],
                                    -e["confidence"], e["candidate"]))
        degraded = []
        limit = self.params["max_expansion_candidates"]
        if len(entries) > limit:
            dropped = len(entries) - limit
            if self.params["resource_policy"] == "error":
                from .errors import E_RESOURCE_LIMIT
                raise XError(E_RESOURCE_LIMIT,
                             f"expansion produced {len(entries)} candidates, "
                             f"limit is {limit}",
                             limit="max_expansion_candidates", value=len(entries))
            entries = entries[:limit]
            degraded.append({"category": "EXPANSION_TRUNCATED", "dropped": dropped})
        return entries, degraded

    # -- version diff ---------------------------------------------------------

    def affected_languages(self, other):
        """Partition languages whose rebuild is required when moving from
        resource ``self`` to resource ``other``.

        A partition language is affected when any direction-table entry or
        term-bridge entry involving that language as the target changed,
        was added, or was removed.
        """
        affected = set()

        def direction_key(entry):
            return (entry["src_lang"], entry["tgt_lang"])

        old_dir = {direction_key(e): e for e in self.direction_table["entries"]}
        new_dir = {direction_key(e): e for e in other.direction_table["entries"]}
        for key in set(old_dir) | set(new_dir):
            if old_dir.get(key) != new_dir.get(key):
                affected.add(key[1])

        def term_key(entry):
            return (entry["src_lang"], entry["src_term"], entry["tgt_lang"])

        old_term = {term_key(e): e for e in self.term_bridge["entries"]}
        new_term = {term_key(e): e for e in other.term_bridge["entries"]}
        for key in set(old_term) | set(new_term):
            if old_term.get(key) != new_term.get(key):
                affected.add(key[2])

        if self.scoring_weights != other.scoring_weights:
            # Scoring parameters are query-time only; they do not invalidate
            # partition content, so no partition is marked here.
            pass
        return affected
