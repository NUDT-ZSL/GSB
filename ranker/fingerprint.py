"""稳定指纹工具：排序键、内容指纹与参数指纹。"""

import hashlib
import json


def stable_key(request_id, item_id):
    """候选任务的稳定排序键，由请求 ID 与候选物品 ID 派生。"""
    return f"{request_id}\u0000{item_id}"


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(obj, prefix=""):
    payload = canonical_json(obj)
    digest = hashlib.sha256((prefix + ":" + payload).encode("utf-8")).hexdigest()
    return digest[:16]


def content_fingerprint(request):
    """请求内容指纹：参与候选、全部特征与召回分数。"""
    items = []
    for c in request["candidates"]:
        if "dense_map" in c:
            entry = {
                "item_id": c["item_id"],
                "scores": c["scores"],
                "dense": c["dense_map"],
                "sparse": c["sparse"],
            }
        else:
            entry = {
                "item_id": c["item_id"],
                "scores": c["scores"],
                "dense": c.get("dense", []),
                "sparse": c.get("sparse", {}),
            }
        items.append(entry)
    payload = {"request_id": request["request_id"], "items": items}
    return fingerprint(payload, "content")


def feature_fingerprint(feature_vector):
    return fingerprint(feature_vector, "feature")
