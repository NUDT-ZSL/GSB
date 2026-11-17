"""管线配置加载、校验与确定性哈希。"""

import hashlib
import json
import os

from .errors import PipelineError

DEFAULT_CONFIG_NAME = "pipeline_config.json"


def load_config(path):
    """读取 JSON 配置并补齐派生路径（相对路径基于配置文件目录解析）。"""
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    base_dir = os.path.dirname(os.path.abspath(path))
    config["_config_path"] = os.path.abspath(path)
    config["_base_dir"] = base_dir

    paths = config.setdefault("paths", {})
    for key, value in list(paths.items()):
        if not os.path.isabs(value):
            paths[key] = os.path.normpath(os.path.join(base_dir, value))
    paths.setdefault("snapshot_dir", os.path.join(base_dir, "..", "data", "output", "snapshots"))
    paths.setdefault("report_path", os.path.join(base_dir, "..", "data", "output", "report.json"))
    paths["snapshot_dir"] = os.path.abspath(paths["snapshot_dir"])
    paths["report_path"] = os.path.abspath(paths["report_path"])
    _validate(config)
    return config


def _validate(config):
    required_sections = (
        "languages", "normalization", "language_identification",
        "tokenization", "bm25", "bridge", "fusion", "batch",
    )
    for section in required_sections:
        if section not in config:
            raise PipelineError(f"missing config section: {section}")
    batch = config["batch"]
    for key in ("batch_size", "snapshot_interval", "max_workers"):
        if not isinstance(batch.get(key), int) or batch[key] < 1:
            raise PipelineError(f"batch.{key} must be positive integer")
    fusion = config["fusion"]
    weight_sum = fusion["mono_weight"] + fusion["cross_weight"]
    if weight_sum <= 0:
        raise PipelineError("fusion weights must sum to a positive number")
    if len(config["languages"]["allowed"]) < 1:
        raise PipelineError("languages.allowed is empty")


def canonical_json_bytes(obj):
    """以固定分隔符、排序键、不转义非 ASCII 的方式序列化，供哈希使用。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def config_hash(config):
    """对影响计算结果的配置内容计算 SHA-256（剔除输出路径与故障注入等非计算字段）。"""
    excluded_top = {
        "_config_path", "_base_dir", "fault_injection", "test_hooks",
    }
    output_path_keys = {"snapshot_dir", "report_path"}
    payload = {k: v for k, v in config.items() if k not in excluded_top}
    if "paths" in payload:
        payload["paths"] = {
            k: v for k, v in payload["paths"].items() if k not in output_path_keys
        }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def translation_hash(translation_pairs):
    """翻译对照表规范化哈希：按源词、目标词排序后计算。"""
    normalized = sorted(
        (p["source"], p["target"], round(float(p["confidence"]), 6))
        for p in translation_pairs
    )
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
