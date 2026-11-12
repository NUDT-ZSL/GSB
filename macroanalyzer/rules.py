"""Vulnerability rule set loading, matching and confidence scoring.

A rule set is a local JSON file:
{
  "ruleset_id": "name",
  "rules": [
    {"id": "R1", "name": "...", "severity": "high",
     "weight": 0.6, "features": ["auto_exec", "process_create"]}
  ]
}
A rule matches when every feature it lists is present in the document.
Confidence = sum(matched weights) / sum(all weights), clamped to [0, 1].
A malformed rule set never crashes analysis: the error is reported and the
built-in default rule set is used instead.
"""

import hashlib
import json

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

BUILTIN_RULESET_ID = "builtin"

BUILTIN_RULESET = {
    "ruleset_id": BUILTIN_RULESET_ID,
    "version": "1.0",
    "rules": [
        {"id": "R-AUTOEXEC-PROC", "name": "Auto-exec entry with process creation",
         "severity": "high", "weight": 0.6,
         "features": ["auto_exec", "process_create"]},
        {"id": "R-DOWNLOAD-DROP", "name": "Network download with file write",
         "severity": "critical", "weight": 0.8,
         "features": ["network_download", "file_write"]},
        {"id": "R-AUTOEXEC-DROP", "name": "Auto-exec entry with file write",
         "severity": "medium", "weight": 0.5,
         "features": ["auto_exec", "file_write"]},
        {"id": "R-OBFUSC-CONCAT", "name": "String concatenation obfuscation",
         "severity": "medium", "weight": 0.4,
         "features": ["obfuscation_string_concat"]},
        {"id": "R-AUTOEXEC", "name": "Automatic execution entry point",
         "severity": "low", "weight": 0.2,
         "features": ["auto_exec"]},
    ],
}


class RulesetError(Exception):
    pass


def _validate(data):
    if not isinstance(data, dict):
        raise RulesetError("ruleset root must be a JSON object")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RulesetError("ruleset must contain a non-empty 'rules' array")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise RulesetError("rule #%d is not an object" % i)
        if not isinstance(rule.get("id"), str) or not rule["id"]:
            raise RulesetError("rule #%d missing string 'id'" % i)
        feats = rule.get("features")
        if not isinstance(feats, list) or not all(
                isinstance(f, str) for f in feats):
            raise RulesetError("rule %s: 'features' must be a string array"
                               % rule.get("id"))
        weight = rule.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) \
                or weight <= 0:
            raise RulesetError("rule %s: 'weight' must be a positive number"
                               % rule.get("id"))
        sev = rule.get("severity", "info")
        if sev not in SEVERITY_ORDER:
            raise RulesetError("rule %s: unknown severity %r"
                               % (rule.get("id"), sev))
        rule.setdefault("severity", "info")
        rule.setdefault("name", rule["id"])
    data.setdefault("ruleset_id", "unnamed")
    return data


def load_ruleset(path):
    """Return (ruleset, error). On any failure returns the builtin ruleset
    plus a human-readable error string."""
    if path is None:
        return BUILTIN_RULESET, None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return _validate(data), None
    except Exception as exc:
        return BUILTIN_RULESET, "failed to load ruleset %r: %s" % (path, exc)


def ruleset_fingerprint(ruleset) -> str:
    canon = json.dumps(ruleset, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def match_rules(ruleset, feature_types):
    """Return (hits, confidence). Hits sorted by severity desc, then id."""
    present = set(feature_types)
    hits = [r for r in ruleset["rules"] if set(r["features"]) <= present]
    total = sum(r["weight"] for r in ruleset["rules"]) or 1.0
    confidence = min(1.0, sum(r["weight"] for r in hits) / total)
    hits = sorted(hits, key=lambda r: (-SEVERITY_ORDER[r["severity"]], r["id"]))
    return ([{"id": r["id"], "name": r["name"], "severity": r["severity"],
              "weight": r["weight"]} for r in hits],
            round(confidence, 6))
