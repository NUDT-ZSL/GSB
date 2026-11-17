"""增量缓存键派生与失效判定。

缓存键由请求内容指纹、相关特征指纹与参数指纹共同派生：
- 归一化统计量键：请求内容 + 归一化方式 + 定点参数 + 注入上下文
- 融合得分键：归一化码 + 权重 + 温度 + 截断 + 定点参数
- 重排折减键：融合分 + 归一化码 + 分组表 + 多样性参数 + 阈值
"""

from .fingerprint import content_fingerprint, fingerprint


def normalization_key(request, config, spec_dict, injection_token):
    payload = {
        "content": content_fingerprint(request),
        "normalization": config["normalization"],
        "spec": spec_dict,
        "injection": injection_token,
    }
    return fingerprint(payload, "cache-norm")


def fusion_key(norm_result, config, spec_dict, injection_token):
    payload = {
        "codes": norm_result["codes"],
        "weights": config["weights"],
        "temperature": config["temperature"],
        "score_clip": config["score_clip"],
        "spec": spec_dict,
        "injection": injection_token,
    }
    return fingerprint(payload, "cache-fuse")


def rerank_key(fused, norm_result, config, groups_hash_value, spec_dict, threshold_override,
               injection_token):
    payload = {
        "scores": [f["score"] for f in fused],
        "codes": norm_result["codes"],
        "groups_hash": groups_hash_value,
        "diversity_penalty": config["diversity_penalty"],
        "rerank_window": config["rerank_window"],
        "spec": spec_dict,
        "threshold_override": threshold_override,
        "injection": injection_token,
    }
    return fingerprint(payload, "cache-rerank")


class CacheBook:
    """记录每次运行每个请求各阶段的命中/失效/重算，供报告输出。"""

    def __init__(self):
        self.entries = {}
        self.totals = {"hits": 0, "misses": 0, "invalidations": 0, "recomputes": 0}

    def record(self, request_id, stage, status, key):
        self.entries.setdefault(request_id, []).append(
            {"stage": stage, "status": status, "cache_key": key}
        )
        if status == "hit":
            self.totals["hits"] += 1
        elif status == "miss":
            self.totals["misses"] += 1
        elif status == "recompute":
            self.totals["recomputes"] += 1
        elif status == "invalidated":
            self.totals["invalidations"] += 1

    def for_request(self, request_id):
        return self.entries.get(request_id, [])
