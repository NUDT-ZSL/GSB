"""分块级持久化、版本识别与原子写出。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np

from . import errors as E
from .config import Spec
from .reconstruction import BlockId, BlockResult, ContributionRecord
from .scene import Scene

VERSION_FILE = "version.json"
INTERRUPT_FILE = "interrupted.json"
BLOCK_DIR = "blocks"
OUTPUT_META_SUFFIX = ".meta.json"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_version_id(scene: Scene, dry_signals: list[np.ndarray], seed_override: int | None = None) -> str:
    """版本标识 = 规格 + 场景参数 + 干信号内容 + 种子 的规范化 SHA-256。"""

    payload = {
        "spec": Spec.canonical(),
        "scene": json.loads(scene.canonical_json().decode("utf-8")),
        "dry_signals": [
            {
                "source_id": scene.sources[idx].id,
                "sha256_q24": hashlib.sha256(
                    np.asarray(dry, dtype=np.float64).tobytes()
                ).hexdigest(),
                "samples": int(dry.shape[0]),
            }
            for idx, dry in enumerate(dry_signals)
        ],
        "seed": int(seed_override if seed_override is not None else scene.seed),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace 整体替换。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise E.BusinessError(E.E_STATE, f"原子写出失败: {target}", {"path": str(exc)}) from exc


def atomic_write_json(target: Path, obj: dict) -> None:
    atomic_write_bytes(target, json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))


def atomic_write_text(target: Path, text: str) -> None:
    atomic_write_bytes(target, text.encode("utf-8"))


def reset_state_dir(state_dir: Path) -> None:
    """运行前清空历史状态，避免版本标识混淆。"""

    if state_dir.exists():
        shutil.rmtree(state_dir)
    (state_dir / BLOCK_DIR).mkdir(parents=True, exist_ok=True)


def prepare_state(state_dir: Path, version_id: str, *, clean: bool) -> dict[str, dict]:
    """启动时核对版本；不一致则整体清空，禁止新旧版本冲激响应混用。

    返回已完成分块的索引 ``{block_name: meta}``。
    """

    version_path = state_dir / VERSION_FILE
    if clean:
        reset_state_dir(state_dir)
    elif not state_dir.exists():
        (state_dir / BLOCK_DIR).mkdir(parents=True, exist_ok=True)
    elif version_path.exists():
        try:
            saved = json.loads(version_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise E.BusinessError(E.E_STATE, "状态版本文件损坏", {"path": str(version_path)}) from exc
        if saved.get("version_id") != version_id:
            reset_state_dir(state_dir)
    else:
        # 没有版本文件的残缺状态，按未知版本处理。
        reset_state_dir(state_dir)

    atomic_write_json(version_path, {"version_id": version_id, "format": 1})
    interrupt = state_dir / INTERRUPT_FILE
    if interrupt.exists():
        interrupt.unlink()

    completed: dict[str, dict] = {}
    block_dir = state_dir / BLOCK_DIR
    for npz_path in sorted(block_dir.glob("*.npz")):
        meta_path = block_dir / f"{npz_path.stem}.json"
        if not meta_path.exists():
            # 只有数据没有元数据，视为未完成，删除以免被误认。
            npz_path.unlink(missing_ok=True)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            npz_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            continue
        if meta.get("version_id") != version_id:
            npz_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            continue
        completed[npz_path.stem] = meta
    return completed


def _record_to_dict(rec: ContributionRecord) -> dict:
    return {
        "image_index": rec.image_index,
        "order": rec.order,
        "band": rec.band,
        "speaker": rec.speaker,
        "sample": rec.sample,
        "fixed_value": rec.fixed_value,
        "float_value": rec.float_value,
        "fixed_contribution": rec.fixed_contribution,
        "float_contribution": rec.float_contribution,
        "residual_contribution": rec.residual_contribution,
    }


def save_block(state_dir: Path, version_id: str, result: BlockResult) -> Path:
    """分块完成后原子写入数据与元数据（先临时文件再 replace）。"""

    block_dir = state_dir / BLOCK_DIR
    name = result.block.name()
    npz_path = block_dir / f"{name}.npz"
    meta_path = block_dir / f"{name}.json"
    payload = {
        "ir_q": result.ir_q,
        "ir_f": result.ir_f,
        "records": np.asarray(
            [_record_to_dict(rec) for rec in result.records],
            dtype=object,
        ),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp.npz", dir=str(block_dir))
    os.close(fd)
    buffer = Path(tmp_name)
    try:
        with open(buffer, "wb") as fh:
            np.savez_compressed(fh, **payload)
        os.replace(buffer, npz_path)
    except BaseException:
        buffer.unlink(missing_ok=True)
        raise
    meta = {
        "version_id": version_id,
        "block": result.block.__dict__,
        "block_energy_fixed": result.block_energy_q,
        "block_energy_float64": result.block_energy_f,
        "record_count": len(result.records),
        "first_energy_violation": result.first_energy_violation,
        "image_summaries": result.image_summaries,
    }
    atomic_write_json(meta_path, meta)
    return npz_path


def load_block(state_dir: Path, block: BlockId) -> BlockResult:
    name = block.name()
    block_dir = state_dir / BLOCK_DIR
    meta = json.loads((block_dir / f"{name}.json").read_text(encoding="utf-8"))
    bundle = np.load(block_dir / f"{name}.npz", allow_pickle=True)
    raw_records = bundle["records"]
    records = [
        ContributionRecord(
            image_index=int(item["image_index"]),
            order=int(item["order"]),
            band=int(item["band"]),
            speaker=int(item["speaker"]),
            sample=int(item["sample"]),
            fixed_value=int(item["fixed_value"]),
            float_value=float(item["float_value"]),
            fixed_contribution=int(item["fixed_contribution"]),
            float_contribution=float(item["float_contribution"]),
            residual_contribution=float(item["residual_contribution"]),
        )
        for item in raw_records
    ]
    return BlockResult(
        block=block,
        ir_q=bundle["ir_q"],
        ir_f=bundle["ir_f"],
        records=records,
        image_summaries=meta["image_summaries"],
        first_energy_violation=meta.get("first_energy_violation"),
        block_energy_q=int(meta["block_energy_fixed"]),
        block_energy_f=float(meta["block_energy_float64"]),
    )


def write_interrupt_marker(state_dir: Path, version_id: str, completed: list[str], reason: str) -> None:
    """可处理终止信号：在分块边界写中断标记。未完成块从未写过完成元数据。"""

    atomic_write_json(
        state_dir / INTERRUPT_FILE,
        {"version_id": version_id, "completed_blocks": sorted(completed), "reason": reason},
    )


def read_interrupt_marker(state_dir: Path) -> dict | None:
    path = state_dir / INTERRUPT_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def output_is_current(meta_path: Path, version_id: str, sample_rate: int, content_sha: str) -> bool:
    """读取已有输出的采样率与内容摘要版本信息，决定是否需要替换。"""

    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        meta.get("version_id") == version_id
        and meta.get("sample_rate") == sample_rate
        and meta.get("content_sha256") == content_sha
    )


def write_output_meta(meta_path: Path, payload: dict) -> None:
    atomic_write_json(meta_path, payload)
