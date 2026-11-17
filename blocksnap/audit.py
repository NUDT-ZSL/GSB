"""Snapshot-chain consistency audit with versioned, whole-replace conclusions.

Each audit conclusion is persisted to ``audits/audit-v<format_version>-s<sequence>.json``.
Re-auditing the same manifest version overwrites that single file (never
appends); a version change produces a new conclusion file and moves the
``latest`` pointer, i.e. conclusions are replaced as a whole.
"""
from __future__ import annotations

import json
import os

from .bitmap import Bitmap, bitmap_byte_length
from .digest import sha256_hex
from .errors import ChainError, IntegrityError
from .logging_utils import log_event
from .manifest import load_manifest

AUDIT_DIRNAME = "audits"
LATEST_NAME = "latest.json"


def run_audit(manager) -> dict:
    manifest = manager.manifest
    objects = manager.objects
    block_size = manager.volume.block_size
    findings: list[dict] = []
    ok = True

    def add(level: str, code: str, message: str, **context) -> None:
        nonlocal ok
        findings.append({"level": level, "code": code, "message": message, **context})
        if level == "error":
            ok = False

    # 1. baseline resolves and digest matches its content id
    baseline = b"\x00" * (block_size * manifest.block_count)
    baseline_ok = False
    try:
        baseline = objects.get(manifest.baseline_object)
        baseline_ok = True
        if sha256_hex(baseline) != manifest.baseline_object.split("-", 1)[1]:
            add("error", "E_INTEGRITY", "baseline object digest mismatch",
                object_id=manifest.baseline_object)
        if len(baseline) != block_size * manifest.block_count:
            add("error", "E_VOLUME", "baseline length inconsistent with geometry",
                baseline_length=len(baseline),
                expected_length=block_size * manifest.block_count)
    except ChainError as exc:
        add("error", "E_CHAIN", exc.message, **exc.context)

    # 2. per-snapshot bitmap <-> reference set correspondence
    reconstructed = bytearray(baseline)
    for index, entry in enumerate(manifest.snapshots):
        is_open_period = index == len(manifest.snapshots) - 1 and not entry.bitmap_object
        if is_open_period:
            bitmap = Bitmap(manifest.block_count)
        else:
            try:
                packed = objects.get(entry.bitmap_object)
                bitmap = Bitmap(manifest.block_count, packed)
            except ChainError as exc:
                add("error", "E_CHAIN", exc.message, snapshot_id=entry.snapshot_id,
                    **exc.context)
                continue
            except Exception as exc:  # noqa: BLE001
                add("error", "E_SNAPSHOT", str(exc), snapshot_id=entry.snapshot_id)
                continue
        if entry.bitmap_length != bitmap_byte_length(manifest.block_count):
            add("error", "E_SNAPSHOT",
                "bitmap length incompatible with block count",
                snapshot_id=entry.snapshot_id, bitmap_length=entry.bitmap_length)
        bitmap_indices = {int(i) for i in bitmap.set_indices().tolist()}
        ref_indices = {ref.block_index for ref in entry.blocks}
        if bitmap_indices != ref_indices:
            ok = False
            add("error", "E_SNAPSHOT",
                "bitmap indices and block reference set differ",
                snapshot_id=entry.snapshot_id,
                bitmap_only=sorted(bitmap_indices - ref_indices),
                reference_only=sorted(ref_indices - bitmap_indices))
        if entry.changed_count != len(bitmap_indices):
            add("error", "E_SNAPSHOT", "changed_count disagrees with bitmap",
                snapshot_id=entry.snapshot_id,
                declared=entry.changed_count, actual=len(bitmap_indices))
        if not baseline_ok:
            add("error", "E_CHAIN",
                "cannot replay chain without baseline",
                snapshot_id=entry.snapshot_id)
            continue

        # Replay the snapshot boundary from its full state-block reference
        # map (content-addressed objects shared with the CoW store).
        boundary = bytearray(block_size * manifest.block_count)
        state_indices = set()
        for ref in entry.state_blocks:
            data = objects.get_verified(
                ref.object_id, ref.digest, ref.block_index
            )
            state_indices.add(ref.block_index)
            start = ref.block_index * block_size
            boundary[start:start + block_size] = data
        if state_indices != set(range(manifest.block_count)):
            add("error", "E_SNAPSHOT",
                "state block map does not cover every volume block",
                snapshot_id=entry.snapshot_id,
                covered=len(state_indices),
                expected=manifest.block_count)

        replayed_digest = "sha256:" + sha256_hex(bytes(boundary))
        if replayed_digest != "sha256:" + entry.state_digest:
            add("error", "E_INTEGRITY",
                "chain replay does not reproduce snapshot state",
                snapshot_id=entry.snapshot_id,
                expected_digest=entry.state_digest,
                replayed_digest=replayed_digest.split(":", 1)[1])
        else:
            findings.append({
                "level": "info",
                "code": "OK",
                "message": "chain replay reproduces snapshot state",
                "snapshot_id": entry.snapshot_id,
                "state_digest": entry.state_digest,
            })

    # 4. topology: parent links form a replayable chain
    previous = None
    for entry in manifest.snapshots:
        if entry.parent_snapshot_id != previous:
            add("error", "E_CHAIN", "broken parent link in snapshot chain",
                snapshot_id=entry.snapshot_id,
                expected_parent=previous,
                actual_parent=entry.parent_snapshot_id)
        previous = entry.snapshot_id

    # 5. manifest version / snapshot version numbers
    if manifest.sequence != len(manifest.snapshots):
        add("error", "E_SNAPSHOT",
            "manifest sequence does not match snapshot count",
            sequence=manifest.sequence,
            snapshot_count=len(manifest.snapshots))
    for index, entry in enumerate(manifest.snapshots, start=1):
        if entry.sequence != index:
            add("error", "E_SNAPSHOT", "snapshot version number out of order",
                snapshot_id=entry.snapshot_id,
                expected_sequence=index, actual_sequence=entry.sequence)
    expected_digest = manifest.compute_content_digest()
    if expected_digest != manifest.content_digest:
        add("error", "E_SNAPSHOT", "manifest content digest invalid",
            expected_digest=manifest.content_digest,
            actual_digest=expected_digest)

    conclusion = {
        "status": "PASS" if ok else "FAIL",
        "format_version": manifest.format_version,
        "sequence": manifest.sequence,
        "manifest_content_digest": manifest.content_digest,
        "snapshot_count": len(manifest.snapshots),
        "finding_count": len(findings),
        "findings": findings,
    }
    _persist_conclusion(manager.store_dir, conclusion)
    log_event(manager.store_dir, "audit.completed",
              code=None, status=conclusion["status"],
              format_version=manifest.format_version,
              sequence=manifest.sequence)
    return conclusion


def _conclusion_path(store_dir: str, conclusion: dict) -> str:
    audit_dir = os.path.join(store_dir, AUDIT_DIRNAME)
    os.makedirs(audit_dir, exist_ok=True)
    return os.path.join(
        audit_dir,
        f"audit-v{conclusion['format_version']}-s{conclusion['sequence']}.json",
    )


def _persist_conclusion(store_dir: str, conclusion: dict) -> None:
    path = _conclusion_path(store_dir, conclusion)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(conclusion, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    latest = os.path.join(store_dir, AUDIT_DIRNAME, LATEST_NAME)
    with open(latest + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(conclusion, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(latest + ".tmp", latest)
