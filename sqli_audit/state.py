"""断点续扫状态管理（任务第 10 条）。

状态文件只保存确定性内容：目标指纹、基线、各探测/确认结果。
不记录时间、耗时、随机数，因此状态文件本身也是可复现的。
恢复重跑时：
- 已完成项直接复用，不再发请求、不重复计数；
- 仅未完成项重新探测，补齐结论。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import TargetConfig


def target_fingerprint(cfg: TargetConfig) -> str:
    """根据目标描述生成稳定指纹，参数/载荷变化会使旧状态自动失效。"""
    from .payloads import CONFIRMATIONS, PROBES

    material = {
        "url": cfg.url,
        "method": cfg.method,
        "baseline": cfg.baseline,
        "params": [(p.name, p.location, p.type) for p in cfg.params],
        "probes": [p.payload_id for p in PROBES],
        "confirmations": [c.payload_id for c in CONFIRMATIONS],
        "data_path": cfg.data_path,
        "order_id_field": cfg.order_id_field,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ScanState:
    def __init__(self, target_fp: str):
        self.target_fp = target_fp
        self.baseline: dict | None = None
        # key 形如 "param:order_no/P01_TAUTOLOGY" / "param:order_no/confirm/C01_BOOL_TRUE"
        self.results: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path, expected_fp: str) -> "ScanState":
        p = Path(path)
        state = cls(expected_fp)
        if not p.exists():
            return state
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 状态文件损坏时安全降级为全新扫描
            return state
        if not isinstance(raw, dict) or raw.get("target_fingerprint") != expected_fp:
            # 目标描述或载荷集合已变化，旧状态不可复用
            return state
        if isinstance(raw.get("baseline"), dict):
            state.baseline = raw["baseline"]
        if isinstance(raw.get("results"), dict):
            state.results = {
                str(k): v for k, v in raw["results"].items() if isinstance(v, dict)
            }
        return state

    def save(self, path: str | Path) -> None:
        payload = {
            "target_fingerprint": self.target_fp,
            "baseline": self.baseline,
            # 键排序保证状态文件字节稳定
            "results": {k: self.results[k] for k in sorted(self.results)},
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def probe_key(param_name: str, payload_id: str) -> str:
        return f"param:{param_name}/{payload_id}"

    @staticmethod
    def confirm_key(param_name: str, payload_id: str) -> str:
        return f"param:{param_name}/confirm/{payload_id}"

    def get(self, key: str) -> dict | None:
        return self.results.get(key)

    def put(self, key: str, value: dict) -> None:
        self.results[key] = value
