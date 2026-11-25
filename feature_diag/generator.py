from __future__ import annotations

from pathlib import Path
from typing import Any

from .sample import write_samples
from .snapshot import load_v1, migrate_v1_to_v2


def prepare_workspace(root: Path, *, seed: int = 20261126, count: int = 32) -> dict[str, Any]:
    root = Path(root)
    data_dir = root / "data"
    configs_dir = root / "configs"
    v1_path = configs_dir / "v1.json"
    data_path, v1_path = write_samples(
        data_dir,
        seed=seed,
        count=count,
        config_path=v1_path,
    )
    v1_config = load_v1(v1_path, require_checksum=True)
    v2_dir = configs_dir / "v2"
    v2_config, migration = migrate_v1_to_v2(
        v1_path,
        v2_dir,
        target_version=2,
        checksum=None,
    )
    return {
        "data_path": str(data_path.relative_to(root)),
        "v1_path": str(v1_path.relative_to(root)),
        "v2_path": str(v2_dir.relative_to(root)),
        "seed": seed,
        "row_count": count,
        "v1_snapshot_version": v1_config.snapshot_version,
        "v2_snapshot_version": v2_config.snapshot_version,
        "v1_checksum": v1_path.with_name(v1_path.name + ".sha256").read_text(encoding="utf-8").strip(),
        "migration": migration,
    }
