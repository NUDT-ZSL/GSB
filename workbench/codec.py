"""倒排表分块、变长整数差值编码与两档位无损压缩。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Sequence

from .errors import E_DECODE_FAILED, E_ENCODE_FAILED, WorkbenchError

MAGIC = b"IBLK01"
LEVEL_VARINT_DELTA = 1
LEVEL_DELTA_LOW_FREQ = 2


@dataclass(frozen=True)
class Posting:
    """单条倒排记录：文档序号、词频与位置列表。"""

    doc_seq: int
    term_freq: int
    positions: tuple[int, ...]


@dataclass(frozen=True)
class InvertedBlock:
    term: str
    chunk_index: int
    postings: tuple[Posting, ...]


def encode_varint(value: int) -> bytes:
    """无符号 LEB128 变长整数编码。"""
    if value < 0:
        raise WorkbenchError(
            f"varint 不支持负数: {value}",
            stage="encode",
        )
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise WorkbenchError(
                "varint 流意外结束",
                stage="decode",
                code=E_DECODE_FAILED,
            )
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift > 63:
            raise WorkbenchError(
                "varint 超出 64 位",
                stage="decode",
                code=E_DECODE_FAILED,
            )


def chunk_postings(
    term: str,
    postings: Sequence[Posting],
    block_size: int,
) -> list[InvertedBlock]:
    """按配置块大小将有序倒排表切为连续分块。"""
    if block_size <= 0:
        raise WorkbenchError(
            f"非法分块大小: {block_size}",
            stage="encode",
        )
    blocks: list[InvertedBlock] = []
    ordered = sorted(postings, key=lambda item: item.doc_seq)
    for chunk_index, start in enumerate(range(0, len(ordered), block_size)):
        slice_ = ordered[start : start + block_size]
        blocks.append(InvertedBlock(term, chunk_index, tuple(slice_)))
    return blocks


def encode_payload(
    postings: Sequence[Posting],
    *,
    store_positions: bool,
) -> bytes:
    """档位无关的差值编码负载，两档位对保留内容使用同一套编码。"""
    try:
        out = bytearray()
        out += encode_varint(len(postings))
        previous_doc = 0
        for posting in postings:
            out += encode_varint(posting.doc_seq - previous_doc)
            out += encode_varint(posting.term_freq)
            previous_doc = posting.doc_seq
            if store_positions:
                out += encode_varint(len(posting.positions))
                previous_pos = 0
                for position in posting.positions:
                    out += encode_varint(position - previous_pos)
                    previous_pos = position
        return bytes(out)
    except WorkbenchError:
        raise
    except Exception as exc:  # pragma: no cover - 防御性分支
        raise WorkbenchError(
            f"倒排负载编码失败: {exc}",
            stage="encode",
        ) from exc


def decode_payload(data: bytes, *, store_positions: bool) -> tuple[Posting, ...]:
    """差值解码并逐项还原文档序号、词频与位置列表。"""
    try:
        offset = 0
        count, offset = decode_varint(data, offset)
        postings: list[Posting] = []
        previous_doc = 0
        for _ in range(count):
            delta, offset = decode_varint(data, offset)
            term_freq, offset = decode_varint(data, offset)
            doc_seq = previous_doc + delta
            previous_doc = doc_seq
            positions: tuple[int, ...] = ()
            if store_positions:
                pos_count, offset = decode_varint(data, offset)
                pos_list: list[int] = []
                previous_pos = 0
                for _ in range(pos_count):
                    pos_delta, offset = decode_varint(data, offset)
                    previous_pos += pos_delta
                    pos_list.append(previous_pos)
                positions = tuple(pos_list)
            postings.append(Posting(doc_seq, term_freq, positions))
        if offset != len(data):
            raise WorkbenchError(
                f"负载存在 {len(data) - offset} 字节冗余",
                stage="decode",
                code=E_DECODE_FAILED,
            )
        return tuple(postings)
    except WorkbenchError:
        raise
    except Exception as exc:
        raise WorkbenchError(
            f"倒排负载解码失败: {exc}",
            stage="decode",
            code=E_DECODE_FAILED,
        ) from exc


def block_key(term: str, chunk_index: int) -> str:
    return f"{term}#c{chunk_index}"


def encode_block(
    block: InvertedBlock,
    *,
    level: int,
    store_positions: bool,
) -> bytes:
    """块容器 = 固定魔数 + 档位/位置标志 + 块键 + 差值负载。"""
    try:
        header = bytearray(MAGIC)
        header += encode_varint(level)
        header += encode_varint(1 if store_positions else 0)
        key = block_key(block.term, block.chunk_index).encode("utf-8")
        header += encode_varint(len(key))
        header += key
        payload = encode_payload(block.postings, store_positions=store_positions)
        header += encode_varint(len(payload))
        return bytes(header) + payload
    except WorkbenchError:
        raise
    except Exception as exc:
        raise WorkbenchError(
            f"块编码失败 {block.term}: {exc}",
            stage="encode",
            code=E_ENCODE_FAILED,
        ) from exc


def decode_block(data: bytes) -> tuple[str, int, int, bool, tuple[Posting, ...]]:
    """返回 (块键, 档位, 位置开关, 倒排记录)。"""
    try:
        if not data.startswith(MAGIC):
            raise WorkbenchError("块魔数不匹配", stage="decode",
                                 code=E_DECODE_FAILED)
        offset = len(MAGIC)
        level, offset = decode_varint(data, offset)
        pos_flag, offset = decode_varint(data, offset)
        store_positions = bool(pos_flag)
        key_len, offset = decode_varint(data, offset)
        key = data[offset : offset + key_len].decode("utf-8")
        offset += key_len
        payload_len, offset = decode_varint(data, offset)
        payload = data[offset : offset + payload_len]
        offset += payload_len
        if offset != len(data):
            raise WorkbenchError("块容器存在冗余字节", stage="decode",
                                 code=E_DECODE_FAILED)
        postings = decode_payload(payload, store_positions=store_positions)
        term, _, chunk_text = key.partition("#c")
        return term, int(chunk_text), level, store_positions, postings
    except WorkbenchError:
        raise
    except Exception as exc:
        raise WorkbenchError(
            f"块解码失败: {exc}",
            stage="decode",
            code=E_DECODE_FAILED,
        ) from exc


def canonical_block_hash(
    term: str,
    chunk_index: int,
    postings: Sequence[Posting],
) -> str:
    """块内容哈希，与编码档位无关，用于判断逻辑内容是否变化。"""
    digest = hashlib.sha256()
    digest.update(block_key(term, chunk_index).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(encode_payload(postings, store_positions=True))
    return digest.hexdigest()


def filter_low_frequency(
    postings: Iterable[Posting],
    *,
    min_doc_freq: int,
) -> tuple[Posting, ...]:
    """档位二低频过滤：文档频率低于阈值的词项整体过滤。"""
    materialized = tuple(postings)
    if len(materialized) < min_doc_freq:
        return ()
    return materialized
