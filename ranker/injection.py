"""独立故障注入配置：对指定请求的指定阶段施加真实输入故障。"""

from .errors import ConfigInvalid, InputInvalid, StageFailed
from .fingerprint import fingerprint


INJECTION_TYPES = (
    "non_finite_feature",
    "weight_length_mismatch",
    "frac_bits_out_of_range",
    "sim_equal_threshold",
)


def load_injection(path):
    import json

    if not path:
        return {"items": [], "hash": fingerprint({"items": []}, "injection")}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    items = raw.get("items", [])
    if not isinstance(items, list):
        raise ConfigInvalid("injection file must contain items list", "load", "E_CONFIG_INVALID")
    for idx, item in enumerate(items):
        if item.get("type") not in INJECTION_TYPES:
            raise ConfigInvalid(
                f"injection item {idx} type must be one of {INJECTION_TYPES}",
                "load",
                "E_CONFIG_INVALID",
            )
        if not item.get("request_id") or not item.get("stage"):
            raise ConfigInvalid(
                f"injection item {idx} needs request_id and stage", "load", "E_CONFIG_INVALID"
            )
        item.setdefault("item_id", None)
    return {"items": items, "hash": fingerprint(raw, "injection")}


def injections_for(items, request_id):
    return [item for item in items if item["request_id"] == request_id]


def apply_normalize_injection(request, items, feature_names):
    for item in items:
        if item["stage"] != "normalize":
            continue
        if item["type"] == "non_finite_feature":
            target = _find_candidate(request, item.get("item_id"))
            target["dense_map"][feature_names[0]] = float("nan")
            raise InputInvalid(
                f"injection {item['id']}: non-finite feature injected",
                "normalize",
                "E_INPUT_INVALID",
            )
        if item["type"] == "frac_bits_out_of_range":
            raise StageFailed(
                f"injection {item['id']}: frac_bits {item.get('frac_bits', 99)} out of range",
                "normalize",
                "E_CONFIG_INVALID",
            )


def apply_fuse_config_injection(config, items):
    for item in items:
        if item["stage"] != "fuse":
            continue
        if item["type"] == "weight_length_mismatch":
            broken = dict(config)
            broken["weights"] = list(config["weights"])[:-1]
            raise StageFailed(
                f"injection {item['id']}: weights length {len(broken['weights'])} "
                f"!= feature dim {len(config['feature_names'])}",
                "fuse",
                "E_CONFIG_INVALID",
            )
        if item["type"] == "frac_bits_out_of_range":
            raise StageFailed(
                f"injection {item['id']}: frac_bits out of range",
                "fuse",
                "E_CONFIG_INVALID",
            )


def rerank_threshold_override(items):
    """sim_equal_threshold：把折减门限改成与候选对相似度定点码完全相等。

    返回 (覆盖门限浮点, 命中注入项 id 列表)。实际命中在重排时由
    sim_q == threshold_q 验证。
    """
    hits = []
    override = None
    for item in items:
        if item["stage"] == "rerank" and item["type"] == "sim_equal_threshold":
            hits.append(item["id"])
            if override is None:
                override = item.get("threshold", 0.75)
    return override, hits


def _find_candidate(request, item_id):
    for cand in request["candidates"]:
        if item_id is None or cand["item_id"] == item_id:
            return cand
    raise StageFailed(
        f"injection target item {item_id} not found", "normalize", "E_INPUT_INVALID"
    )
