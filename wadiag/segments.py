from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .bloom import BloomFilter, BloomSummary, build_bloom
from .errors import EngineError, error
from .models import StoredRecord, StorageConfig, WriteRecord
from .serialization import canonical_json_bytes, sha256_bytes


SEGMENT_SCHEMA = "wadiag.segment.v1"
MANIFEST_SCHEMA = "wadiag.segment_manifest.v1"


@dataclass(frozen=True)
class SparseIndexEntry:
    block_no: int
    first_key: str
    last_key: str
    offset: int
    record_count: int
    max_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_no": self.block_no,
            "first_key": self.first_key,
            "last_key": self.last_key,
            "offset": self.offset,
            "record_count": self.record_count,
            "max_seq": self.max_seq,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SparseIndexEntry":
        return cls(
            block_no=int(value["block_no"]),
            first_key=str(value["first_key"]),
            last_key=str(value["last_key"]),
            offset=int(value["offset"]),
            record_count=int(value["record_count"]),
            max_seq=int(value["max_seq"]),
        )


@dataclass(frozen=True)
class SegmentMeta:
    run_id: str
    segment_no: int
    version: int
    level: int
    generation: int
    min_seq: int
    max_seq: int
    filename: str
    min_key: str
    max_key: str
    record_count: int
    tombstone_count: int
    size_bytes: int
    sha256: str
    content_summary: str
    index_entries: int
    block_count: int
    bloom_bits: int
    bloom_hashes: int
    bloom_fp_upper: str
    bloom: BloomSummary
    index: tuple[SparseIndexEntry, ...]
    latest_versions: tuple[dict[str, Any], ...]

    @property
    def uid(self) -> str:
        return f"{self.run_id}:S{self.segment_no:08d}:v{self.version}"

    def to_db_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "run_id": self.run_id,
            "segment_no": self.segment_no,
            "version": self.version,
            "level": self.level,
            "generation": self.generation,
            "min_seq": self.min_seq,
            "max_seq": self.max_seq,
            "filename": self.filename,
            "min_key": self.min_key,
            "max_key": self.max_key,
            "record_count": self.record_count,
            "tombstone_count": self.tombstone_count,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_summary": self.content_summary,
            "index_entries": self.index_entries,
            "block_count": self.block_count,
            "bloom_bits": self.bloom_bits,
            "bloom_hashes": self.bloom_hashes,
            "bloom_fp_upper": self.bloom_fp_upper,
        }

    def to_manifest(self) -> dict[str, Any]:
        return {
            **self.to_db_dict(),
            "bloom": self.bloom.to_dict(),
            "index": [entry.to_dict() for entry in self.index],
            "latest_versions": list(self.latest_versions),
        }


@dataclass(frozen=True)
class Segment:
    meta: SegmentMeta
    records: tuple[StoredRecord, ...]
    payload: bytes

    @property
    def bloom_filter(self) -> BloomFilter:
        return BloomFilter.from_summary(self.meta.bloom)


def _record_line(record: StoredRecord) -> bytes:
    return canonical_json_bytes(record.to_dict())


def _header_line(
    run_id: str,
    segment_no: int,
    version: int,
    level: int,
    generation: int,
    record_count: int,
    storage: StorageConfig,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema": SEGMENT_SCHEMA,
            "run_id": run_id,
            "segment_no": segment_no,
            "version": version,
            "level": level,
            "generation": generation,
            "record_count": record_count,
            "compression_block_size_bytes": storage.compression_block_size_bytes,
            "bloom_bits_per_key": storage.bloom_bits_per_key,
            "bloom_hash_count": storage.bloom_hash_count,
        }
    )


def _build_sparse_index(
    records: tuple[StoredRecord, ...],
    record_lines: tuple[bytes, ...],
    header_size: int,
    block_size: int,
) -> tuple[SparseIndexEntry, ...]:
    offsets: list[int] = []
    cursor = header_size
    for line in record_lines:
        offsets.append(cursor)
        cursor += len(line)
    ordered = sorted(range(len(records)), key=lambda i: (records[i].key, records[i].seq))
    entries: list[SparseIndexEntry] = []
    current_indexes: list[int] = []
    current_bytes = 0
    for record_index in ordered:
        line_size = len(record_lines[record_index])
        if current_indexes and current_bytes + line_size > block_size:
            entries.append(
                _make_index_entry(len(entries), records, current_indexes, offsets)
            )
            current_indexes = []
            current_bytes = 0
        current_indexes.append(record_index)
        current_bytes += line_size
    if current_indexes:
        entries.append(_make_index_entry(len(entries), records, current_indexes, offsets))
    return tuple(entries)


def _make_index_entry(
    block_no: int,
    records: tuple[StoredRecord, ...],
    indexes: list[int],
    offsets: list[int],
) -> SparseIndexEntry:
    ordered = sorted(indexes, key=lambda i: (records[i].key, records[i].seq))
    first = records[ordered[0]]
    last = records[ordered[-1]]
    return SparseIndexEntry(
        block_no=block_no,
        first_key=first.key,
        last_key=last.key,
        offset=offsets[ordered[0]],
        record_count=len(ordered),
        max_seq=max(records[i].seq for i in ordered),
    )


def build_segment(
    records: Iterable[StoredRecord],
    *,
    run_id: str,
    segment_no: int,
    version: int,
    level: int,
    generation: int,
    storage: StorageConfig,
) -> Segment:
    record_tuple = tuple(records)
    if not record_tuple:
        raise error("E_INTERNAL", "不能生成空段", segment_no=segment_no)
    header = _header_line(
        run_id,
        segment_no,
        version,
        level,
        generation,
        len(record_tuple),
        storage,
    )
    record_lines = tuple(_record_line(record) for record in record_tuple)
    payload = header + b"".join(record_lines)
    keys = [record.key for record in record_tuple]
    bloom, bloom_summary = build_bloom(
        keys,
        storage.bloom_bits_per_key,
        storage.bloom_hash_count,
    )
    index = _build_sparse_index(
        record_tuple,
        record_lines,
        len(header),
        storage.compression_block_size_bytes,
    )
    latest: dict[str, StoredRecord] = {}
    for record in record_tuple:
        previous = latest.get(record.key)
        if previous is None or record.seq > previous.seq:
            latest[record.key] = record
    latest_versions = tuple(
        {
            "key": key,
            "seq": latest[key].seq,
            "op": latest[key].op,
            "tombstone": latest[key].is_tombstone,
            "value_bytes": latest[key].value_bytes,
        }
        for key in sorted(latest)
    )
    content_summary = sha256_bytes(canonical_json_bytes(latest_versions))
    filename = f"S{segment_no:08d}-v{version}.seg"
    meta = SegmentMeta(
        run_id=run_id,
        segment_no=segment_no,
        version=version,
        level=level,
        generation=generation,
        min_seq=min(record.seq for record in record_tuple),
        max_seq=max(record.seq for record in record_tuple),
        filename=filename,
        min_key=min(keys),
        max_key=max(keys),
        record_count=len(record_tuple),
        tombstone_count=sum(1 for record in record_tuple if record.is_tombstone),
        size_bytes=len(payload),
        sha256=sha256_bytes(payload),
        content_summary=content_summary,
        index_entries=len(index),
        block_count=len(index),
        bloom_bits=bloom_summary.bit_width,
        bloom_hashes=bloom_summary.hash_count,
        bloom_fp_upper=bloom_summary.false_positive_upper,
        bloom=bloom_summary,
        index=index,
        latest_versions=latest_versions,
    )
    return Segment(meta=meta, records=record_tuple, payload=payload)


def build_flush_segments(
    records: Iterable[WriteRecord],
    storage: StorageConfig,
    run_id: str,
    *,
    first_segment_no: int = 1,
) -> tuple[Segment, ...]:
    groups: list[list[StoredRecord]] = []
    current: list[StoredRecord] = []
    used = 0
    for record in records:
        stored = StoredRecord.from_write(record, generation=0)
        cost = stored.footprint_bytes
        if current and used + cost > storage.memtable_capacity_bytes:
            groups.append(current)
            current = []
            used = 0
        current.append(stored)
        used += cost
    if current:
        groups.append(current)
    sized_groups: list[list[StoredRecord]] = []
    pending_groups = list(groups)
    while pending_groups:
        group = pending_groups.pop(0)
        probe = build_segment(
            group,
            run_id=run_id,
            segment_no=0,
            version=1,
            level=0,
            generation=0,
            storage=storage,
        )
        if (
            probe.meta.size_bytes > storage.segment_size_limit_bytes
            and len(group) > 1
        ):
            midpoint = len(group) // 2
            pending_groups.insert(0, group[midpoint:])
            pending_groups.insert(0, group[:midpoint])
            continue
        sized_groups.append(group)
    return tuple(
        build_segment(
            group,
            run_id=run_id,
            segment_no=first_segment_no + index,
            version=1,
            level=0,
            generation=0,
            storage=storage,
        )
        for index, group in enumerate(sized_groups)
    )


def _estimate_merged_line_size(record: StoredRecord, storage: StorageConfig) -> int:
    return max(len(_record_line(record)), min(storage.segment_size_limit_bytes, 1))


def build_merged_segments(
    records: Iterable[StoredRecord],
    storage: StorageConfig,
    run_id: str,
    *,
    first_segment_no: int,
    level: int,
    generation: int,
) -> tuple[Segment, ...]:
    ordered = sorted(records, key=lambda record: (record.key, record.seq, record.origin_generation))
    if not ordered:
        return ()
    groups: list[list[StoredRecord]] = []
    current: list[StoredRecord] = []
    used = 0
    for record in ordered:
        cost = _estimate_merged_line_size(record, storage)
        if current and used + cost > storage.segment_size_limit_bytes:
            groups.append(current)
            current = []
            used = 0
        current.append(record)
        used += cost
    if current:
        groups.append(current)
    sized_groups: list[list[StoredRecord]] = []
    pending_groups = list(groups)
    while pending_groups:
        group = pending_groups.pop(0)
        probe = build_segment(
            group,
            run_id=run_id,
            segment_no=0,
            version=1,
            level=level,
            generation=generation,
            storage=storage,
        )
        if (
            probe.meta.size_bytes > storage.segment_size_limit_bytes
            and len(group) > 1
        ):
            midpoint = len(group) // 2
            pending_groups.insert(0, group[midpoint:])
            pending_groups.insert(0, group[:midpoint])
            continue
        sized_groups.append(group)
    return tuple(
        build_segment(
            group,
            run_id=run_id,
            segment_no=first_segment_no + index,
            version=1,
            level=level,
            generation=generation,
            storage=storage,
        )
        for index, group in enumerate(sized_groups)
    )


def parse_segment_payload(
    payload: bytes,
    *,
    run_id: str,
    filename: str,
) -> Segment:
    try:
        lines = payload.splitlines(keepends=True)
        if not lines:
            raise ValueError("空段文件")
        import json

        header = json.loads(lines[0].decode("utf-8"))
        if header.get("schema") != SEGMENT_SCHEMA:
            raise ValueError("段文件 schema 不匹配")
        if header.get("run_id") != run_id:
            raise ValueError("段文件 run_id 不匹配")
        expected = int(header["record_count"])
        raw_records = [json.loads(line.decode("utf-8")) for line in lines[1:]]
        if len(raw_records) != expected:
            raise ValueError("段文件记录数不匹配")
        stored = tuple(StoredRecord.from_dict(item) for item in raw_records)
        segment_no = int(header["segment_no"])
        version = int(header["version"])
        level = int(header["level"])
        generation = int(header["generation"])
        block_size = int(header["compression_block_size_bytes"])
        bloom_bits_per_key = int(header["bloom_bits_per_key"])
        bloom_hash_count = int(header["bloom_hash_count"])
    except (ValueError, KeyError, UnicodeError, TypeError) as exc:
        raise error(
            "E_BAD_SEGMENT",
            "段文件解析失败",
            filename=filename,
            reason=str(exc),
        ) from exc
    keys = [record.key for record in stored]
    bloom, bloom_summary = build_bloom(
        keys,
        bloom_bits_per_key,
        bloom_hash_count,
    )
    record_lines = tuple(_record_line(record) for record in stored)
    header_bytes = lines[0]
    index = _build_sparse_index(stored, record_lines, len(header_bytes), block_size)
    latest: dict[str, StoredRecord] = {}
    for record in stored:
        previous = latest.get(record.key)
        if previous is None or record.seq > previous.seq:
            latest[record.key] = record
    latest_versions = tuple(
        {
            "key": key,
            "seq": latest[key].seq,
            "op": latest[key].op,
            "tombstone": latest[key].is_tombstone,
            "value_bytes": latest[key].value_bytes,
        }
        for key in sorted(latest)
    )
    meta = SegmentMeta(
        run_id=run_id,
        segment_no=segment_no,
        version=version,
        level=level,
        generation=generation,
        min_seq=min(record.seq for record in stored),
        max_seq=max(record.seq for record in stored),
        filename=filename,
        min_key=min(keys),
        max_key=max(keys),
        record_count=len(stored),
        tombstone_count=sum(1 for record in stored if record.is_tombstone),
        size_bytes=len(payload),
        sha256=sha256_bytes(payload),
        content_summary=sha256_bytes(canonical_json_bytes(latest_versions)),
        index_entries=len(index),
        block_count=len(index),
        bloom_bits=bloom_summary.bit_width,
        bloom_hashes=bloom_summary.hash_count,
        bloom_fp_upper=bloom_summary.false_positive_upper,
        bloom=bloom_summary,
        index=index,
        latest_versions=latest_versions,
    )
    return Segment(meta=meta, records=stored, payload=payload)


def build_manifest(run_id: str, segments: Iterable[Segment], kind: str) -> dict[str, Any]:
    items = [segment.meta.to_manifest() for segment in segments]
    digest_material = canonical_json_bytes({"run_id": run_id, "kind": kind, "segments": items})
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "kind": kind,
        "segment_count": len(items),
        "segments": items,
        "manifest_digest": sha256_bytes(digest_material),
    }


def estimate_lookup(
    key: str,
    segments: Iterable[Segment],
    *,
    sample_ratio: str = "1.0",
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    segment_reads = 0
    block_reads = 0
    checked = 0
    for segment in sorted(segments, key=lambda item: (item.meta.level, item.meta.segment_no)):
        checked += 1
        meta = segment.meta
        observation: dict[str, Any] = {
            "segment_no": meta.segment_no,
            "level": meta.level,
            "min_key": meta.min_key,
            "max_key": meta.max_key,
            "index_entries": meta.index_entries,
            "bloom_bits": meta.bloom_bits,
            "bloom_hashes": meta.bloom_hashes,
            "bloom_fp_upper": meta.bloom_fp_upper,
        }
        if key < meta.min_key or key > meta.max_key:
            observation.update(
                {
                    "decision": "range_skip",
                    "estimated_blocks": 0,
                }
            )
            observations.append(observation)
            continue
        observation["bloom_maybe"] = segment.bloom_filter.might_contain(key)
        if not observation["bloom_maybe"]:
            observation.update(
                {
                    "decision": "bloom_skip",
                    "estimated_blocks": 0,
                }
            )
            observations.append(observation)
            continue
        candidates = [entry for entry in meta.index if entry.first_key <= key]
        if not candidates:
            estimated_blocks = 1
            block = None
        else:
            block = candidates[-1]
            estimated_blocks = 1
        observation.update(
            {
                "decision": "index_probe",
                "index_block": None if block is None else block.block_no,
                "index_first_key": None if block is None else block.first_key,
                "index_last_key": None if block is None else block.last_key,
                "index_offset": None if block is None else block.offset,
                "estimated_blocks": estimated_blocks,
            }
        )
        observations.append(observation)
        segment_reads += 1
        block_reads += estimated_blocks
    return {
        "key": key,
        "mode": "exact_key",
        "sample_ratio": str(sample_ratio),
        "sampling": {
            "declared_ratio": str(sample_ratio),
            "applied": False,
            "reason": "exact_key_lookup_uses_all_range_and_bloom_observations",
        },
        "segments_observed": checked,
        "estimated_segment_reads": segment_reads,
        "estimated_block_reads": block_reads,
        "observations": observations,
        "deterministic": True,
    }
