"""倒排索引构建器：增量构建、版本失效、块级复用与原子提交。"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codec import (
    LEVEL_DELTA_LOW_FREQ,
    LEVEL_VARINT_DELTA,
    Posting,
    InvertedBlock,
    block_key,
    canonical_block_hash,
    chunk_postings,
    decode_block,
    encode_block,
    filter_low_frequency,
)
from .errors import (
    E_BUILD_FAILED,
    E_ENCODE_FAILED,
    make_failure,
)
from .normalization import ParsedDocument
from .store import (
    IndexConfig,
    IndexLayout,
    canonical_json,
    config_fingerprint,
    dump_json,
    load_json,
    read_index_shard,
    remove_path,
    sha256_bytes,
    snapshot_manifest,
    version_fingerprint,
    vocab_fingerprint,
    write_doc_shard,
    write_index_shard,
)


@dataclass
class BuildResult:
    manifest: dict[str, Any]
    reused_blocks: list[dict[str, Any]] = field(default_factory=list)
    rebuilt_blocks: list[dict[str, Any]] = field(default_factory=list)
    affected_shards: list[int] = field(default_factory=list)
    recovered: bool = False
    graceful_stop: bool = False
    failures: list[dict[str, Any]] = field(default_factory=list)

    def block_summary(self) -> dict[str, Any]:
        return {
            "reused": self.reused_blocks,
            "rebuilt": self.rebuilt_blocks,
            "affected_shards": sorted(set(self.affected_shards)),
        }


class GracefulStopRequested(Exception):
    """测试或信号触发的可处理退出。"""


class IndexBuilder:
    def __init__(self, config: IndexConfig, layout: IndexLayout,
                 vocab_path: str | os.PathLike[str] | None = None):
        config.validate()
        self.config = config
        self.layout = layout
        self.vocab_path = Path(vocab_path or config.vocab_path)
        self.vocab_fp = vocab_fingerprint(self.vocab_path)
        self.cfg_fp = config_fingerprint(config)

        self.postings: dict[str, list[Posting]] = {}
        self.doc_id_by_seq: list[str] = []
        self.seq_by_doc_id: dict[str, int] = {}
        self.doc_records: dict[int, ParsedDocument] = {}
        self.doc_shard_of_seq: list[int] = []

        self.old_manifest: dict[str, Any] | None = None
        self.old_blocks: dict[str, bytes] = {}
        self._install_signal_handlers()
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def _install_signal_handlers(self) -> None:
        if not hasattr(signal, "SIGTERM"):
            return
        self._previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, self._handle_stop_signal)
        if hasattr(signal, "SIGINT"):
            self._previous_int = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle_stop_signal)

    def restore_signal_handlers(self) -> None:
        if not hasattr(signal, "SIGTERM"):
            return
        signal.signal(signal.SIGTERM, self._previous_term)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, self._previous_int)

    def _handle_stop_signal(self, signum: int, _frame: Any) -> None:
        self._stop_requested = True

    def _check_stop(self) -> None:
        if self._stop_requested:
            raise GracefulStopRequested("收到可处理退出信号")

    def _progress(self, phase: str, processed: int, total: int,
                  extra: dict[str, Any] | None = None) -> None:
        data = {
            "phase": phase,
            "processed": processed,
            "total": total,
            "compression_level": self.config.compression_level,
            "store_positions": self.config.store_positions,
        }
        if extra:
            data.update(extra)
        dump_json(self.layout.progress_path, data)

    def load_committed_state(self) -> bool:
        """从已提交清单恢复内存状态；强制终止后仅信任已提交产物。"""
        if not self.layout.manifest_path.exists():
            return False
        try:
            manifest = load_json(self.layout.manifest_path)
            self.old_manifest = manifest
            self.doc_id_by_seq = list(manifest["doc_ids"])
            self.seq_by_doc_id = {
                doc_id: seq for seq, doc_id in enumerate(self.doc_id_by_seq)
            }
            for block_info in manifest["blocks"]:
                shard_id = block_info["shard_id"]
                shard_map = self.old_blocks.setdefault(shard_id, {})
                shard_path = self.layout.index_shard_path(shard_id)
                blob = read_index_shard(shard_path)[block_info["block_key"]]
                shard_map[block_info["block_key"]] = blob
            self.doc_shard_of_seq = list(manifest.get("doc_shard_of_seq", []))
            self._rebuild_postings_from_doc_shards(manifest)
            return True
        except Exception as exc:
            raise type(exc)(
                f"已提交索引损坏，无法恢复: {exc}"
            ) from exc

    def _rebuild_postings_from_doc_shards(self, manifest: dict[str, Any]) -> None:
        """强制终止或新会话恢复时，仅依据已提交文档分片重建内存倒排表。"""
        from .normalization import parse_document

        self.postings = {}
        self.doc_records = {}
        for shard in sorted(manifest["doc_shards"], key=lambda item: item["shard_id"]):
            shard_path = self.layout.base_dir / shard["path"]
            with open(shard_path, "r", encoding="utf-8") as handle:
                raw_lines = [line.rstrip("\n") for line in handle if line.strip()]
            for raw_line in raw_lines:
                document = parse_document(raw_line)
                doc_seq = self.seq_by_doc_id[document.doc_id]
                self.doc_records[doc_seq] = document
                for term, term_freq in document.term_freq.items():
                    self.postings.setdefault(term, []).append(
                        Posting(
                            doc_seq=doc_seq,
                            term_freq=term_freq,
                            positions=document.positions.get(term, ()),
                        )
                    )

    def add_documents(self, documents: list[ParsedDocument]) -> None:
        """增量摄入已解析文档，维护倒排表（文档序号按摄入顺序分配）。"""
        for document in documents:
            if document.doc_id in self.seq_by_doc_id:
                continue
            doc_seq = len(self.doc_id_by_seq)
            self.doc_id_by_seq.append(document.doc_id)
            self.seq_by_doc_id[document.doc_id] = doc_seq
            self.doc_records[doc_seq] = document
            shard_id = doc_seq // self.config.doc_shard_size
            self.doc_shard_of_seq.append(shard_id)
            for term, term_freq in document.term_freq.items():
                positions = document.positions.get(term, ())
                posting = Posting(
                    doc_seq=doc_seq,
                    term_freq=term_freq,
                    positions=positions,
                )
                self.postings.setdefault(term, []).append(posting)

    def _planned_blocks(self) -> dict[str, list[InvertedBlock]]:
        planned: dict[str, list[InvertedBlock]] = {}
        threshold = self.config.low_freq_threshold
        for term in sorted(self.postings):
            postings = sorted(self.postings[term], key=lambda item: item.doc_seq)
            if self.config.compression_level == LEVEL_DELTA_LOW_FREQ:
                postings = list(filter_low_frequency(
                    postings, min_doc_freq=threshold,
                ))
                if not postings:
                    planned[term] = []
                    continue
            planned[term] = chunk_postings(term, postings, self.config.block_size)
        return planned

    def _expected_version(self, shard_digests: list[tuple[str, str]]) -> str:
        return version_fingerprint(
            self.vocab_fp,
            self.cfg_fp,
            self.config.compression_level,
            self.config.store_positions,
            shard_digests,
        )

    def build(
        self,
        new_documents: list[ParsedDocument] | None = None,
        *,
        total_docs: int | None = None,
    ) -> BuildResult:
        """构建/增量构建并原子提交全部索引产物。"""
        result = BuildResult(manifest={})
        recovered = self.load_committed_state()
        result.recovered = recovered
        if new_documents:
            self.add_documents(new_documents)
        self.layout.ensure()

        expected_total = total_docs if total_docs is not None else len(self.doc_id_by_seq)
        self._progress("planning", 0, expected_total,
                       {"doc_count": len(self.doc_id_by_seq)})
        planned = self._planned_blocks()

        old_registry: dict[str, dict[str, Any]] = {}
        if self.old_manifest is not None:
            old_registry = {
                entry["block_key"]: entry for entry in self.old_manifest["blocks"]
            }

        shard_blobs: dict[int, dict[str, bytes]] = {}
        block_registry: list[dict[str, Any]] = []
        filtered_terms: list[str] = []
        planned_keys: set[str] = set()

        for term, blocks in planned.items():
            if not blocks:
                filtered_terms.append(term)
                continue
            for block in blocks:
                key = block_key(block.term, block.chunk_index)
                planned_keys.add(key)
                content_hash = canonical_block_hash(
                    block.term, block.chunk_index, block.postings,
                )
                shard_id = block.postings[0].doc_seq // self.config.doc_shard_size
                old_entry = old_registry.get(key)
                blob = self._reuse_blob(old_entry, content_hash, key)
                if blob is not None:
                    status = "reused"
                    result.reused_blocks.append({
                        "block_key": key,
                        "shard_id": shard_id,
                        "reason": "格式一致且内容哈希匹配",
                    })
                else:
                    blob = encode_block(
                        block,
                        level=self.config.compression_level,
                        store_positions=self.config.store_positions,
                    )
                    status = "rebuilt"
                    reason = self._rebuild_reason(old_entry, content_hash)
                    result.rebuilt_blocks.append({
                        "block_key": key,
                        "shard_id": shard_id,
                        "reason": reason,
                    })
                shard_blobs.setdefault(shard_id, {})[key] = blob
                block_registry.append({
                    "block_key": key,
                    "term": block.term,
                    "chunk_index": block.chunk_index,
                    "shard_id": shard_id,
                    "content_hash": content_hash,
                    "level": self.config.compression_level,
                    "store_positions": self.config.store_positions,
                    "posting_count": len(block.postings),
                    "doc_seq_min": block.postings[0].doc_seq,
                    "doc_seq_max": block.postings[-1].doc_seq,
                    "encoded_bytes": len(blob),
                    "status": status,
                })
                self._check_stop()

        self._retire_missing_blocks(old_registry, planned_keys, result)
        self._progress("writing_shards", len(self.doc_id_by_seq), expected_total)
        doc_shards = self._write_doc_shards()
        index_shards, affected = self._write_index_shards(shard_blobs, result)
        result.affected_shards = affected

        shard_digests = [(entry["path"], entry["digest"])
                         for entry in doc_shards + index_shards]
        index_fp = self._expected_version(shard_digests)
        manifest = {
            "version_fingerprint": index_fp,
            "vocab_fingerprint": self.vocab_fp,
            "config_fingerprint": self.cfg_fp,
            "compression_level": self.config.compression_level,
            "store_positions": self.config.store_positions,
            "low_freq_threshold": self.config.low_freq_threshold,
            "doc_shard_size": self.config.doc_shard_size,
            "block_size": self.config.block_size,
            "doc_count": len(self.doc_id_by_seq),
            "doc_ids": self.doc_id_by_seq,
            "doc_shard_of_seq": self.doc_shard_of_seq,
            "doc_shards": doc_shards,
            "index_shards": index_shards,
            "blocks": sorted(block_registry, key=lambda item: item["block_key"]),
            "filtered_terms": sorted(filtered_terms),
            "block_reuse": result.block_summary(),
        }
        dump_json(self.layout.manifest_path, manifest)
        snapshot_manifest(self.layout, manifest)
        self._progress("committed", len(self.doc_id_by_seq), expected_total,
                       {"version_fingerprint": index_fp})
        result.manifest = manifest
        return result

    def _reuse_blob(
        self,
        old_entry: dict[str, Any] | None,
        content_hash: str,
        key: str,
    ) -> bytes | None:
        if old_entry is None:
            return None
        if old_entry["level"] != self.config.compression_level:
            return None
        if old_entry["store_positions"] != self.config.store_positions:
            return None
        if old_entry["content_hash"] != content_hash:
            return None
        blob = self.old_blocks.get(old_entry["shard_id"], {}).get(key)
        if blob is None:
            return None
        try:
            term, chunk_index, level, has_pos, postings = decode_block(blob)
            if level != self.config.compression_level or has_pos != self.config.store_positions:
                return None
            if canonical_block_hash(term, chunk_index, postings) != content_hash:
                return None
        except Exception:
            return None
        return blob

    def _rebuild_reason(
        self,
        old_entry: dict[str, Any] | None,
        content_hash: str,
    ) -> str:
        if old_entry is None:
            return "新增块（旧清单不存在）"
        reasons: list[str] = []
        if old_entry["level"] != self.config.compression_level:
            reasons.append("压缩档位变化")
        if old_entry["store_positions"] != self.config.store_positions:
            reasons.append("位置开关变化")
        if old_entry["content_hash"] != content_hash:
            reasons.append("块内容变化")
        return "、".join(reasons) if reasons else "旧块无法校验，强制重建"

    def _retire_missing_blocks(
        self,
        old_registry: dict[str, dict[str, Any]],
        planned_keys: set[str],
        result: BuildResult,
    ) -> None:
        for key, entry in old_registry.items():
            if key not in planned_keys:
                result.rebuilt_blocks.append({
                    "block_key": key,
                    "shard_id": entry["shard_id"],
                    "reason": "低频过滤或词项移除导致块退役",
                })

    def _write_doc_shards(self) -> list[dict[str, Any]]:
        shards: list[dict[str, Any]] = []
        ordered_records = [self.doc_records[seq] for seq in sorted(self.doc_records)]
        for shard_id, start in enumerate(
            range(0, len(ordered_records), self.config.doc_shard_size)
        ):
            batch = ordered_records[start : start + self.config.doc_shard_size]
            path = self.layout.doc_shard_path(shard_id)
            digest = write_doc_shard(path, [record.raw_line for record in batch])
            shards.append({
                "kind": "doc",
                "shard_id": shard_id,
                "path": str(path.relative_to(self.layout.base_dir)),
                "digest": digest,
                "doc_count": len(batch),
            })
        return sorted(shards, key=lambda item: item["path"])

    def _write_index_shards(
        self,
        shard_blobs: dict[int, dict[str, bytes]],
        result: BuildResult,
    ) -> tuple[list[dict[str, Any]], list[int]]:
        affected: set[int] = set()
        old_index_shards = {}
        if self.old_manifest is not None:
            old_index_shards = {
                entry["shard_id"]: entry
                for entry in self.old_manifest["index_shards"]
            }
        all_shard_ids = set(shard_blobs) | set(old_index_shards)
        shards: list[dict[str, Any]] = []
        for shard_id in sorted(all_shard_ids):
            blocks = shard_blobs.get(shard_id, {})
            path = self.layout.index_shard_path(shard_id)
            if not blocks:
                remove_path(path)
                affected.add(shard_id)
                continue
            ordered_blocks = [(key, blocks[key]) for key in sorted(blocks)]
            digest = write_index_shard(path, shard_id, ordered_blocks)
            old_entry = old_index_shards.get(shard_id)
            if old_entry is None or old_entry.get("digest") != digest:
                affected.add(shard_id)
            shards.append({
                "kind": "index",
                "shard_id": shard_id,
                "path": str(path.relative_to(self.layout.base_dir)),
                "digest": digest,
                "block_count": len(ordered_blocks),
            })
        return sorted(shards, key=lambda item: item["path"]), sorted(affected)

    def detect_version_mismatch(self) -> dict[str, Any] | None:
        """比较旧清单版本指纹与当前配置，返回失效原因（无旧产物返回 None）。"""
        if self.old_manifest is None and self.layout.manifest_path.exists():
            self.load_committed_state()
        if self.old_manifest is None:
            return None
        manifest = self.old_manifest
        mismatches: list[str] = []
        if manifest.get("vocab_fingerprint") != self.vocab_fp:
            mismatches.append("vocab_fingerprint")
        if manifest.get("config_fingerprint") != self.cfg_fp:
            mismatches.append("config_fingerprint")
        if manifest.get("compression_level") != self.config.compression_level:
            mismatches.append("compression_level")
        if manifest.get("store_positions") != self.config.store_positions:
            mismatches.append("store_positions")
        shard_digests = [
            (entry["path"], entry["digest"])
            for entry in manifest.get("doc_shards", []) + manifest.get("index_shards", [])
        ]
        expected = self._expected_version(shard_digests)
        if manifest.get("version_fingerprint") != expected:
            mismatches.append("version_fingerprint")
        if not mismatches:
            return None
        return {
            "old_version": manifest.get("version_fingerprint"),
            "expected_version": expected,
            "mismatched_fields": mismatches,
        }
