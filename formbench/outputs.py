from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_version_id(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version_id")
    return version if isinstance(version, str) else None


def atomic_write_versioned_json(
    path: str | Path,
    payload: dict[str, Any],
    *,
    version_id: str,
    archive_dir: str | Path | None = None,
) -> dict[str, str]:
    """Write JSON via a same-directory temp file and version-gated replacement."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["version_id"] = version_id
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        old_version = read_version_id(target)
        if target.exists() and old_version == version_id:
            tmp_path.unlink(missing_ok=True)
            return {"status": "unchanged", "path": str(target), "version_id": version_id}

        archived_to: str | None = None
        if target.exists():
            archive_path = Path(archive_dir) if archive_dir else target.parent / "archive"
            archive_path.mkdir(parents=True, exist_ok=True)
            label = old_version[:16] if old_version else "unknown"
            destination = archive_path / f"{target.stem}.{label}{target.suffix}"
            counter = 1
            while destination.exists():
                destination = archive_path / (
                    f"{target.stem}.{label}.{counter}{target.suffix}"
                )
                counter += 1
            os.replace(target, destination)
            archived_to = str(destination)

        os.replace(tmp_path, target)
        return {
            "status": "replaced",
            "path": str(target),
            "version_id": version_id,
            "archived_to": archived_to or "",
        }
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
