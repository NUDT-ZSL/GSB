"""Deterministic varint + delta encoding and posting-list block codec.

Block binary layout (all integers are unsigned varints, little-endian base-128):
    header: block_count, first_doc_id, payload_byte_len
    payload: delta-encoded doc_ids (first delta = doc_id - first_doc_id),
             followed by raw term frequencies.
"""

from .errors import ConfigInvalidError


def encode_varint(value):
    if not isinstance(value, int) or value < 0:
        raise ConfigInvalidError("varint requires a non-negative int", value=value)
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(buf, offset):
    shift = 0
    result = 0
    while True:
        if offset >= len(buf):
            raise ConfigInvalidError("truncated varint", offset=offset)
        byte = buf[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise ConfigInvalidError("varint overflow", offset=offset)


def encode_block(postings):
    """postings: list of (doc_id, tf) sorted by doc_id ascending."""
    if not postings:
        raise ConfigInvalidError("cannot encode empty block")
    count = len(postings)
    first_doc_id = postings[0][0]
    payload = bytearray()
    prev = first_doc_id
    for doc_id, _tf in postings:
        delta = doc_id - prev
        if delta < 0:
            raise ConfigInvalidError("postings not sorted by doc_id", doc_id=doc_id)
        payload += encode_varint(delta)
        prev = doc_id
    for _doc_id, tf in postings:
        if tf <= 0:
            raise ConfigInvalidError("term frequency must be positive", tf=tf)
        payload += encode_varint(tf)
    header = encode_varint(count) + encode_varint(first_doc_id) + encode_varint(len(payload))
    return bytes(header) + bytes(payload)


def decode_block(buf, offset=0):
    """Decode one block starting at offset. Returns (postings, next_offset)."""
    count, offset = decode_varint(buf, offset)
    first_doc_id, offset = decode_varint(buf, offset)
    payload_len, offset = decode_varint(buf, offset)
    payload = buf[offset:offset + payload_len]
    if len(payload) != payload_len:
        raise ConfigInvalidError("truncated block payload")
    pos = 0
    doc_ids = []
    doc_id = first_doc_id
    for _ in range(count):
        delta, pos = decode_varint(payload, pos)
        doc_id += delta
        doc_ids.append(doc_id)
    tfs = []
    for _ in range(count):
        tf, pos = decode_varint(payload, pos)
        tfs.append(tf)
    if pos != payload_len:
        raise ConfigInvalidError("block payload has trailing bytes")
    return list(zip(doc_ids, tfs)), offset + payload_len


def chunk_postings(postings, block_size):
    """Split a posting list into blocks of at most block_size entries."""
    if not isinstance(block_size, int) or block_size <= 0:
        raise ConfigInvalidError("block_size must be a positive integer",
                                 block_size=block_size)
    return [postings[i:i + block_size]
            for i in range(0, len(postings), block_size)]
