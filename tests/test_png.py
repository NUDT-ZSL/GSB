from __future__ import annotations

import binascii
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

from layout_engine.errors import ImageFormatError
from layout_engine.png import load_png_gray, load_png_gray_bytes


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def _png(width: int, height: int, bit_depth: int, color_type: int, rows: list[bytes]) -> bytes:
    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _chunk(b"IEND", b"")
    )


class PNGParserTests(unittest.TestCase):
    def test_grayscale_and_rgb_decode_to_same_luma(self) -> None:
        gray = np.array([[20, 80], [140, 240]], dtype=np.uint8)
        gray_rows = [row.tobytes() for row in gray]
        gray_payload = _png(2, 2, 8, 0, gray_rows)
        decoded_gray, info = load_png_gray_bytes(
            gray_payload, page="gray.png", expected_width=2, expected_height=2
        )
        self.assertEqual(info.pixel_mode, "L")
        self.assertTrue(np.array_equal(decoded_gray, gray))

        rgb_rows = []
        for row in gray:
            rgb_rows.append(b"".join(bytes([value, value, value]) for value in row))
        rgb_payload = _png(2, 2, 8, 2, rgb_rows)
        decoded_rgb, info_rgb = load_png_gray_bytes(
            rgb_payload, page="rgb.png", expected_width=2, expected_height=2
        )
        self.assertEqual(info_rgb.pixel_mode, "RGB")
        self.assertTrue(np.array_equal(decoded_rgb, gray))

    def test_wrong_dimension_and_signature_are_image_format_errors(self) -> None:
        payload = _png(2, 2, 8, 0, [b"\x01\x02", b"\x03\x04"])
        with self.assertRaises(ImageFormatError) as context:
            load_png_gray_bytes(
                payload, page="wrong.png", expected_width=3, expected_height=2
            )
        self.assertEqual(context.exception.code, "E_IMAGE_FORMAT")
        with self.assertRaises(ImageFormatError):
            load_png_gray_bytes(
                b"not-png", page="bad.png", expected_width=2, expected_height=2
            )

    def test_unsupported_bit_depth_is_rejected(self) -> None:
        payload = _png(2, 2, 16, 0, [b"\x00" * 4, b"\x00" * 4])
        with self.assertRaises(ImageFormatError) as context:
            load_png_gray_bytes(
                payload, page="deep.png", expected_width=2, expected_height=2
            )
        self.assertEqual(context.exception.code, "E_IMAGE_FORMAT")

    def test_file_loader_returns_image_and_raw_bytes(self) -> None:
        payload = _png(1, 1, 8, 0, [b"\x7f"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "one.png"
            path.write_bytes(payload)
            image, info, raw = load_png_gray(
                path, page="one.png", expected_width=1, expected_height=1
            )
        self.assertEqual(image.shape, (1, 1))
        self.assertEqual(info.width, 1)
        self.assertEqual(raw, payload)


if __name__ == "__main__":
    unittest.main()
