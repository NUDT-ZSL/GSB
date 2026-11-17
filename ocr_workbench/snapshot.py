"""Atomic, whole-replacement stage snapshots and crash recovery.

Each snapshot is written to a temporary file in the same directory and then
committed with ``os.replace`` (atomic on POSIX and Windows).  A new global
version always replaces the previous snapshot file as a whole; partial or
interrupted writes leave the previous committed snapshot untouched, so crash
recovery only ever observes fully-replaced snapshots.
"""

import json
import os
import tempfile

from .errors import E_SNAPSHOT_CORRUPT, WorkbenchError
from .font import FONT_VERSION

SNAPSHOT_NAME = "snapshot.json"
PROGRESS_NAME = "progress.json"


def _atomic_write_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _summarize(lines):
    summary = []
    for line in lines:
        summary.append({
            "bbox": line["bbox"],
            "text": line["text"],
            "char_count": len(line["chars"]),
        })
    return summary


class SnapshotStore:
    def __init__(self, workdir):
        self.workdir = workdir
        self.snapshot_path = os.path.join(workdir, SNAPSHOT_NAME)
        self.progress_path = os.path.join(workdir, PROGRESS_NAME)

    def commit(self, version, config_bundle, completed, image_results,
               stage_cache_keys):
        """Atomically replace the snapshot for a new global version."""
        det_hash = config_bundle.detection_fingerprint()
        rec_hash = config_bundle.recognition_fingerprint()
        images = []
        for result in image_results:
            images.append({
                "name": result.name,
                "image_fingerprint": result.fingerprint,
                "reused_stages": list(result.reused_stages),
                "recomputed_stages": list(result.recomputed_stages),
                "cache_keys": stage_cache_keys.get(result.name, {}),
                "products_summary": _summarize(result.lines),
            })
        snapshot = {
            "snapshot_version": int(version),
            "global_version": int(version),
            "font_version": FONT_VERSION,
            "config_hash": {
                "detection": det_hash,
                "recognition": rec_hash,
            },
            "completed_images": list(completed),
            "images": images,
        }
        _atomic_write_json(self.snapshot_path, snapshot)
        progress = {
            "global_version": int(version),
            "completed_images": list(completed),
            "font_version": FONT_VERSION,
        }
        _atomic_write_json(self.progress_path, progress)
        return snapshot

    def load(self):
        if not os.path.exists(self.snapshot_path):
            return None
        try:
            with open(self.snapshot_path, "r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
            required = ("snapshot_version", "config_hash",
                        "completed_images", "images", "font_version")
            for key in required:
                if key not in snapshot:
                    raise WorkbenchError(E_SNAPSHOT_CORRUPT,
                                         f"missing field {key}")
            if snapshot["font_version"] != FONT_VERSION:
                raise WorkbenchError(
                    E_SNAPSHOT_CORRUPT,
                    f"font {snapshot['font_version']} != {FONT_VERSION}")
            return snapshot
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkbenchError(E_SNAPSHOT_CORRUPT, str(exc))

    def load_progress(self):
        if not os.path.exists(self.progress_path):
            return None
        with open(self.progress_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
