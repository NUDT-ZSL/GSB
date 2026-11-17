"""Manifest codecs (v1/v2/v3), strict validation, canonical digest, in-memory upgrade.

v1: {"format_version":1, "entries":{key: value}}
v2: + per-entry {"key","value","version","targets":[node|"*"]}
v3: + "policy" (distribution strategy) and "derive" (derivation relations)
"""
import hashlib
import json

from .errors import E_FORMAT, PlatformError

CURRENT_VERSION = 3
SUPPORTED_VERSIONS = (1, 2, 3)

DEFAULT_POLICY = {
    "strategy": "broadcast",      # broadcast | shard
    "max_concurrency": 4,
    "timeout_ms": 500,
    "retries": 2,
}


def _err(path, msg):
    raise PlatformError(E_FORMAT, msg, path=path)


def _require(cond, path, msg):
    if not cond:
        _err(path, msg)


def _is_str(x):
    return isinstance(x, str)


def _validate_entry_v2(entry, path):
    _require(isinstance(entry, dict), path, "entry must be an object")
    for field in ("key", "value", "version", "targets"):
        _require(field in entry, f"{path}.{field}", f"missing field '{field}'")
    _require(_is_str(entry["key"]), f"{path}.key", "key must be a string")
    _require(_is_str(entry["value"]), f"{path}.value", "value must be a string")
    _require(isinstance(entry["version"], int) and not isinstance(entry["version"], bool)
             and entry["version"] >= 1, f"{path}.version", "version must be an int >= 1")
    _require(isinstance(entry["targets"], list) and entry["targets"]
             and all(_is_str(t) for t in entry["targets"]),
             f"{path}.targets", "targets must be a non-empty list of strings")


def _validate_policy(policy, path):
    _require(isinstance(policy, dict), path, "policy must be an object")
    for field in ("strategy", "max_concurrency", "timeout_ms", "retries"):
        _require(field in policy, f"{path}.{field}", f"missing field '{field}'")
    _require(policy["strategy"] in ("broadcast", "shard"),
             f"{path}.strategy", "strategy must be 'broadcast' or 'shard'")
    _require(isinstance(policy["max_concurrency"], int) and policy["max_concurrency"] >= 1,
             f"{path}.max_concurrency", "max_concurrency must be an int >= 1")
    _require(isinstance(policy["timeout_ms"], int) and policy["timeout_ms"] >= 1,
             f"{path}.timeout_ms", "timeout_ms must be an int >= 1")
    _require(isinstance(policy["retries"], int) and policy["retries"] >= 0,
             f"{path}.retries", "retries must be an int >= 0")


def _validate_derive(derive, path):
    _require(isinstance(derive, list), path, "derive must be a list")
    for i, rule in enumerate(derive):
        rp = f"{path}[{i}]"
        _require(isinstance(rule, dict), rp, "derive rule must be an object")
        for field in ("node", "from", "algorithm"):
            _require(field in rule, f"{rp}.{field}", f"missing field '{field}'")
        _require(_is_str(rule["node"]), f"{rp}.node", "node must be a string")
        _require(isinstance(rule["from"], list) and all(_is_str(k) for k in rule["from"]),
                 f"{rp}.from", "from must be a list of strings")
        _require(rule["algorithm"] == "sha256", f"{rp}.algorithm",
                 "only 'sha256' is supported")


def detect_version(doc):
    _require(isinstance(doc, dict), "$", "manifest root must be an object")
    _require("format_version" in doc, "$.format_version", "missing field 'format_version'")
    ver = doc["format_version"]
    _require(ver in SUPPORTED_VERSIONS, "$.format_version",
             f"unknown format_version {ver!r}; supported: {list(SUPPORTED_VERSIONS)}")
    return ver


def normalize(doc):
    """Validate any supported version and return the canonical v3 internal form."""
    ver = detect_version(doc)
    if ver == 1:
        _require("entries" in doc, "$.entries", "missing field 'entries'")
        _require(isinstance(doc["entries"], dict), "$.entries",
                 "v1 entries must be an object of key->value")
        entries = []
        for key in sorted(doc["entries"]):
            value = doc["entries"][key]
            _require(_is_str(value), f"$.entries.{key}", "v1 values must be strings")
            entries.append({"key": key, "value": value, "version": 1, "targets": ["*"]})
        policy = dict(DEFAULT_POLICY)
        derive = []
    elif ver == 2:
        _require("entries" in doc, "$.entries", "missing field 'entries'")
        _require(isinstance(doc["entries"], list), "$.entries", "v2 entries must be a list")
        entries = []
        for i, entry in enumerate(doc["entries"]):
            _validate_entry_v2(entry, f"$.entries[{i}]")
            entries.append({"key": entry["key"], "value": entry["value"],
                            "version": entry["version"],
                            "targets": sorted(entry["targets"])})
        policy = dict(DEFAULT_POLICY)
        derive = []
    else:
        _require("entries" in doc, "$.entries", "missing field 'entries'")
        _require(isinstance(doc["entries"], list), "$.entries", "v3 entries must be a list")
        entries = []
        for i, entry in enumerate(doc["entries"]):
            _validate_entry_v2(entry, f"$.entries[{i}]")
            entries.append({"key": entry["key"], "value": entry["value"],
                            "version": entry["version"],
                            "targets": sorted(entry["targets"])})
        _require("policy" in doc, "$.policy", "missing field 'policy'")
        _validate_policy(doc["policy"], "$.policy")
        _require("derive" in doc, "$.derive", "missing field 'derive'")
        _validate_derive(doc["derive"], "$.derive")
        policy = {k: doc["policy"][k] for k in
                  ("strategy", "max_concurrency", "timeout_ms", "retries")}
        derive = [{"node": r["node"], "from": sorted(r["from"]), "algorithm": r["algorithm"]}
                  for r in doc["derive"]]

    keys = [e["key"] for e in entries]
    _require(len(keys) == len(set(keys)), "$.entries", "duplicate entry keys")
    entries.sort(key=lambda e: e["key"])
    derive.sort(key=lambda r: (r["node"], tuple(r["from"])))
    return {"format_version": CURRENT_VERSION, "entries": entries,
            "policy": policy, "derive": derive}


def loads(text):
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        _err(f"$[line {exc.lineno} col {exc.colno}]", f"invalid JSON: {exc.msg}")
    return normalize(doc)


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise PlatformError(E_FORMAT, f"cannot read manifest: {exc}", path=path)
    return loads(text)


def dumps(manifest):
    """Serialize the canonical v3 form (current version on write)."""
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def canonical_digest(manifest):
    """Stable digest: identical for the same semantic manifest regardless of
    source version, field order, or whitespace."""
    canon = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
