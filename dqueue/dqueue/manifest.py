"""Parsing and validation of task manifests and retry policy tables.

Validation is fully executed before any persistent state is written, so a
rejected manifest leaves the database untouched.
"""

import hashlib
import json
import re

from .errors import DQError, ErrorCode

TOPIC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,63}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def payload_digest(payload):
    """SHA-256 hex digest of the canonical JSON encoding of the payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_nonneg_int(value, field, code):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DQError(code, f"{field} must be a non-negative integer",
                      field=field, got=repr(value))
    return value


def _as_pos_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DQError(ErrorCode.E_POLICY_INVALID,
                      f"{field} must be a positive integer",
                      field=field, got=repr(value))
    return value


def parse_policies(raw):
    """Validate the retry policy table and return label -> policy dict."""
    if not isinstance(raw, list) or not raw:
        raise DQError(ErrorCode.E_PARSE,
                      "retry policy table must be a non-empty JSON array")
    policies = {}
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DQError(ErrorCode.E_PARSE,
                          "each policy must be a JSON object", index=idx)
        label = item.get("label")
        if not isinstance(label, str) or not label:
            raise DQError(ErrorCode.E_PARSE,
                          "policy label must be a non-empty string", index=idx)
        if label in policies:
            raise DQError(ErrorCode.E_PARSE,
                          "duplicate retry policy label", label=label)
        base_ms = _as_pos_int(item.get("base_ms"), f"policy[{label}].base_ms")
        cap_ms = _as_pos_int(item.get("cap_ms"), f"policy[{label}].cap_ms")
        multiplier = item.get("multiplier", 2)
        if isinstance(multiplier, bool) or not isinstance(multiplier,
                                                          (int, float)) \
                or multiplier < 1:
            raise DQError(ErrorCode.E_POLICY_INVALID,
                          "policy multiplier must be a number >= 1",
                          label=label, got=repr(multiplier))
        jitter_pct = item.get("jitter_pct", 10)
        if isinstance(jitter_pct, bool) or not isinstance(jitter_pct,
                                                          (int, float)) \
                or not 0 <= jitter_pct <= 100:
            raise DQError(ErrorCode.E_POLICY_INVALID,
                          "policy jitter_pct must be within [0, 100]",
                          label=label, got=repr(jitter_pct))
        policies[label] = {
            "label": label,
            "base_ms": base_ms,
            "multiplier": float(multiplier),
            "cap_ms": cap_ms,
            "jitter_pct": float(jitter_pct),
        }
    return policies


def parse_manifest(raw, policies):
    """Validate task rows against the policy table.

    Returns a list of normalized task dicts ready for persistence.  Raises on
    the first invalid row; nothing is written by this function.
    """
    if not isinstance(raw, list) or not raw:
        raise DQError(ErrorCode.E_PARSE,
                      "task manifest must be a non-empty JSON array")
    tasks = []
    seen = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DQError(ErrorCode.E_PARSE,
                          "each task must be a JSON object", index=idx)
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
            raise DQError(ErrorCode.E_PARSE,
                          "task_id must match [A-Za-z0-9_-]{1,64}",
                          index=idx, got=repr(task_id))
        if task_id in seen:
            raise DQError(ErrorCode.E_TASK_DUPLICATE,
                          "duplicate task_id in manifest", task_id=task_id)
        seen.add(task_id)

        topic = item.get("topic")
        if not isinstance(topic, str) or not TOPIC_RE.match(topic):
            raise DQError(ErrorCode.E_TOPIC_INVALID,
                          "topic must match [A-Za-z][A-Za-z0-9_.-]{0,63}",
                          task_id=task_id, got=repr(topic))

        fire_ms = _as_nonneg_int(item.get("fire_ms"), "fire_ms",
                                 ErrorCode.E_CLOCK_INVALID)
        max_retries = _as_nonneg_int(item.get("max_retries"),
                                     "max_retries",
                                     ErrorCode.E_RETRIES_INVALID)

        policy_label = item.get("policy_label")
        if not isinstance(policy_label, str) \
                or policy_label not in policies:
            raise DQError(ErrorCode.E_POLICY_NOT_FOUND,
                          "task references an unknown retry policy label",
                          task_id=task_id, got=repr(policy_label),
                          known_labels=sorted(policies))

        payload = item.get("payload")
        if payload is None:
            raise DQError(ErrorCode.E_PARSE,
                          "task payload is required", task_id=task_id)
        digest = payload_digest(payload)
        claimed = item.get("payload_digest")
        if claimed is None:
            claimed = digest
        if not isinstance(claimed, str) or claimed != digest:
            raise DQError(ErrorCode.E_PAYLOAD_DIGEST_MISMATCH,
                          "payload_digest does not match payload content",
                          task_id=task_id, expected=digest, got=claimed)

        tasks.append({
            "task_id": task_id,
            "topic": topic,
            "fire_ms": fire_ms,
            "policy_label": policy_label,
            "max_retries": max_retries,
            "payload": payload,
            "payload_digest": digest,
        })
    return tasks


def load_files(manifest_path, policies_path):
    with open(policies_path, "r", encoding="utf-8") as fh:
        try:
            policies_raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise DQError(ErrorCode.E_PARSE,
                          "policy table is not valid JSON",
                          detail=str(exc)) from exc
    with open(manifest_path, "r", encoding="utf-8") as fh:
        try:
            manifest_raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise DQError(ErrorCode.E_PARSE,
                          "manifest is not valid JSON",
                          detail=str(exc)) from exc
    policies = parse_policies(policies_raw)
    tasks = parse_manifest(manifest_raw, policies)
    return tasks, policies
