"""报告构建与校验模块。

报告包含：业务错误码、崩溃分类、差异结论、恢复记录。
编码(encode)、执行(execute)、恢复(recover)三模块的 error_codes 必须语义一致；
缺字段或码值冲突即判校验失败。
"""
from __future__ import annotations

from . import codec

REQUIRED_TOP = ("version", "corpus", "encode", "execute", "recover", "diff")
MODULE_REQUIRED = {
    "encode": ("error_codes", "cases"),
    "execute": ("error_codes", "classifications", "crashes"),
    "recover": ("error_codes", "records"),
}
CORPUS_REQUIRED = ("fingerprint", "class_counts")
DIFF_REQUIRED = ("new_crashes", "disappeared_crashes", "behavior_flips", "counts")


def build_report(corpus_meta: dict, encode_cases: list[dict],
                 execute_stats: dict, recover_records: list[dict],
                 diff_result: dict) -> dict:
    # 每个模块持有独立码表副本，校验器负责检测三者是否语义一致
    return {
        "version": 1,
        "corpus": corpus_meta,
        "encode": {"error_codes": dict(codec.ERROR_CODES), "cases": encode_cases},
        "execute": {"error_codes": dict(codec.ERROR_CODES), **execute_stats},
        "recover": {"error_codes": dict(codec.ERROR_CODES), "records": recover_records},
        "diff": diff_result,
    }


def validate_report(rep: dict) -> list[str]:
    """返回问题列表；空列表表示校验通过。"""
    problems: list[str] = []
    if not isinstance(rep, dict):
        return ["report is not a JSON object"]
    for k in REQUIRED_TOP:
        if k not in rep:
            problems.append(f"missing top-level field: {k}")
    if problems:
        return problems
    for mod, fields in MODULE_REQUIRED.items():
        if not isinstance(rep[mod], dict):
            problems.append(f"module not an object: {mod}")
            continue
        for f in fields:
            if f not in rep[mod]:
                problems.append(f"missing field: {mod}.{f}")
    if problems:
        return problems
    # 三模块错误码集合一致性：名称与码值都必须相同，冲突即失败
    base = rep["encode"]["error_codes"]
    for mod in ("execute", "recover"):
        other = rep[mod]["error_codes"]
        if set(other) != set(base):
            problems.append(f"error code set mismatch: {mod} vs encode")
        else:
            for name, val in base.items():
                if other[name] != val:
                    problems.append(
                        f"error code conflict: {mod}.{name}={other[name]} != {val}")
    for k in CORPUS_REQUIRED:
        if k not in rep["corpus"]:
            problems.append(f"missing field: corpus.{k}")
    for k in DIFF_REQUIRED:
        if k not in rep["diff"]:
            problems.append(f"missing field: diff.{k}")
    return problems
