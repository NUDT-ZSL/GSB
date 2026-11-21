from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, localcontext


@dataclass(frozen=True)
class BloomSummary:
    bit_width: int
    hash_count: int
    key_count: int
    bits_b64: str
    false_positive_upper: str

    def to_dict(self) -> dict[str, object]:
        return {
            "bit_width": self.bit_width,
            "hash_count": self.hash_count,
            "key_count": self.key_count,
            "bits_b64": self.bits_b64,
            "false_positive_upper": self.false_positive_upper,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "BloomSummary":
        return cls(
            bit_width=int(value["bit_width"]),
            hash_count=int(value["hash_count"]),
            key_count=int(value["key_count"]),
            bits_b64=str(value["bits_b64"]),
            false_positive_upper=str(value["false_positive_upper"]),
        )


class BloomFilter:
    def __init__(self, bit_width: int, hash_count: int, bits: bytearray) -> None:
        self.bit_width = bit_width
        self.hash_count = hash_count
        self.bits = bits

    @staticmethod
    def _positions(key: str, bit_width: int, hash_count: int):
        payload = key.encode("utf-8")
        first = int.from_bytes(hashlib.sha256(b"\x01" + payload).digest()[:8], "big")
        second = int.from_bytes(hashlib.sha256(b"\x02" + payload).digest()[:8], "big")
        second |= 1
        for index in range(hash_count):
            yield (first + index * second + index * index) % bit_width

    def add(self, key: str) -> None:
        for position in self._positions(key, self.bit_width, self.hash_count):
            self.bits[position >> 3] |= 1 << (position & 7)

    def might_contain(self, key: str) -> bool:
        for position in self._positions(key, self.bit_width, self.hash_count):
            if not self.bits[position >> 3] & (1 << (position & 7)):
                return False
        return True

    def to_summary(self, key_count: int) -> BloomSummary:
        encoded = base64.b64encode(bytes(self.bits)).decode("ascii")
        return BloomSummary(
            bit_width=self.bit_width,
            hash_count=self.hash_count,
            key_count=key_count,
            bits_b64=encoded,
            false_positive_upper=_false_positive_upper(
                self.bit_width,
                self.hash_count,
                key_count,
            ),
        )

    @classmethod
    def from_summary(cls, summary: BloomSummary) -> "BloomFilter":
        payload = base64.b64decode(summary.bits_b64.encode("ascii"), validate=True)
        expected = (summary.bit_width + 7) // 8
        if len(payload) != expected:
            raise ValueError("布隆摘要位宽与字节长度不一致")
        return cls(summary.bit_width, summary.hash_count, bytearray(payload))


def choose_bloom_parameters(
    key_count: int,
    bits_per_key: int,
    configured_hash_count: int,
) -> tuple[int, int]:
    if key_count <= 0:
        return 64, 1
    raw_bits = max(64, key_count * bits_per_key)
    bit_width = ((raw_bits + 63) // 64) * 64
    natural_hashes = max(1, int(round((bit_width / key_count) * math.log(2))))
    hash_count = max(1, min(configured_hash_count, natural_hashes))
    return bit_width, hash_count


def build_bloom(
    keys: list[str],
    bits_per_key: int,
    configured_hash_count: int,
) -> tuple[BloomFilter, BloomSummary]:
    distinct = sorted(set(keys))
    bit_width, hash_count = choose_bloom_parameters(
        len(distinct),
        bits_per_key,
        configured_hash_count,
    )
    bloom = BloomFilter(bit_width, hash_count, bytearray((bit_width + 7) // 8))
    for key in distinct:
        bloom.add(key)
    return bloom, bloom.to_summary(len(distinct))


def _false_positive_upper(bit_width: int, hash_count: int, key_count: int) -> str:
    with localcontext() as context:
        context.prec = 50
        m = Decimal(bit_width)
        exponent = Decimal(hash_count * max(key_count, 1)) / m
        occupancy = Decimal(1) - (-exponent).exp()
        probability = occupancy**hash_count
        quantized = probability.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING)
        return format(quantized, "f")
