"""异常注入：仅作用于已解析的输入数据或已持久化状态文件。

注入清单是普通 JSON，注入动作不修改任何源码；
非法振幅 / 网格不可整除 / 缺失通道都通过改写材质或配置数据触发，
损坏摘要通过改写状态目录里的摘要 JSON 触发。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ErrorCode


@dataclass(frozen=True)
class Injection:
    injection_id: str
    type: str
    material_id: str
    description: str


def load_injections(path: str | Path) -> list[Injection]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("injections", payload if isinstance(payload, list) else [])
    return [
        Injection(
            injection_id=item["injection_id"],
            type=item["type"],
            material_id=item["material_id"],
            description=item.get("description", ""),
        )
        for item in items
    ]


def apply_input_injections(material_payload: dict, config_payload: dict, injections: list[Injection]):
    """把数据类注入应用到内存中的清单/配置字典，返回实际生效记录。"""
    materials = {item["material_id"]: item for item in material_payload["materials"]}
    applied: list[dict] = []

    for injection in injections:
        record = injection.__dict__.copy()
        target = materials.get(injection.material_id)
        if injection.type == "illegal_amplitude":
            if target is not None:
                target["amplitudes"] = list(target.get("amplitudes", []))
                if target["amplitudes"]:
                    target["amplitudes"][-1] = -1.0
                record["expected_code"] = ErrorCode.E_AMPLITUDE_INVALID
        elif injection.type == "grid_indivisible":
            if target is not None:
                target["grid_rows"] = target["height"] + 1
                target["grid_cols"] = target["width"] + 1
                record["expected_code"] = ErrorCode.E_GRID_DIVISIBILITY
        elif injection.type == "missing_channel":
            if target is not None:
                target["channels"] = [name for name in target.get("channels", []) if name != "normal"]
                record["expected_code"] = ErrorCode.E_CHANNEL_MISSING
        elif injection.type == "corrupt_chunk_digest":
            # 状态类注入不在此处理，由 corrupt_persisted_chunk 针对状态目录执行。
            record["expected_code"] = ErrorCode.E_CHUNK_DIGEST_MISMATCH
            record["state_based"] = True
        else:
            record["expected_code"] = ErrorCode.E_UNKNOWN
        applied.append(record)
    return applied


def corrupt_persisted_chunk(state_root: str | Path, material_id: str, chunk_index: int) -> Path:
    """损坏指定已持久化块的摘要字段，模拟崩溃/篡改后的坏状态。"""
    meta_path = Path(state_root) / material_id / f"chunk_{chunk_index:04d}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["digest"] = "0" * len(meta.get("digest", "0"))
    tmp = meta_path.with_name(meta_path.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(meta_path)
    return meta_path
