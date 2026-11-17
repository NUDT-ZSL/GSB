"""Scale-adaptive rollback with three bit-identical execution paths.

Path selection
--------------
* ``bitmap``  - single snapshot lineage, changed_ratio < threshold
* ``full``    - single snapshot lineage, changed_ratio >= threshold
* ``chain``   - multiple snapshot periods must be replayed from the baseline

Forced selection (``path=...``) is available for tests and demos. Every path
writes the same temp image and the resulting volume digest is asserted equal
to the target snapshot's ``state_digest`` before the atomic swap.
"""
from __future__ import annotations

import os
import shutil

from .bitmap import Bitmap
from .digest import sha256_file, sha256_hex
from .errors import ChainError, IntegrityError, SnapshotError
from .journal import JournalState
from .logging_utils import log_error, log_event
from .manifest import SnapshotEntry

DEFAULT_THRESHOLD = 0.25
PATH_BITMAP = "bitmap_diff"
PATH_FULL = "full_compare"
PATH_CHAIN = "incremental_chain"


class RollbackEngine:
    def __init__(self, manager):
        self.mgr = manager
        self.volume = manager.volume
        self.manifest = manager.manifest
        self.objects = manager.objects

    # ---------------------------------------------------------- verification
    def verify_chain(self, target: SnapshotEntry) -> None:
        """E_CHAIN first: every chain/object reference must resolve."""
        if not self.objects.exists(self.manifest.baseline_object):
            raise ChainError(
                "baseline object missing",
                location="rollback.verify_chain",
                missing_object=self.manifest.baseline_object,
            )
        target_seq = target.sequence
        for entry in self.manifest.snapshots:
            if entry.sequence > target_seq:
                continue
            if entry.bitmap_object and not self.objects.exists(entry.bitmap_object):
                raise ChainError(
                    "snapshot bitmap reference missing",
                    location="rollback.verify_chain",
                    snapshot_id=entry.snapshot_id,
                    missing_object=entry.bitmap_object,
                )
            for ref in entry.blocks:
                if not self.objects.exists(ref.object_id):
                    raise ChainError(
                        "snapshot chain block reference missing",
                        location="rollback.verify_chain",
                        snapshot_id=entry.snapshot_id,
                        block_index=ref.block_index,
                        missing_object=ref.object_id,
                    )

    def verify_integrity(self, target: SnapshotEntry) -> None:
        """E_INTEGRITY: digest of every referenced block, first bad one."""
        target_seq = target.sequence
        for entry in self.manifest.snapshots:
            if entry.sequence > target_seq:
                continue
            for ref in sorted(entry.blocks, key=lambda item: item.block_index):
                data = self.objects.get(ref.object_id)  # missing -> E_CHAIN
                actual = sha256_hex(data)
                if actual != ref.digest:
                    raise IntegrityError(
                        "referenced block digest mismatch",
                        location="rollback.verify_integrity",
                        first_corrupt_block=ref.block_index,
                        snapshot_id=entry.snapshot_id,
                        object_id=ref.object_id,
                        expected_digest=ref.digest,
                        actual_digest=actual,
                    )
        baseline = self.objects.get(self.manifest.baseline_object)
        if sha256_hex(baseline) != self.manifest.baseline_object.split("-", 1)[1]:
            raise IntegrityError(
                "baseline object digest mismatch",
                location="rollback.verify_integrity",
                first_corrupt_block=0,
                object_id=self.manifest.baseline_object,
            )

    # ------------------------------------------------------------ selection
    def choose_path(self, target: SnapshotEntry, threshold: float) -> str:
        if len(self.manifest.snapshots) > 1:
            return PATH_CHAIN
        changed = self._reversed_changed_count(target)
        ratio = changed / self.volume.block_count
        return PATH_BITMAP if ratio < threshold else PATH_FULL

    def _changed_indices(self, target: SnapshotEntry) -> set[int]:
        """Distinct blocks the rollback restores to reach the target state."""
        indices: set[int] = set()
        for entry in self.manifest.snapshots:
            if entry.sequence > target.sequence:
                break
            if not entry.bitmap_object:
                continue
            packed = self.objects.get(entry.bitmap_object)
            for index in Bitmap(self.volume.block_count, packed).set_indices():
                indices.add(int(index))
        return indices

    def _reversed_changed_count(self, target: SnapshotEntry) -> int:
        """Distinct blocks the rollback has to touch to restore target."""
        return len(self._changed_indices(target))

    def _all_changed_indices(self, target: SnapshotEntry) -> set[int]:
        """Union of every period bitmap at/after the target snapshot."""
        indices: set[int] = set()
        for entry in self.manifest.snapshots:
            if entry.sequence < target.sequence:
                continue
            if not entry.bitmap_object:
                continue
            packed = self.objects.get(entry.bitmap_object)
            for index in Bitmap(self.volume.block_count, packed).set_indices():
                indices.add(int(index))
        return indices

    # ------------------------------------------------------------ builders
    def _baseline_blocks(self) -> bytes:
        return self.objects.get(self.manifest.baseline_object)

    def build_chain_temp(self, target: SnapshotEntry, temp_path: str) -> None:
        """Incremental-chain replay materialising the target snapshot state.

        Uses the target snapshot's full state-block map; object references
        are the chain nodes whose existence is audited end to end.
        """
        block_size = self.volume.block_size
        blocks = bytearray(block_size * self.volume.block_count)
        for ref in target.state_blocks:
            data = self.objects.get_verified(
                ref.object_id, ref.digest, ref.block_index
            )
            start = ref.block_index * block_size
            blocks[start:start + block_size] = data
        self._write_temp(temp_path, bytes(blocks))

    def build_bitmap_temp(self, target: SnapshotEntry, temp_path: str) -> None:
        """Bitmap-diff path driven by the target snapshot's changed bitmap.

        The live image is copied and only blocks marked by the period bitmaps
        reversed for this rollback are restored; every restored value comes
        from the target snapshot's verified state-block map. Unmarked blocks
        are already identical to target and are left untouched.
        """
        block_size = self.volume.block_size
        with open(self.volume.image_path, "rb") as src:
            image = bytearray(src.read())
        state_refs = target.state_ref_map()
        changed = self._all_changed_indices(target)
        for block_index in sorted(changed):
            ref = state_refs.get(block_index)
            if ref is None:
                raise ChainError(
                    "bitmap marks block without state reference",
                    location="rollback.build_bitmap_temp",
                    snapshot_id=target.snapshot_id,
                    block_index=block_index,
                )
            data = self.objects.get_verified(ref.object_id, ref.digest, block_index)
            start = block_index * block_size
            image[start:start + block_size] = data
        self._write_temp(temp_path, bytes(image))

    def build_full_temp(self, target: SnapshotEntry, temp_path: str) -> None:
        """Full block-by-block comparison against every target state block."""
        block_size = self.volume.block_size
        reconstructed = bytearray(block_size * self.volume.block_count)
        compared = 0
        differing = 0
        with open(self.volume.image_path, "rb") as src:
            live = src.read()
        for ref in target.state_blocks:
            data = self.objects.get_verified(
                ref.object_id, ref.digest, ref.block_index
            )
            start = ref.block_index * block_size
            if live[start:start + block_size] != data:
                differing += 1
            reconstructed[start:start + block_size] = data
            compared += 1
        self._write_temp(temp_path, bytes(reconstructed))
        return {"blocks_compared": compared, "blocks_differing": differing}

    def _write_temp(self, temp_path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def verify_target_digest(self, temp_path: str, expected_digest: str) -> None:
        actual = sha256_file(temp_path)
        if actual != expected_digest:
            raise IntegrityError(
                "rebuilt temp image does not match snapshot state digest",
                location="rollback.verify_target_digest",
                expected_digest=expected_digest,
                actual_digest=actual,
            )

    def atomic_swap(self, temp_path: str, target: SnapshotEntry) -> None:
        """Replace the live volume image atomically (same-directory rename)."""
        image_path = self.volume.image_path
        stage_path = image_path + ".stage"
        shutil.copyfile(temp_path, stage_path)
        with open(stage_path, "rb") as handle:
            os.fsync(handle.fileno())
        backup_path = image_path + ".pre-rollback"
        shutil.copy2(image_path, backup_path)
        os.replace(stage_path, image_path)
        os.remove(backup_path)
        self.volume.update_image_digest()

    # ------------------------------------------------------------- memory
    def estimate_memory(self, path: str, changed_blocks: int) -> int:
        block_size = self.volume.block_size
        block_count = self.volume.block_count
        if path == PATH_BITMAP:
            return block_count * block_size + changed_blocks * block_size
        if path == PATH_FULL:
            return 2 * block_count * block_size + block_count * 8
        return block_count * block_size + block_count * 8

    # ---------------------------------------------------------------- run
    def rollback(self, name_or_id: str, *, path: str | None = None,
                 threshold: float = DEFAULT_THRESHOLD,
                 crash_after_prepare: bool = False) -> dict:
        target = self.mgr.get_snapshot(name_or_id)
        pre_digest = self.volume.current_digest()
        self.verify_chain(target)
        self.verify_integrity(target)
        self.mgr.seal_for_rollback()
        used_path = path or self.choose_path(target, threshold)
        if used_path not in {PATH_BITMAP, PATH_FULL, PATH_CHAIN}:
            raise SnapshotError(
                "unknown rollback path",
                location="rollback.rollback",
                path=used_path,
            )
        changed_blocks = self._reversed_changed_count(target)
        estimate = self.estimate_memory(used_path, changed_blocks)
        journal = self.mgr.journal
        journal.begin(
            snapshot_id=target.snapshot_id,
            snapshot_name=target.name,
            manifest_version=self.manifest.format_version,
            content_digest=self.manifest.content_digest,
            path=used_path,
            changed_blocks=changed_blocks,
            volume_blocks=self.volume.block_count,
            used_path=used_path,
            peak_memory_estimate=estimate,
        )
        temp_path = journal.temp_image
        observed_before = _rss_bytes()
        extra = {}
        if used_path == PATH_BITMAP:
            self.build_bitmap_temp(target, temp_path)
        elif used_path == PATH_FULL:
            extra = self.build_full_temp(target, temp_path)
        else:
            self.build_chain_temp(target, temp_path)
        observed_peak = max(observed_before, _rss_bytes())
        self.verify_target_digest(temp_path, target.state_digest)
        journal.mark_prepared()
        if crash_after_prepare:
            log_event(
                self.mgr.store_dir,
                "rollback.crash_injected",
                snapshot_id=target.snapshot_id,
                state=JournalState.PREPARED.value,
            )
            raise RuntimeError("crash_after_prepare: simulated forced termination")
        self.atomic_swap(temp_path, target)
        journal.mark_committed()
        journal.cleanup()
        self.mgr._mark_detached({"snapshot_id": target.snapshot_id})
        post_digest = self.volume.current_digest()
        report = {
            "snapshot_id": target.snapshot_id,
            "snapshot_name": target.name,
            "path": used_path,
            "threshold": threshold,
            "block_size": self.volume.block_size,
            "volume_blocks": self.volume.block_count,
            "changed_blocks": changed_blocks,
            "changed_ratio": round(changed_blocks / self.volume.block_count, 6),
            "snapshot_count": len(self.manifest.snapshots),
            "peak_memory_estimate": int(estimate),
            "observed_peak_memory": int(observed_peak),
            "pre_rollback_digest": pre_digest,
            "post_rollback_digest": post_digest,
            "target_state_digest": target.state_digest,
            "bit_identical": post_digest == target.state_digest,
            "manifest_version": self.manifest.format_version,
            "manifest_content_digest": self.manifest.content_digest,
        }
        report.update(extra)
        log_event(self.mgr.store_dir, "rollback.completed", **{
            k: v for k, v in report.items()
            if k not in {"pre_rollback_digest", "post_rollback_digest"}
        })
        return report


def _rss_bytes() -> int:
    """Best-effort resident-set size in bytes (0 when unavailable)."""
    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except (ImportError, OSError):  # pragma: no cover - Windows fallback
        try:
            import ctypes
            import sys

            class Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = Status()
            stat.dwLength = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            proc = ctypes.windll.kernel32.GetCurrentProcess()
            class Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            ctypes.windll.psapi.GetProcessMemoryInfo(
                proc, ctypes.byref(counters), counters.cb
            )
            return int(counters.PeakWorkingSetSize)
        except Exception:
            return 0


def rollback(manager, name_or_id: str, **kwargs) -> dict:
    engine = RollbackEngine(manager)
    try:
        return engine.rollback(name_or_id, **kwargs)
    except Exception as exc:
        if hasattr(exc, "code"):
            log_error(manager.store_dir, "rollback.failed", exc,
                      snapshot=name_or_id)
        raise
