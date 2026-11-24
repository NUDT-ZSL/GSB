"""生成固定种子样本：材质清单、生成配置与异常注入清单。"""

from __future__ import annotations

import json
from pathlib import Path


def _material(material_id: str, seed: int, width: int, height: int, grid_rows: int, grid_cols: int) -> dict:
    palette = {
        "mat_brick": [0.62, 0.28, 0.20],
        "mat_metal": [0.55, 0.58, 0.62],
        "mat_stone": [0.48, 0.46, 0.42],
        "mat_fabric": [0.30, 0.42, 0.55],
    }
    return {
        "material_id": material_id,
        "seed": seed,
        "width": width,
        "height": height,
        "base_color": palette[material_id],
        "roughness": round(0.35 + (seed % 7) * 0.04, 6),
        "normal_strength": round(1.2 + seed * 0.15, 6),
        "octaves": 4,
        "frequencies": [1.0 / 32.0, 1.0 / 16.0, 1.0 / 8.0, 1.0 / 4.0],
        "amplitudes": [0.5, 0.25, 0.125, 0.0625],
        "channels": ["color", "roughness", "normal", "height"],
        "grid_rows": grid_rows,
        "grid_cols": grid_cols,
    }


def generate_samples(sample_dir: str | Path) -> dict:
    sample_dir = Path(sample_dir)
    sample_dir.mkdir(parents=True, exist_ok=True)

    materials = {
        "materials": [
            _material("mat_brick", seed=11, width=64, height=48, grid_rows=2, grid_cols=2),
            _material("mat_metal", seed=23, width=64, height=64, grid_rows=2, grid_cols=4),
            _material("mat_stone", seed=37, width=96, height=64, grid_rows=2, grid_cols=3),
            _material("mat_fabric", seed=51, width=48, height=48, grid_rows=3, grid_cols=3),
        ]
    }
    config = {
        "grid_rows": 2,
        "grid_cols": 2,
        "concurrency": 2,
        "memory_budget_bytes": 8 * 1024 * 1024,
        "overlap": 2,
        "tolerance": 1e-6,
        "dtype": "float32",
        "output_dir": "build/output",
        "state_dir": "build/state",
        "archive_dir": "build/archive",
    }
    injections = {
        "injections": [
            {
                "injection_id": "INJ_AMPLITUDE",
                "type": "illegal_amplitude",
                "material_id": "mat_brick",
                "description": "将最后一个倍频振幅改为负数，触发 E_AMPLITUDE_INVALID",
            },
            {
                "injection_id": "INJ_GRID",
                "type": "grid_indivisible",
                "material_id": "mat_metal",
                "description": "令分块行列大于纹理尺寸且不可整除，触发 E_GRID_DIVISIBILITY",
            },
            {
                "injection_id": "INJ_CHANNEL",
                "type": "missing_channel",
                "material_id": "mat_stone",
                "description": "移除 normal 通道声明，触发 E_CHANNEL_MISSING",
            },
            {
                "injection_id": "INJ_DIGEST",
                "type": "corrupt_chunk_digest",
                "material_id": "mat_fabric",
                "description": "损坏已持久化块摘要，触发 E_CHUNK_DIGEST_MISMATCH",
            },
        ]
    }

    paths = {
        "materials": sample_dir / "materials.json",
        "config": sample_dir / "config.json",
        "injections": sample_dir / "injections.json",
    }
    paths["materials"].write_text(json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["config"].write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["injections"].write_text(json.dumps(injections, ensure_ascii=False, indent=2), encoding="utf-8")
    return {name: str(path) for name, path in paths.items()}
