"""确定性变异语料生成模块。

同一 seed 生成的字节流完全一致（可哈希校验）。覆盖三类变异：
    length_overflow    长度字段溢出（声明长度大于实际载荷，含 0xFFFF 等极值）
    checksum_bypass    校验和绕过（仅破坏校验和字段）
    payload_truncation 载荷截断（帧尾被截断，声明长度保持不变）
另提供 marker 用例注入（SLOW=失控休眠, ABORT=强制终止），用于执行器验收。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random

from . import codec

CLASS_LENGTH_OVERFLOW = "length_overflow"
CLASS_CHECKSUM_BYPASS = "checksum_bypass"
CLASS_PAYLOAD_TRUNCATION = "payload_truncation"
MUTATION_CLASSES = (
    CLASS_LENGTH_OVERFLOW,
    CLASS_CHECKSUM_BYPASS,
    CLASS_PAYLOAD_TRUNCATION,
)

CLASS_INJECTED_TIMEOUT = "injected_timeout"
CLASS_INJECTED_ABORT = "injected_abort"


def _mutate_length_overflow(rng: random.Random, base: bytes) -> bytes:
    frame = bytearray(base)
    actual = len(base) - codec.HEADER_LEN - codec.CHECKSUM_LEN
    # 保证 declared > 帧内可读字节数（actual+2 含校验和），越界读取必然触发
    declared = rng.choice([
        actual + rng.randint(3, 64),
        0xFFFF,
        codec.MAX_PAYLOAD,
    ])
    frame[3:5] = declared.to_bytes(2, "big")
    # 重算校验和，使该用例隔离“长度”单一缺陷
    frame[-2:] = codec.checksum16(bytes(frame[:-2])).to_bytes(2, "big")
    return bytes(frame)


def _mutate_checksum_bypass(rng: random.Random, base: bytes) -> bytes:
    frame = bytearray(base)
    frame[-1] ^= 0xFF  # 仅破坏校验和
    return bytes(frame)


def _mutate_payload_truncation(rng: random.Random, base: bytes) -> bytes:
    # 至少保留头+校验和长度，截断尾部；声明长度不变
    cut = rng.randint(1, max(1, len(base) - codec.MIN_FRAME_LEN))
    return base[: len(base) - cut]


_MUTATORS = {
    CLASS_LENGTH_OVERFLOW: _mutate_length_overflow,
    CLASS_CHECKSUM_BYPASS: _mutate_checksum_bypass,
    CLASS_PAYLOAD_TRUNCATION: _mutate_payload_truncation,
}


def generate(seed: int, count: int) -> list[dict]:
    """按固定种子确定性生成 count 条变异用例，三类轮转覆盖。"""
    rng = random.Random(seed)
    cases = []
    for i in range(count):
        payload = bytes(rng.randrange(256) for _ in range(rng.randint(1, 32)))
        base = codec.encode(payload)
        cls = MUTATION_CLASSES[i % len(MUTATION_CLASSES)]
        data = _MUTATORS[cls](rng, base)
        cases.append({
            "id": f"case-{i:04d}",
            "class": cls,
            "round": 0,
            "source": f"gen-{cls}",
            "data_hex": data.hex(),
        })
    return cases


def make_marker_case(case_id: str, marker: bytes, cls: str) -> dict:
    """构造合法帧但载荷携带特殊标记，供被测解析器缺陷模式触发失控/终止。"""
    payload = marker + b"-" + bytes(range(8))
    return {
        "id": case_id,
        "class": cls,
        "round": 0,
        "source": "injected",
        "data_hex": codec.encode(payload).hex(),
    }


def fingerprint(cases: list[dict]) -> str:
    """语料版本指纹：全部用例字节流顺序拼接的 SHA-256。"""
    h = hashlib.sha256()
    for c in cases:
        h.update(bytes.fromhex(c["data_hex"]))
    return h.hexdigest()


def counts_by_class(cases: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["class"]] = counts.get(c["class"], 0) + 1
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="framefuzz.corpus")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--out", default=None, help="可选：语料 JSON 输出路径")
    args = ap.parse_args(argv)
    cases = generate(args.seed, args.count)
    fp = fingerprint(cases)
    counts = counts_by_class(cases)
    print(f"seed={args.seed} count={len(cases)} fingerprint={fp}")
    for cls in MUTATION_CLASSES:
        print(f"  {cls}: {counts.get(cls, 0)}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"seed": args.seed, "fingerprint": fp,
                       "class_counts": counts, "cases": cases},
                      f, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
