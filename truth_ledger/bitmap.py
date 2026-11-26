from __future__ import annotations

import math

from .errors import LedgerError


def full_row_bitmap(row_count: int) -> bytes:
    if row_count < 0:
        raise ValueError("row_count must be non-negative")
    byte_count = math.ceil(row_count / 8)
    bitmap = bytearray(b"\xff" * byte_count)
    if row_count % 8:
        bitmap[-1] = (1 << (row_count % 8)) - 1
    return bytes(bitmap)


def validate_row_bitmap(
    bitmap: bytes,
    row_count: int,
    *,
    stage: str,
    where: str,
) -> None:
    expected_bytes = math.ceil(row_count / 8)
    if not isinstance(bitmap, (bytes, bytearray, memoryview)):
        raise LedgerError(
            "E_BITMAP_COVER",
            "bitmap is not a binary BLOB",
            stage=stage,
            details={"where": where},
        )
    bitmap = bytes(bitmap)
    if len(bitmap) < expected_bytes:
        raise LedgerError(
            "E_BITMAP_COVER",
            "bitmap is shorter than required to cover shard rows",
            stage=stage,
            details={
                "where": where,
                "expected_bytes": expected_bytes,
                "actual_bytes": len(bitmap),
                "row_count": row_count,
            },
        )
    if len(bitmap) != expected_bytes:
        raise LedgerError(
            "E_BITMAP_COVER",
            "bitmap contains bits outside the declared shard boundary",
            stage=stage,
            details={
                "where": where,
                "expected_bytes": expected_bytes,
                "actual_bytes": len(bitmap),
                "row_count": row_count,
            },
        )
    for row in range(row_count):
        byte_index = row // 8
        bit_index = row % 8
        if not (bitmap[byte_index] >> bit_index) & 1:
            raise LedgerError(
                "E_BITMAP_COVER",
                "bitmap does not cover every shard row",
                stage=stage,
                details={"where": where, "missing_row_offset": row},
            )
    valid_bits = row_count % 8
    if valid_bits:
        padding_mask = 0xFF ^ ((1 << valid_bits) - 1)
        if bitmap[-1] & padding_mask:
            bad_bit = next(
                bit
                for bit in range(valid_bits, 8)
                if (bitmap[-1] >> bit) & 1
            )
            raise LedgerError(
                "E_BITMAP_COVER",
                "bitmap sets a row bit outside the shard boundary",
                stage=stage,
                details={"where": where, "out_of_range_bit": bad_bit},
            )
