"""MS-OVBA 2.4.1 compressed container (de)compression.

The compressor emits raw (uncompressed) chunks for full 4096-byte blocks and
literal-only compressed chunks for the final partial block; both are fully
spec-compliant. The decompressor handles raw chunks and compressed chunks
with literal and copy tokens.
"""

import struct

SIGNATURE_BYTE = 0x01
CHUNK_SIGNATURE = 0b011


class VBACompressionError(Exception):
    """Raised when a compressed container is malformed or truncated."""


def compress_container(data: bytes) -> bytes:
    """Compress *data* into a CompressedContainer (deterministic)."""
    out = bytearray([SIGNATURE_BYTE])
    for i in range(0, len(data), 4096):
        block = data[i:i + 4096]
        if len(block) == 4096:
            # raw chunk: flag=0, signature=0b011, size=4095
            out += struct.pack("<H", (CHUNK_SIGNATURE << 12) | 0x0FFF)
            out += block
        else:
            # literal-only compressed chunk
            chunk = bytearray()
            for j in range(0, len(block), 8):
                chunk.append(0x00)  # flag byte: 8 literal tokens
                chunk += block[j:j + 8]
            header = 0x8000 | (CHUNK_SIGNATURE << 12) | (len(chunk) - 1)
            out += struct.pack("<H", header)
            out += chunk
    return bytes(out)


def decompress_container(container: bytes) -> bytes:
    """Decompress a CompressedContainer. Raises VBACompressionError."""
    if len(container) < 1 or container[0] != SIGNATURE_BYTE:
        raise VBACompressionError("bad container signature byte")
    pos = 1
    out = bytearray()
    while pos < len(container):
        if pos + 2 > len(container):
            raise VBACompressionError(
                "truncated chunk header at offset %d" % pos)
        (header,) = struct.unpack_from("<H", container, pos)
        pos += 2
        chunk_size = (header & 0x0FFF) + 3
        signature = (header >> 12) & 0x7
        flag = (header >> 15) & 0x1
        if signature != CHUNK_SIGNATURE:
            raise VBACompressionError(
                "bad chunk signature 0x%x at offset %d" % (signature, pos - 2))
        data_len = chunk_size - 2
        if pos + data_len > len(container):
            raise VBACompressionError(
                "truncated chunk data at offset %d: need %d bytes, have %d"
                % (pos, data_len, len(container) - pos))
        data = container[pos:pos + data_len]
        pos += data_len
        if flag == 0:
            if data_len != 4096:
                raise VBACompressionError(
                    "raw chunk must be exactly 4096 bytes, got %d" % data_len)
            out += data
        else:
            out += _decompress_chunk(data)
    return bytes(out)


def _decompress_chunk(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        flags = data[i]
        i += 1
        for bit in range(8):
            if i >= len(data):
                break
            if flags & (1 << bit):
                if i + 2 > len(data):
                    raise VBACompressionError("truncated copy token")
                token = data[i] | (data[i + 1] << 8)
                i += 2
                current = len(out)
                # bits used by the offset: ceil(log2(current)), minimum 4
                bit_count = max(4, (current - 1).bit_length()) if current > 1 else 4
                length_mask = 0xFFFF >> bit_count
                length = (token & length_mask) + 3
                offset = (token >> (16 - bit_count)) + 1
                if offset > current:
                    raise VBACompressionError("copy token offset out of range")
                start = current - offset
                for k in range(length):
                    out.append(out[start + k])
            else:
                out.append(data[i])
                i += 1
    return bytes(out)
