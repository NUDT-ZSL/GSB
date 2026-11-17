"""精排配置与特征分组表的加载、校验与指纹。"""

from .errors import ConfigInvalid
from .fingerprint import fingerprint
from .fixedpoint import MAX_FRAC_BITS, MIN_FRAC_BITS, ROUNDING_MODES

# 不参与配置指纹（也不影响任何打分结果）的运行期参数。
RUNTIME_ONLY_KEYS = ("max_workers", "snapshot_interval")


def load_groups(path):
    """特征分组表：每组声明特征索引/名称以及量纲冲突标记。"""
    import json

    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict) or "groups" not in raw:
        raise ConfigInvalid("feature group table must contain 'groups'", "load", "E_CONFIG_INVALID")
    groups = []
    seen = set()
    for idx, g in enumerate(raw["groups"]):
        name = g.get("name") or f"group_{idx}"
        features = list(g.get("features", []))
        if not features:
            raise ConfigInvalid(f"group {name} has no features", "load", "E_CONFIG_INVALID")
        for f in features:
            if f in seen:
                raise ConfigInvalid(f"feature {f} appears in multiple groups", "load", "E_CONFIG_INVALID")
            seen.add(f)
        groups.append(
            {
                "name": name,
                "features": features,
                "scale_conflict": bool(g.get("scale_conflict", True)),
            }
        )
    table = {
        "groups": groups,
        "similarity_threshold": raw.get("similarity_threshold", 0.75),
        "hash": fingerprint(raw, "groups"),
    }
    return table


def groups_hash(table):
    return table["hash"]


def load_config(path):
    import json

    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return validate_config(raw)


def validate_config(raw):
    def need(key, typ):
        if key not in raw:
            raise ConfigInvalid(f"missing config key: {key}", "load", "E_CONFIG_INVALID")
        if not isinstance(raw[key], typ):
            raise ConfigInvalid(f"config key {key} must be {typ.__name__}", "load", "E_CONFIG_INVALID")
        return raw[key]

    feature_names = need("feature_names", list)
    dim = len(feature_names)
    if dim == 0 or len(set(feature_names)) != dim:
        raise ConfigInvalid("feature_names must be non-empty and unique", "load", "E_CONFIG_INVALID")

    weights = need("weights", list)
    if len(weights) != dim or not all(isinstance(w, (int, float)) for w in weights):
        raise ConfigInvalid("weights length must equal feature dimension", "load", "E_CONFIG_INVALID")

    norm_methods = need("normalization", list)
    if len(norm_methods) != dim or any(m not in ("minmax", "zscore") for m in norm_methods):
        raise ConfigInvalid("normalization methods must be minmax/zscore per feature", "load", "E_CONFIG_INVALID")

    frac_bits = need("frac_bits", int)
    if not (MIN_FRAC_BITS <= frac_bits <= MAX_FRAC_BITS):
        raise ConfigInvalid(
            f"frac_bits must be within [{MIN_FRAC_BITS},{MAX_FRAC_BITS}]", "load", "E_CONFIG_INVALID"
        )

    levels = raw.get("levels")
    if levels is not None and (not isinstance(levels, int) or levels < 2):
        raise ConfigInvalid("levels must be null or integer >= 2", "load", "E_CONFIG_INVALID")

    rounding = need("rounding", str)
    if rounding not in ROUNDING_MODES:
        raise ConfigInvalid(f"rounding must be one of {ROUNDING_MODES}", "load", "E_CONFIG_INVALID")

    temperature = need("temperature", (int, float))
    if temperature <= 0:
        raise ConfigInvalid("temperature must be positive", "load", "E_CONFIG_INVALID")

    clip = need("score_clip", list)
    if len(clip) != 2 or clip[0] > clip[1]:
        raise ConfigInvalid("score_clip must be [low, high] with low <= high", "load", "E_CONFIG_INVALID")

    window = need("rerank_window", int)
    if window <= 0:
        raise ConfigInvalid("rerank_window must be positive", "load", "E_CONFIG_INVALID")

    diversity_penalty = need("diversity_penalty", (int, float))
    if not (0.0 <= diversity_penalty <= 1.0):
        raise ConfigInvalid("diversity_penalty must be within [0,1]", "load", "E_CONFIG_INVALID")

    max_workers = raw.get("max_workers", 1)
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ConfigInvalid("max_workers must be positive integer", "load", "E_CONFIG_INVALID")

    snapshot_interval = raw.get("snapshot_interval", 1)
    if not isinstance(snapshot_interval, int) or snapshot_interval < 1:
        raise ConfigInvalid("snapshot_interval must be positive integer", "load", "E_CONFIG_INVALID")

    tolerance = raw.get("error_tolerance", 1)
    if not isinstance(tolerance, int) or tolerance < 0:
        raise ConfigInvalid("error_tolerance must be non-negative integer (LSB)", "load", "E_CONFIG_INVALID")

    cfg = {
        "feature_names": list(feature_names),
        "weights": [float(w) for w in weights],
        "normalization": list(norm_methods),
        "frac_bits": frac_bits,
        "levels": levels,
        "rounding": rounding,
        "temperature": float(temperature),
        "score_clip": [float(clip[0]), float(clip[1])],
        "rerank_window": window,
        "diversity_penalty": float(diversity_penalty),
        "max_workers": max_workers,
        "snapshot_interval": snapshot_interval,
        "error_tolerance": tolerance,
    }
    cfg["scientific_hash"] = config_hash(cfg)
    return cfg


def config_hash(cfg):
    """配置指纹：剔除并发度与快照间隔等运行期参数。"""
    payload = {k: v for k, v in cfg.items() if k not in RUNTIME_ONLY_KEYS and k != "scientific_hash"}
    return fingerprint(payload, "config")


def group_indices(table, feature_names):
    """把分组表中的特征名解析为特征索引列表。"""
    index_of = {name: i for i, name in enumerate(feature_names)}
    resolved = []
    used = set()
    for g in table["groups"]:
        idxs = []
        for f in g["features"]:
            if f not in index_of:
                raise ConfigInvalid(f"group feature {f} not in feature_names", "load", "E_CONFIG_INVALID")
            if index_of[f] in used:
                raise ConfigInvalid(f"feature {f} assigned to multiple groups", "load", "E_CONFIG_INVALID")
            used.add(index_of[f])
            idxs.append(index_of[f])
        resolved.append({"name": g["name"], "indices": idxs, "scale_conflict": g["scale_conflict"]})
    missing = [i for i in range(len(feature_names)) if i not in used]
    if missing:
        names = [feature_names[i] for i in missing]
        raise ConfigInvalid(f"features not covered by group table: {names}", "load", "E_CONFIG_INVALID")
    return resolved
