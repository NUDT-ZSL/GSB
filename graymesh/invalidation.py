"""Patch application and precise dependency-invalidation computation.

Given old and new object sets, compute which derived artifacts must be
rebuilt and which can be safely reused:
  - parse_cache:      parsed representation of changed objects
  - member_snapshot:  resolved member lists of affected upstream groups
  - connection_pool:  pooled connections of changed/removed members
  - mirror_sampling:  sampling decisions of affected mirror policies / routes
Unaffected artifacts are listed under "reused" and must produce
bit-identical decisions after the patch.
"""

from collections import deque

from .errors import BusinessError, ErrorCode
from .hashing import canonical_json
from .models import KINDS, build_dependency_graph, validate_object


def _fmt(key):
    return "%s/%s" % key


def apply_patch(objects, patch):
    """Validate and apply a patch to an object dict. Returns a new dict.

    Raises BusinessError(E_PATCH_FORMAT) on malformed input; never mutates input.
    """
    if not isinstance(patch, dict):
        raise BusinessError(ErrorCode.PATCH_FORMAT, "patch must be a dict")
    patch_id = patch.get("patch_id")
    if not isinstance(patch_id, str) or not patch_id:
        raise BusinessError(ErrorCode.PATCH_FORMAT, "patch.patch_id must be a non-empty string")
    expected = patch.get("expected_version")
    if expected is not None and not isinstance(expected, int):
        raise BusinessError(ErrorCode.PATCH_FORMAT, "patch.expected_version must be int or null")
    ops = patch.get("ops")
    if not isinstance(ops, list) or not ops:
        raise BusinessError(ErrorCode.PATCH_FORMAT, "patch.ops must be a non-empty list")

    result = dict(objects)
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise BusinessError(ErrorCode.PATCH_FORMAT, "invalid op at index %d" % index)
        action, kind, oid = op.get("op"), op.get("kind"), op.get("id")
        if (action not in ("add", "replace", "remove") or kind not in KINDS
                or not isinstance(oid, str) or not oid):
            raise BusinessError(ErrorCode.PATCH_FORMAT, "invalid op at index %d" % index,
                                {"op": op})
        key = (kind, oid)
        if action == "remove":
            result.pop(key, None)
            continue
        obj = op.get("object")
        if not isinstance(obj, dict) or obj.get("kind") != kind or obj.get("id") != oid:
            raise BusinessError(ErrorCode.PATCH_FORMAT,
                                "op %d: object kind/id must match op kind/id" % index)
        validate_object(obj)
        result[key] = obj
    return result


def _changed_keys(old, new):
    changed = set()
    for key in old.keys() | new.keys():
        old_obj, new_obj = old.get(key), new.get(key)
        if old_obj is None or new_obj is None:
            changed.add(key)
        elif canonical_json(old_obj) != canonical_json(new_obj):
            changed.add(key)
    return changed


def _affected_closure(old, new, changed):
    """Changed objects plus everything that transitively depends on them."""
    union = dict(old)
    union.update(new)
    if union:
        _, reverse = build_dependency_graph(union)
    else:
        reverse = {}
    affected = set()
    queue = deque(sorted(changed))
    while queue:
        key = queue.popleft()
        if key in affected:
            continue
        affected.add(key)
        for dependent in reverse.get(key, []):
            if dependent not in affected:
                queue.append(dependent)
    return affected, reverse


def _propagation_paths(changed, affected, reverse):
    """Shortest propagation path from each changed object to each dependent."""
    paths = []
    for source in sorted(changed):
        parents = {source: None}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for dependent in reverse.get(node, []):
                if dependent not in parents:
                    parents[dependent] = node
                    queue.append(dependent)
        for target in sorted(affected):
            if target == source or target not in parents:
                continue
            chain = []
            node = target
            while node is not None:
                chain.append(_fmt(node))
                node = parents[node]
            paths.append({"from": _fmt(source), "path": list(reversed(chain))})
    return paths


def compute_invalidation(old, new, patch_id, new_version):
    """Compute the invalidation event for a patch. Pure function."""
    changed = _changed_keys(old, new)
    affected, reverse = _affected_closure(old, new, changed)

    parse_invalid = set(changed)
    pool_invalid = {k for k in changed if k[0] == "upstream_member"}

    candidate_groups = {k for k in old.keys() | new.keys() if k[0] == "upstream_group"}
    snapshot_invalid = set()
    for key in candidate_groups:
        if key in changed:
            snapshot_invalid.add(key)
            continue
        obj = new.get(key) or old.get(key)
        refs = {("upstream_member", m) for m in obj["spec"]["member_ids"]}
        if obj["spec"].get("fallback_group_id"):
            refs.add(("upstream_group", obj["spec"]["fallback_group_id"]))
        if refs & changed:
            snapshot_invalid.add(key)

    mirror_invalid = {k for k in changed if k[0] in ("mirror_policy", "gray_route")}
    changed_policies = {k[1] for k in changed if k[0] == "mirror_policy"}
    for key in old.keys() | new.keys():
        if key[0] != "gray_route" or key in mirror_invalid:
            continue
        obj = new.get(key) or old.get(key)
        if obj["spec"].get("mirror_policy_id") in changed_policies:
            mirror_invalid.add(key)

    invalidated = {
        "parse_cache": [_fmt(k) for k in sorted(parse_invalid)],
        "member_snapshot": [_fmt(k) for k in sorted(snapshot_invalid)],
        "connection_pool": [_fmt(k) for k in sorted(pool_invalid)],
        "mirror_sampling": [_fmt(k) for k in sorted(mirror_invalid)],
    }

    all_keys = old.keys() | new.keys()
    reused = {
        "parse_cache": [_fmt(k) for k in sorted(all_keys - changed)],
        "member_snapshot": [_fmt(k) for k in sorted(candidate_groups - snapshot_invalid)],
        "connection_pool": [_fmt(k) for k in sorted(
            {k for k in all_keys if k[0] == "upstream_member"} - pool_invalid)],
        "mirror_sampling": [_fmt(k) for k in sorted(
            {k for k in all_keys if k[0] == "gray_route"} - mirror_invalid)],
    }

    return {
        "version": new_version,
        "trigger": {"patch_id": patch_id, "changed": [_fmt(k) for k in sorted(changed)]},
        "affected_closure": [_fmt(k) for k in sorted(affected)],
        "invalidated": invalidated,
        "reused": reused,
        "propagation": _propagation_paths(changed, affected, reverse),
    }
