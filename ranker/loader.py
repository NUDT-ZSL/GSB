"""候选集 JSONL 逐行加载与校验。

校验规则：请求 ID 非空、候选物品 ID 在请求内唯一、召回分数与稠密特征为
有限数、稀疏计数为非负整数、特征维度与配置一致。不合法条目不阻断其余
条目处理，而是进入失败清单，业务码 E_INPUT_INVALID。
"""

import math

from .errors import InputInvalid


def _is_finite_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def parse_line(line, line_no, dense_dim, recall_names):
    obj = _load_json(line, line_no)
    request_id = obj.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise InputInvalid(f"line {line_no}: empty request_id", "load")
    item_id = obj.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        raise InputInvalid(f"line {line_no}: empty item_id", "load")

    scores = obj.get("scores", {})
    if not isinstance(scores, dict):
        raise InputInvalid(f"line {line_no}: scores must be object", "load")
    for name in recall_names:
        if not _is_finite_number(scores.get(name)):
            raise InputInvalid(f"line {line_no}: score {name} missing/non-finite", "load")
    for key, value in scores.items():
        if not _is_finite_number(value):
            raise InputInvalid(f"line {line_no}: score {key} non-finite", "load")

    dense = obj.get("dense", [])
    if not isinstance(dense, list) or not all(_is_finite_number(x) for x in dense):
        raise InputInvalid(f"line {line_no}: dense must be list of finite numbers", "load")

    sparse = obj.get("sparse", {})
    if not isinstance(sparse, dict):
        raise InputInvalid(f"line {line_no}: sparse must be object", "load")
    for key, value in sparse.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InputInvalid(f"line {line_no}: sparse {key} must be non-negative integer", "load")

    if len(dense) != dense_dim:
        raise InputInvalid(
            f"line {line_no}: dense dim {len(dense)} != config dense dim {dense_dim}",
            "load",
        )
    return {
        "request_id": request_id,
        "item_id": item_id,
        "scores": {k: float(scores[k]) for k in recall_names},
        "dense": [float(x) for x in dense],
        "sparse": dict(sparse),
        "line_no": line_no,
    }


def _load_json(line, line_no):
    import json

    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise InputInvalid(f"line {line_no}: malformed JSON ({exc.msg})", "load") from exc
    if not isinstance(obj, dict):
        raise InputInvalid(f"line {line_no}: record must be JSON object", "load")
    return obj


def load_candidates(path, dense_dim, recall_names, sparse_names):
    """返回 (请求顺序列表, 失败清单)。

    请求顺序按请求首次出现的行号确定；同一请求内候选保持文件中的相对顺序。
    任一候选不合法时整个请求进入失败清单（该请求不参与后续阶段）。
    """
    grouped = {}
    order = []
    failures = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = parse_line(line, line_no, dense_dim, recall_names)
            except InputInvalid as exc:
                failures.append(
                    {
                        "line_no": line_no,
                        "request_id": _safe_request_id(raw),
                        "stage": "load",
                        "code": exc.code,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                continue
            rid = rec["request_id"]
            if rid not in grouped:
                grouped[rid] = {
                    "request_id": rid,
                    "candidates": [],
                    "seen_items": set(),
                    "first_line": line_no,
                }
                order.append(rid)
            bucket = grouped[rid]
            if rec["item_id"] in bucket["seen_items"]:
                failures.append(
                    {
                        "line_no": line_no,
                        "request_id": rid,
                        "stage": "load",
                        "code": InputInvalid.code,
                        "error_type": "InputInvalid",
                        "message": f"line {line_no}: duplicate item_id {rec['item_id']} in request {rid}",
                    }
                )
                bucket["invalid"] = True
                continue
            bucket["seen_items"].add(rec["item_id"])
            bucket["candidates"].append(rec)

    requests, dropped = [], set()
    for rid in order:
        bucket = grouped[rid]
        if bucket.get("invalid"):
            dropped.add(rid)
            failures.append(
                {
                    "line_no": bucket["first_line"],
                    "request_id": rid,
                    "stage": "load",
                    "code": InputInvalid.code,
                    "error_type": "InputInvalid",
                    "message": f"request {rid} dropped due to invalid candidate rows",
                }
            )
            continue
        requests.append({"request_id": rid, "candidates": bucket["candidates"]})
    failures.sort(key=lambda f: (f["line_no"], f.get("request_id") or "", f["message"]))
    return requests, failures, dropped


def _safe_request_id(raw):
    import json

    try:
        obj = json.loads(raw)
        rid = obj.get("request_id")
        return rid if isinstance(rid, str) and rid else None
    except Exception:
        return None
