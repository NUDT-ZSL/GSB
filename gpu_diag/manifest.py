"""资源池 / 配额表 / 作业队列的本地 JSON 加载与强校验。

任一非法字段都会被收集，最终以 :class:`ManifestValidationError`（E_MANIFEST）
整体拒绝加载，不会返回部分可用对象。
"""

import json
import os
import re

from .errors import BusinessError, E_MANIFEST, ManifestValidationError
from .models import Job, MemoryCurve, Pool, QuotaLimit, ScaleParams, TopologyGroup

_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_TOPO_CONSTRAINTS = ("same_group", "distinct_group", "any")
_SCALE_FIELDS = ("batch_size", "seq_len", "model_dim")


class Manifest:
    def __init__(self, pools, quotas, jobs, scale_bounds, scale_params, raw):
        self.pools = pools
        self.quotas = quotas
        self.jobs = jobs
        self.scale_bounds = scale_bounds
        self.scale_params = scale_params
        self.raw = raw


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise BusinessError(E_MANIFEST, "manifest file not found", field_path=path)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_MANIFEST,
            "invalid JSON: {}".format(exc.msg),
            field_path="{}:{}:{}".format(path, exc.lineno, exc.colno),
        )


def _load_raw(path):
    if os.path.isdir(path):
        pieces = {}
        for name in ("pools", "quotas", "jobs"):
            file_path = os.path.join(path, "{}.json".format(name))
            pieces[name] = _read_json(file_path)
        scale_path = os.path.join(path, "scale_params.json")
        if os.path.exists(scale_path):
            pieces["scale"] = _read_json(scale_path)
        return pieces, True
    return _read_json(path), False


def _issue(issues, field_path, message):
    issues.append({"field_path": field_path, "message": message})


def _validate_scale(raw, issues):
    scale_bounds = {}
    bounds = raw.get("scale_bounds")
    if not isinstance(bounds, dict):
        _issue(issues, "$.scale_bounds", "must be an object with batch_size/seq_len/model_dim ranges")
        bounds = {}
    for name in _SCALE_FIELDS:
        rng = bounds.get(name)
        path = "$.scale_bounds.{}".format(name)
        if not isinstance(rng, list) or len(rng) != 2:
            _issue(issues, path, "must be a [min, max] list")
            continue
        low, high = rng
        if not (_is_int(low) and _is_int(high)) or low <= 0 or high < low:
            _issue(issues, path, "must be positive integers with min <= max")
            continue
        scale_bounds[name] = (low, high)

    scale_params = {}
    params = raw.get("scale_params")
    if not isinstance(params, dict):
        _issue(issues, "$.scale_params", "must be an object")
        return scale_bounds, None
    for name in _SCALE_FIELDS:
        path = "$.scale_params.{}".format(name)
        value = params.get(name)
        if not _is_int(value):
            _issue(issues, path, "missing or not an integer")
            continue
        if name in scale_bounds:
            low, high = scale_bounds[name]
            if not low <= value <= high:
                _issue(issues, path, "value {} outside allowed range [{}, {}]".format(value, low, high))
        scale_params[name] = value
    return scale_bounds, ScaleParams(**scale_params) if len(scale_params) == 3 else None


def _validate_pools(raw_pools, issues):
    pools = []
    if not isinstance(raw_pools, list) or not raw_pools:
        _issue(issues, "$.pools", "must be a non-empty list")
        return pools
    seen_names = set()
    for index, raw_pool in enumerate(raw_pools):
        base = "$.pools[{}]".format(index)
        if not isinstance(raw_pool, dict):
            _issue(issues, base, "must be an object")
            continue
        name = raw_pool.get("name")
        if not isinstance(name, str) or not name:
            _issue(issues, "{}.name".format(base), "must be a non-empty string")
        elif name in seen_names:
            _issue(issues, "{}.name".format(base), "duplicate pool name: {}".format(name))
        else:
            seen_names.add(name)

        card_count = raw_pool.get("card_count")
        if not _is_int(card_count) or card_count <= 0:
            _issue(issues, "{}.card_count".format(base), "must be a positive integer")
            card_count = 0

        card_memory = raw_pool.get("card_memory_mib")
        if not _is_int(card_memory) or card_memory <= 0:
            _issue(issues, "{}.card_memory_mib".format(base), "must be a positive integer (MiB)")

        groups = _validate_topology(raw_pool, card_count, base, issues)
        drained = _validate_drained(raw_pool, card_count, base, issues)
        if name and card_count:
            pools.append(Pool(name, card_count, card_memory or 0, groups, tuple(drained)))
    return pools


def _validate_topology(raw_pool, card_count, base, issues):
    raw_groups = raw_pool.get("topology_groups")
    path = "{}.topology_groups".format(base)
    if not isinstance(raw_groups, list) or not raw_groups:
        _issue(issues, path, "must be a non-empty list of topology groups")
        return []
    groups = []
    seen_labels = set()
    covered = set()
    for g_index, raw_group in enumerate(raw_groups):
        g_path = "{}[{}]".format(path, g_index)
        if not isinstance(raw_group, dict):
            _issue(issues, g_path, "must be an object")
            continue
        label = raw_group.get("label")
        if not isinstance(label, str) or not _LABEL_RE.match(label or ""):
            _issue(issues, "{}.label".format(g_path), "must match [A-Za-z0-9_.-]+")
        elif label in seen_labels:
            _issue(issues, "{}.label".format(g_path), "duplicate topology label: {}".format(label))
        else:
            seen_labels.add(label)
        cards = raw_group.get("cards")
        if not isinstance(cards, list) or not cards:
            _issue(issues, "{}.cards".format(g_path), "must be a non-empty integer list")
            continue
        group_cards = []
        for c_index, card in enumerate(cards):
            c_path = "{}.cards[{}]".format(g_path, c_index)
            if not _is_int(card) or not 0 <= card < card_count:
                _issue(issues, c_path, "card index must be an integer within [0, {})".format(card_count))
                continue
            if card in covered:
                _issue(issues, c_path, "card {} already assigned to another topology group".format(card))
                continue
            covered.add(card)
            group_cards.append(card)
        if label and group_cards:
            groups.append(TopologyGroup(label, tuple(sorted(group_cards))))
    if card_count and len(covered) != card_count:
        _issue(issues, path, "topology groups must cover every card exactly once (covered {}/{})".format(
            len(covered), card_count))
    return [group for group in sorted(groups, key=lambda item: item.label)]


def _validate_drained(raw_pool, card_count, base, issues):
    drained = raw_pool.get("drained_cards", [])
    path = "{}.drained_cards".format(base)
    if not isinstance(drained, list):
        _issue(issues, path, "must be a list of card indices")
        return []
    result = []
    for index, card in enumerate(drained):
        if not _is_int(card) or not 0 <= card < card_count:
            _issue(issues, "{}[{}]".format(path, index), "drained card outside [0, {})".format(card_count))
        elif card not in result:
            result.append(card)
    return sorted(result)


def _validate_quotas(raw_quotas, issues):
    quotas = {}
    if not isinstance(raw_quotas, list) or not raw_quotas:
        _issue(issues, "$.quotas", "must be a non-empty list")
        return quotas
    seen = set()
    for index, raw_quota in enumerate(raw_quotas):
        base = "$.quotas[{}]".format(index)
        if not isinstance(raw_quota, dict):
            _issue(issues, base, "must be an object")
            continue
        group_id = raw_quota.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            _issue(issues, "{}.group_id".format(base), "must be a non-empty string")
        elif group_id in seen:
            _issue(issues, "{}.group_id".format(base), "duplicate quota group: {}".format(group_id))
        else:
            seen.add(group_id)
        max_cards = raw_quota.get("max_cards")
        if not _is_int(max_cards) or max_cards <= 0:
            _issue(issues, "{}.max_cards".format(base), "must be a positive integer")
        max_memory = raw_quota.get("max_memory_mib")
        if not _is_int(max_memory) or max_memory <= 0:
            _issue(issues, "{}.max_memory_mib".format(base), "must be a positive integer")
        if group_id and _is_int(max_cards) and _is_int(max_memory):
            quotas[group_id] = QuotaLimit(group_id, max_cards, max_memory)
    return quotas


def _validate_jobs(raw_jobs, pools_by_name, quotas, issues):
    jobs = []
    if not isinstance(raw_jobs, list) or not raw_jobs:
        _issue(issues, "$.jobs", "must be a non-empty list")
        return jobs
    seen_ids = set()
    for index, raw_job in enumerate(raw_jobs):
        base = "$.jobs[{}]".format(index)
        if not isinstance(raw_job, dict):
            _issue(issues, base, "must be an object")
            continue
        job_id = raw_job.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            _issue(issues, "{}.job_id".format(base), "must be a non-empty string")
        elif job_id in seen_ids:
            _issue(issues, "{}.job_id".format(base), "duplicate job id: {}".format(job_id))
        else:
            seen_ids.add(job_id)

        pool_name = raw_job.get("pool_name")
        pool = pools_by_name.get(pool_name) if isinstance(pool_name, str) else None
        if pool is None:
            _issue(issues, "{}.pool_name".format(base), "unknown pool: {!r}".format(pool_name))

        card_count = raw_job.get("card_count")
        if not _is_int(card_count) or card_count <= 0:
            _issue(issues, "{}.card_count".format(base), "must be a positive integer")
        elif pool is not None and card_count > pool.card_count:
            _issue(issues, "{}.card_count".format(base),
                    "requested {} cards exceeds pool {} total {}".format(card_count, pool.name, pool.card_count))

        quota_group = raw_job.get("quota_group")
        if quota_group not in quotas:
            _issue(issues, "{}.quota_group".format(base),
                    "quota group {!r} not found in quota table".format(quota_group))

        priority = raw_job.get("priority")
        if not _is_int(priority):
            _issue(issues, "{}.priority".format(base), "must be an integer")

        constraint = raw_job.get("topology_constraint")
        if constraint not in _TOPO_CONSTRAINTS:
            _issue(issues, "{}.topology_constraint".format(base),
                    "must be one of {}".format(",".join(_TOPO_CONSTRAINTS)))

        curve = _validate_curve(raw_job.get("memory_curve"), base, issues)
        if job_id and pool is not None and quota_group in quotas and _is_int(card_count) \
                and _is_int(priority) and constraint in _TOPO_CONSTRAINTS and curve is not None:
            jobs.append(Job(job_id, pool.name, card_count, quota_group, priority,
                            constraint, curve, index))
    return jobs


def _validate_curve(raw_curve, base, issues):
    path = "{}.memory_curve".format(base)
    if not isinstance(raw_curve, dict):
        _issue(issues, path, "must be an object")
        return None
    bytes_per_param = raw_curve.get("bytes_per_param")
    base_mib = raw_curve.get("base_mib")
    safety_pct = raw_curve.get("safety_pct")
    valid = True
    if not _is_int(bytes_per_param) or bytes_per_param <= 0:
        _issue(issues, "{}.bytes_per_param".format(path), "must be a positive integer")
        valid = False
    if not _is_int(base_mib) or base_mib < 0:
        _issue(issues, "{}.base_mib".format(path), "must be a non-negative integer (MiB)")
        valid = False
    if not _is_int(safety_pct) or not 0 <= safety_pct <= 100:
        _issue(issues, "{}.safety_pct".format(path), "must be an integer in [0, 100]")
        valid = False
    if not valid:
        return None
    return MemoryCurve(bytes_per_param, base_mib, safety_pct)


def parse_manifest(raw):
    """对已加载的 JSON 字典执行强校验，失败时抛出 E_MANIFEST 聚合异常。"""
    issues = []
    if not isinstance(raw, dict):
        raise ManifestValidationError([{"field_path": "$", "message": "manifest root must be an object"}])
    scale_bounds, scale_params = _validate_scale(raw, issues)
    pools = _validate_pools(raw.get("pools"), issues)
    quotas = _validate_quotas(raw.get("quotas"), issues)
    jobs = _validate_jobs(raw.get("jobs"), {pool.name: pool for pool in pools}, quotas, issues)
    if issues:
        raise ManifestValidationError(issues)
    return Manifest(pools, quotas, jobs, scale_bounds, scale_params, raw)


def load_manifest(path):
    """从本地 JSON 文件或三文件目录加载并整体校验清单。"""
    raw, is_dir = _load_raw(path)
    if is_dir:
        merged = {
            "pools": raw.get("pools"),
            "quotas": raw.get("quotas"),
            "jobs": raw.get("jobs"),
        }
        scale = raw.get("scale") or {}
        merged["scale_bounds"] = scale.get("scale_bounds")
        merged["scale_params"] = scale.get("scale_params")
        raw = merged
    return parse_manifest(raw)
