"""Copy-on-write snapshot manager: the domain entry point."""
from __future__ import annotations

import json
import os
import shutil

from .bitmap import Bitmap, bitmap_byte_length
from .digest import sha256_hex, sha256_file
from .errors import ChainError, SnapshotError, VolumeError
from .journal import Journal, JournalState
from .logging_utils import log_error, log_event
from .manifest import (
    MANIFEST_FILENAME,
    MANIFEST_BACKUP_SUFFIX,
    BlockRef,
    Manifest,
    SnapshotEntry,
    load_manifest,
    save_manifest,
)
from .objectstore import ObjectStore
from .volume import Volume

LIVE_BITMAP_NAME = "live.bitmap"
STATE_FILENAME = "state.json"


class SnapshotManager:
    """Owns one volume image plus its snapshot store directory."""

    def __init__(self, volume: Volume, store_dir: str, *, auto_recover: bool = True):
        self.volume = volume
        self.store_dir = os.path.abspath(store_dir)
        os.makedirs(self.store_dir, exist_ok=True)
        self.objects = ObjectStore(self.store_dir)
        self.bitmap_path = os.path.join(self.store_dir, LIVE_BITMAP_NAME)
        self.state_path = os.path.join(self.store_dir, STATE_FILENAME)
        self.journal = Journal(self.store_dir)
        self.live_bitmap = Bitmap(volume.block_count)
        self._detached = False
        self._load_or_init_store()
        if auto_recover:
            self.recover_pending()

    # ---------------------------------------------------------------- setup
    def _load_or_init_store(self) -> None:
        manifest_path = os.path.join(self.store_dir, MANIFEST_FILENAME)
        if os.path.exists(manifest_path):
            self.manifest = load_manifest(self.store_dir)
            self._check_geometry()
            if os.path.exists(self.bitmap_path):
                self.live_bitmap = Bitmap.load(
                    self.volume.block_count, self.bitmap_path
                )
            else:
                self.live_bitmap = Bitmap(self.volume.block_count)
        else:
            image_bytes = self.volume.read_all()
            baseline_id, _ = self.objects.put(image_bytes)
            self.manifest = Manifest(
                format_version=1,
                sequence=0,
                block_size=self.volume.block_size,
                block_count=self.volume.block_count,
                baseline_object=baseline_id,
                snapshots=[],
            )
            save_manifest(self.store_dir, self.manifest)
            self.live_bitmap = Bitmap(self.volume.block_count)
            self.live_bitmap.save(self.bitmap_path)
        self._detached = self._read_state().get("detached", False)

    def _check_geometry(self) -> None:
        if self.manifest.block_size != self.volume.block_size:
            raise SnapshotError(
                "manifest block_size does not match volume",
                location="manager._check_geometry",
                manifest_block_size=self.manifest.block_size,
                volume_block_size=self.volume.block_size,
            )
        if self.manifest.block_count != self.volume.block_count:
            raise SnapshotError(
                "manifest block_count does not match volume",
                location="manager._check_geometry",
                manifest_block_count=self.manifest.block_count,
                volume_block_count=self.volume.block_count,
            )

    def _read_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        with open(self.state_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_state(self, **fields) -> None:
        state = self._read_state()
        state.update(fields)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)

    @property
    def detached(self) -> bool:
        return self._detached

    def close(self) -> None:
        self.volume.close()

    def __enter__(self) -> "SnapshotManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------------------------------------------------------------- writes
    def _cow_write(self, block_index: int, data: bytes) -> None:
        if self._detached or not self.manifest.snapshots:
            self.volume.write_block(block_index, data)
            self.volume.flush()
            return
        if not self.live_bitmap.is_set(block_index):
            before = self.volume.read_block(block_index)
            object_id, digest = self.objects.put(before)
            entry = self.manifest.snapshots[-1]
            entry.blocks.append(
                BlockRef(
                    block_index=block_index,
                    object_id=object_id,
                    digest=digest,
                    length=len(before),
                )
            )
            self.live_bitmap.mark(block_index)
            self._persist_live_period(entry)
        self.volume.write_block(block_index, data)
        self.volume.flush()

    def write_block(self, block_index: int, data: bytes) -> None:
        try:
            self.volume._check_index(block_index)
            if not isinstance(data, bytes) or len(data) != self.volume.block_size:
                raise VolumeError(
                    "block data must be exactly block_size bytes",
                    location="manager.write_block",
                    block_index=block_index,
                )
            self._cow_write(block_index, data)
        except VolumeError as exc:
            log_error(self.store_dir, "volume.write_rejected", exc)
            raise

    def inject_hex(self, block_index: int, hex_string: str) -> None:
        try:
            block = self.volume.hex_to_block(block_index, hex_string)
        except VolumeError as exc:
            log_error(self.store_dir, "volume.inject_rejected", exc)
            raise
        self._cow_write(block_index, block)

    # ---------------------------------------------------------------- snapshots
    def create_snapshot(self, name: str) -> SnapshotEntry:
        if not isinstance(name, str) or not name.strip():
            raise SnapshotError(
                "snapshot name must be a non-empty string",
                location="manager.create_snapshot",
                name=repr(name),
            )
        if self._detached:
            raise SnapshotError(
                "cannot snapshot a detached (post-rollback) lineage; "
                "rebuild a store on the restored volume",
                location="manager.create_snapshot",
            )
        if name in self.manifest.names():
            exc = SnapshotError(
                "snapshot name already exists",
                location="manager.create_snapshot",
                name=name,
            )
            log_error(self.store_dir, "snapshot.create_rejected", exc)
            raise exc
        if self.journal.exists():
            raise SnapshotError(
                "an unfinished rollback journal blocks new snapshots",
                location="manager.create_snapshot",
            )
        self.volume.flush()
        state_digest = self.volume.current_digest()
        if self.manifest.snapshots:
            self._seal_live_period()
            self.live_bitmap.clear()
            self.live_bitmap.save(self.bitmap_path)
        state_blocks = self._capture_state_blocks()
        seq = self.manifest.sequence + 1
        entry = SnapshotEntry(
            snapshot_id=f"snap-{seq:04d}-{sha256_hex(name.encode('utf-8'))[:8]}",
            name=name,
            sequence=seq,
            created_volume_version=self.volume.volume_version + 1,
            bitmap_object="",
            bitmap_length=bitmap_byte_length(self.volume.block_count),
            changed_count=0,
            blocks=[],
            state_digest=state_digest,
            parent_snapshot_id=(
                self.manifest.snapshots[-1].snapshot_id
                if self.manifest.snapshots
                else None
            ),
            state_blocks=state_blocks,
        )
        self.manifest.snapshots.append(entry)
        self.manifest.sequence = seq
        save_manifest(self.store_dir, self.manifest)
        new_version = self.volume.increment_version()
        entry.created_volume_version = new_version
        save_manifest(self.store_dir, self.manifest)
        log_event(
            self.store_dir,
            "snapshot.created",
            snapshot_id=entry.snapshot_id,
            snapshot_name=name,
            sequence=seq,
            volume_version=new_version,
            state_digest=state_digest,
        )
        return entry

    def _persist_live_period(self, entry: SnapshotEntry) -> None:
        """Persist live bitmap + changed count onto the open period entry."""
        packed = self.live_bitmap.to_bytes()
        bitmap_id, _ = self.objects.put(packed)
        entry.bitmap_object = bitmap_id
        entry.bitmap_length = len(packed)
        entry.changed_count = self.live_bitmap.changed_count()
        self.live_bitmap.save(self.bitmap_path)
        save_manifest(self.store_dir, self.manifest)

    def _seal_live_period(self) -> SnapshotEntry | None:
        """Seal the open period. Detached lineages keep stored bitmaps intact."""
        if self._detached or not self.manifest.snapshots:
            return None
        entry = self.manifest.snapshots[-1]
        self._persist_live_period(entry)
        return entry

    def _capture_state_blocks(self) -> list[BlockRef]:
        """Content-addressed full block map at the snapshot boundary."""
        refs = []
        for index in range(self.volume.block_count):
            data = self.volume.read_block(index)
            object_id, digest = self.objects.put(data)
            refs.append(BlockRef(
                block_index=index,
                object_id=object_id,
                digest=digest,
                length=len(data),
            ))
        return refs

    def list_snapshots(self) -> list[SnapshotEntry]:
        return list(self.manifest.snapshots)

    def get_snapshot(self, name_or_id: str) -> SnapshotEntry:
        for entry in self.manifest.snapshots:
            if entry.name == name_or_id or entry.snapshot_id == name_or_id:
                return entry
        raise SnapshotError(
            "snapshot not found",
            location="manager.get_snapshot",
            name_or_id=name_or_id,
            known_names=self.manifest.names(),
        )

    # ------------------------------------------------------------- recovery
    def recover_pending(self) -> dict | None:
        """Recover or abort an interrupted rollback using the journal.

        Returns the journal record if a rollback was resumed, else None.
        PREPARED + valid temp image + matching manifest -> complete the swap.
        Any inconsistency -> abort, leaving the live volume untouched.
        """
        if not self.journal.exists():
            return None
        record = self.journal.read()
        if record.get("state") == JournalState.COMMITTED.value:
            self.journal.cleanup()
            log_event(self.store_dir, "rollback.recovered_committed", **{
                k: record.get(k)
                for k in ("snapshot_id", "snapshot_name", "manifest_version")
            })
            self._mark_detached(record)
            return record
        result = self._attempt_resume(record)
        return result

    def _attempt_resume(self, record: dict) -> dict | None:
        snapshot_id = record.get("snapshot_id")
        if record.get("manifest_content_digest") != self.manifest.content_digest:
            return self._abort_journal(
                record, "manifest content changed while rollback was interrupted"
            )
        if record.get("manifest_version") != self.manifest.format_version:
            return self._abort_journal(
                record, "manifest format version changed while rollback interrupted"
            )
        try:
            entry = self.manifest.get_by_id(snapshot_id)
        except SnapshotError:
            return self._abort_journal(record, "target snapshot missing on restart")
        temp_path = self.journal.temp_image
        if not os.path.exists(temp_path):
            return self._abort_journal(record, "temp rollback image missing")
        expected_len = self.volume.block_size * self.volume.block_count
        if os.path.getsize(temp_path) != expected_len:
            return self._abort_journal(record, "temp rollback image truncated")
        from .rollback import RollbackEngine

        engine = RollbackEngine(self)
        try:
            engine.verify_target_digest(temp_path, entry.state_digest)
        except Exception as exc:  # noqa: BLE001 - recovery must not crash
            log_error(self.store_dir, "rollback.recovery_abort", exc,
                      snapshot_id=snapshot_id)
            return self._abort_journal(record, f"temp image verification failed: {exc}")
        engine.atomic_swap(temp_path, entry)
        self.journal.mark_committed()
        self.journal.cleanup()
        self._mark_detached(record)
        log_event(
            self.store_dir,
            "rollback.recovered_completed",
            code=None,
            snapshot_id=snapshot_id,
            snapshot_name=entry.name,
        )
        return record

    def _abort_journal(self, record: dict, reason: str) -> dict:
        self.journal.cleanup()
        log_event(
            self.store_dir,
            "rollback.recovery_aborted",
            code="E_SNAPSHOT",
            reason=reason,
            snapshot_id=record.get("snapshot_id"),
        )
        return {"aborted": True, "reason": reason, "record": record}

    def _mark_detached(self, record: dict) -> None:
        self._detached = True
        self._write_state(detached=True, last_rollback=record.get("snapshot_id"))
        self.live_bitmap = Bitmap(self.volume.block_count)
        self.live_bitmap.save(self.bitmap_path)

    # ----------------------------------------------------- whole replacement
    def replace_manifest(self, new_manifest: Manifest,
                         bundle_objects: dict[str, bytes] | None = None) -> dict:
        """Replace the whole manifest; never merge old and new entries.

        A format-version change OR a content-digest difference triggers the
        replacement. The old manifest is kept as a read-only backup and the
        snapshot chain (parent links) is rebuilt deterministically afterwards.
        """
        if not isinstance(new_manifest, Manifest):
            raise SnapshotError(
                "replacement must be a Manifest instance",
                location="manager.replace_manifest",
            )
        if new_manifest.block_size != self.volume.block_size:
            raise SnapshotError(
                "replacement manifest block_size mismatch",
                location="manager.replace_manifest",
                manifest_block_size=new_manifest.block_size,
                volume_block_size=self.volume.block_size,
            )
        if new_manifest.block_count != self.volume.block_count:
            raise SnapshotError(
                "replacement manifest block_count mismatch",
                location="manager.replace_manifest",
                manifest_block_count=new_manifest.block_count,
                volume_block_count=self.volume.block_count,
            )
        old = self.manifest
        old_digest = old.content_digest
        new_digest = new_manifest.compute_content_digest()
        version_changed = new_manifest.format_version != old.format_version
        digest_changed = new_digest != old_digest
        if not (version_changed or digest_changed):
            return {
                "replaced": False,
                "reason": "no version or content difference",
                "old_format_version": old.format_version,
                "new_format_version": new_manifest.format_version,
                "affected_snapshots": [],
            }
        if self.journal.exists():
            raise SnapshotError(
                "an unfinished rollback journal blocks manifest replacement",
                location="manager.replace_manifest",
            )

        staged = dict(bundle_objects or {})
        for entry in new_manifest.snapshots:
            for ref in entry.blocks:
                if not self.objects.exists(ref.object_id):
                    if ref.object_id not in staged:
                        raise ChainError(
                            "replacement bundle missing referenced block object",
                            location="manager.replace_manifest",
                            snapshot_id=entry.snapshot_id,
                            missing_object=ref.object_id,
                            block_index=ref.block_index,
                        )
            if entry.bitmap_object and not self.objects.exists(entry.bitmap_object):
                if entry.bitmap_object not in staged:
                    raise ChainError(
                        "replacement bundle missing bitmap object",
                        location="manager.replace_manifest",
                        snapshot_id=entry.snapshot_id,
                        missing_object=entry.bitmap_object,
                    )
        if not self.objects.exists(new_manifest.baseline_object):
            if new_manifest.baseline_object not in staged:
                raise ChainError(
                    "replacement bundle missing baseline object",
                    location="manager.replace_manifest",
                    missing_object=new_manifest.baseline_object,
                )
        for object_id, data in staged.items():
            self.objects.put(data)

        self._rebuild_chain_links(new_manifest)
        backup_path = os.path.join(
            self.store_dir, MANIFEST_FILENAME + MANIFEST_BACKUP_SUFFIX
        )
        if os.path.exists(backup_path):
            os.remove(backup_path)
        shutil.copy2(os.path.join(self.store_dir, MANIFEST_FILENAME), backup_path)
        try:
            os.chmod(backup_path, 0o444)
        except OSError:  # pragma: no cover - Windows read-only semantics
            os.chmod(backup_path, 0o444)

        new_manifest.sequence = len(new_manifest.snapshots)
        save_manifest(self.store_dir, new_manifest)
        self.manifest = new_manifest
        self.live_bitmap = Bitmap(self.volume.block_count)
        self.live_bitmap.save(self.bitmap_path)
        self._write_state(detached=False, last_rollback=None)
        self._detached = False

        affected = [entry.snapshot_id for entry in new_manifest.snapshots]
        report = {
            "replaced": True,
            "old_format_version": old.format_version,
            "new_format_version": new_manifest.format_version,
            "version_changed": version_changed,
            "old_content_digest": old_digest,
            "new_content_digest": new_digest,
            "digest_changed": digest_changed,
            "affected_snapshots": affected,
            "backup_manifest": os.path.abspath(backup_path),
            "rebuilt_chain": [
                {"snapshot_id": e.snapshot_id, "parent_snapshot_id": e.parent_snapshot_id}
                for e in new_manifest.snapshots
            ],
        }
        log_event(self.store_dir, "manifest.replaced", code=None, **{
            k: v for k, v in report.items() if k != "backup_manifest"
        })
        return report

    @staticmethod
    def _rebuild_chain_links(manifest: Manifest) -> None:
        ordered = sorted(manifest.snapshots, key=lambda item: item.sequence)
        previous = None
        for entry in ordered:
            entry.parent_snapshot_id = previous
            previous = entry.snapshot_id

    def seal_for_rollback(self) -> None:
        """Persist the in-flight bitmap before a rollback engine runs."""
        if self.manifest.snapshots:
            self._seal_live_period()


def open_store(image_path: str, store_dir: str, *, auto_recover: bool = True
               ) -> SnapshotManager:
    """Load a volume and attach its snapshot store, recovering journals."""
    volume = Volume.load(image_path)
    return SnapshotManager(volume, store_dir, auto_recover=auto_recover)
