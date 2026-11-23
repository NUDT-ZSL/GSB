"""Stage 1: deterministic sharded ingest + word-alignment validation."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .errors import Codes, PipelineError
from .util import checksum_obj, codepoint_interval, sha256_hex

REQUIRED_FIELDS = (
    "pair_id", "src_tokens", "tgt_tokens", "align",
    "src_len", "tgt_len", "noise_level", "domain_tag",
)


@dataclass(frozen=True)
class IngestConfig:
    shard_size: int = 500

    def validate(self):
        if not isinstance(self.shard_size, int) or self.shard_size < 1:
            raise PipelineError(Codes.CONFIG_INVALID, "shard_size must be a positive integer")


def _validate_record(rec, src_vocab_set, tgt_vocab_set):
    pair_id = rec.get("pair_id")
    for field in REQUIRED_FIELDS:
        if field not in rec:
            raise PipelineError(Codes.SCHEMA, f"missing field {field}", pair_id=pair_id)
    src, tgt = rec["src_tokens"], rec["tgt_tokens"]
    if not src or not tgt:
        raise PipelineError(Codes.EMPTY_SENT, "empty source or target sentence", pair_id=pair_id)
    if rec["src_len"] != len(src) or rec["tgt_len"] != len(tgt):
        raise PipelineError(
            Codes.LEN_MISMATCH,
            f"declared lengths ({rec['src_len']},{rec['tgt_len']}) != actual ({len(src)},{len(tgt)})",
            pair_id=pair_id,
        )
    for pos, tok in enumerate(src):
        if tok not in src_vocab_set:
            raise PipelineError(Codes.VOCAB_TOKEN, f"src token {tok!r} not in controlled vocab",
                                pair_id=pair_id, position=pos)
    for pos, tok in enumerate(tgt):
        if tok not in tgt_vocab_set:
            raise PipelineError(Codes.VOCAB_TOKEN, f"tgt token {tok!r} not in controlled vocab",
                                pair_id=pair_id, position=pos)
    prev_s = -1
    for idx, entry in enumerate(rec["align"]):
        if (not isinstance(entry, list)) or len(entry) != 2:
            raise PipelineError(Codes.ALIGN_RANGE, "align entry must be [src_pos, tgt_pos]",
                                pair_id=pair_id, position=idx)
        s, t = entry
        if not (0 <= s < len(src)) or not (0 <= t < len(tgt)):
            raise PipelineError(
                Codes.ALIGN_RANGE,
                f"align ({s},{t}) out of range src_len={len(src)} tgt_len={len(tgt)}",
                pair_id=pair_id, position=idx,
            )
        if s <= prev_s:
            raise PipelineError(
                Codes.ALIGN_RANGE,
                f"align direction illegal: src position {s} not strictly increasing",
                pair_id=pair_id, position=idx,
            )
        prev_s = s


def run_ingest(corpus_path, src_vocab, tgt_vocab, vocab_version, cfg: IngestConfig,
               config_checksum):
    """Read corpus JSONL, sort by pair_id ascending, validate, shard, summarize."""
    cfg.validate()
    corpus_path = Path(corpus_path)
    raw = corpus_path.read_bytes()
    records = []
    for lineno, line in enumerate(raw.decode("utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PipelineError(Codes.SCHEMA, f"invalid JSON at line {lineno}: {exc}")
        records.append(rec)

    records.sort(key=lambda r: r["pair_id"])
    src_set, tgt_set = set(src_vocab), set(tgt_vocab)
    seen = set()
    pair_summaries = []
    for rec in records:
        pair_id = rec["pair_id"]
        if pair_id in seen:
            raise PipelineError(Codes.DUP_PAIR, "duplicate pair_id", pair_id=pair_id)
        seen.add(pair_id)
        _validate_record(rec, src_set, tgt_set)
        idx = len(pair_summaries)
        pair_summaries.append({
            "pair_id": pair_id,
            "shard": idx // cfg.shard_size,
            "codepoint_interval": codepoint_interval(rec["src_tokens"] + rec["tgt_tokens"]),
            "src_len": rec["src_len"],
            "tgt_len": rec["tgt_len"],
            "align_len": len(rec["align"]),
        })

    summary = {
        "stage": "ingest",
        "vocab_version": vocab_version,
        "config_checksum": config_checksum,
        "input_checksum": sha256_hex(raw),
        "pair_count": len(records),
        "shard_count": math.ceil(len(records) / cfg.shard_size) if records else 0,
        "avg_align_len": (
            sum(p["align_len"] for p in pair_summaries) / len(pair_summaries)
            if pair_summaries else 0.0
        ),
        "pairs": pair_summaries,
    }
    summary["summary_checksum"] = checksum_obj(summary)
    return summary, records
