"""Minimal deterministic CFB (OLE2 compound document) writer.

Used by the built-in sample generator. Streams smaller than the 4096-byte
mini-stream cutoff are stored in the mini stream (referenced through the
MiniFAT); larger streams use regular 512-byte sectors. Directory entries
are linked as a simple right-sibling chain; olefile (0.47) resolves
children by walking ``kids`` linearly, so red-black ordering is not
required for reading.
"""

import struct

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF

SECTOR_SIZE = 512
MINI_SECTOR_SIZE = 64
MINI_CUTOFF = 4096

_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


class Storage:
    def __init__(self, name, children=()):
        self.name = name
        self.children = list(children)


class Stream:
    def __init__(self, name, data: bytes):
        self.name = name
        self.data = bytes(data)


def _pad(data: bytes, multiple: int) -> bytes:
    rem = len(data) % multiple
    if rem:
        data += b"\x00" * (multiple - rem)
    return data


def build_cfb(root_children) -> bytes:
    """Build a version-3 CFB file from Storage/Stream nodes. Deterministic."""
    # Flatten the tree; sid 0 is the root entry.
    flat = [Storage("Root Entry")]

    def add(node):
        node._sid = len(flat)
        flat.append(node)
        node._kids = []
        if isinstance(node, Storage):
            for child in node.children:
                node._kids.append(add(child))
        return node._sid

    root = flat[0]
    root._kids = [add(c) for c in root_children]

    # Link siblings as a right chain; parent points at first child.
    for e in flat:
        e._left = NOSTREAM
        e._right = NOSTREAM
        e._child = NOSTREAM
    for e in flat:
        kids = getattr(e, "_kids", [])
        if kids:
            e._child = kids[0]
            for a, b in zip(kids, kids[1:]):
                flat[a]._right = b

    # Split streams into mini (< cutoff) and regular.
    mini_streams = [e for e in flat
                    if isinstance(e, Stream) and len(e.data) < MINI_CUTOFF]
    big_streams = [e for e in flat
                   if isinstance(e, Stream) and len(e.data) >= MINI_CUTOFF]

    # Mini stream content and per-stream mini-sector chains.
    minifat = []
    mini_content = bytearray()
    for e in mini_streams:
        e._mini_start = len(minifat)
        padded = _pad(e.data, MINI_SECTOR_SIZE)
        count = len(padded) // MINI_SECTOR_SIZE
        for k in range(count):
            minifat.append(len(minifat) + 1 if k + 1 < count else ENDOFCHAIN)
        mini_content += padded
    mini_stream_bytes = bytes(mini_content)
    minifat_sectors = ((len(minifat) + 127) // 128) if minifat else 0

    # Sector layout: dir, big streams, mini stream, minifat, FAT.
    dir_sectors = (len(flat) * 128 + SECTOR_SIZE - 1) // SECTOR_SIZE
    sec = dir_sectors
    for e in big_streams:
        e._start = sec
        e._padded = _pad(e.data, SECTOR_SIZE)
        e._nsec = len(e._padded) // SECTOR_SIZE
        sec += e._nsec
    if mini_stream_bytes:
        root._start = sec
        root._size = len(mini_stream_bytes)
        padded_mini = _pad(mini_stream_bytes, SECTOR_SIZE)
        sec += len(padded_mini) // SECTOR_SIZE
    else:
        root._start = ENDOFCHAIN
        root._size = 0
        padded_mini = b""
    first_minifat_sector = sec if minifat_sectors else ENDOFCHAIN
    sec += minifat_sectors
    data_end = sec

    fat_sectors = 1
    while True:
        total = data_end + fat_sectors
        need = (total + 127) // 128
        if need == fat_sectors:
            break
        fat_sectors = need
    total_sectors = data_end + fat_sectors
    if fat_sectors > 109:
        raise ValueError("file too large for header-only DIFAT")

    # Build FAT.
    fat = [FREESECT] * (fat_sectors * 128)

    def chain(start, count):
        for k in range(count):
            fat[start + k] = start + k + 1 if k + 1 < count else ENDOFCHAIN

    chain(0, dir_sectors)
    for e in big_streams:
        chain(e._start, e._nsec)
    if mini_stream_bytes:
        chain(root._start, len(padded_mini) // SECTOR_SIZE)
    if minifat_sectors:
        chain(first_minifat_sector, minifat_sectors)
    for s in range(data_end, total_sectors):
        fat[s] = FATSECT

    # Header.
    hdr = bytearray()
    hdr += _SIGNATURE
    hdr += b"\x00" * 16                       # CLSID
    hdr += struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6)
    hdr += b"\x00" * 6                        # reserved
    hdr += struct.pack("<I", 0)               # directory sectors (v3 => 0)
    hdr += struct.pack("<I", fat_sectors)
    hdr += struct.pack("<I", 0)               # first directory sector
    hdr += struct.pack("<I", 0)               # transaction signature
    hdr += struct.pack("<I", MINI_CUTOFF)
    hdr += struct.pack("<I", first_minifat_sector)
    hdr += struct.pack("<I", minifat_sectors)
    hdr += struct.pack("<I", ENDOFCHAIN)      # first DIFAT sector
    hdr += struct.pack("<I", 0)               # DIFAT sector count
    difat = list(range(data_end, total_sectors))
    difat += [FREESECT] * (109 - len(difat))
    hdr += struct.pack("<109I", *difat)
    assert len(hdr) == SECTOR_SIZE

    # Directory entries.
    dir_bytes = bytearray()
    for e in flat:
        dir_bytes += _pack_entry(e)
    dir_bytes = _pad(bytes(dir_bytes), dir_sectors * SECTOR_SIZE)

    out = bytearray(bytes(hdr) + dir_bytes)
    for e in big_streams:
        out += e._padded
    out += padded_mini
    for s in range(minifat_sectors):
        entries = minifat[s * 128:(s + 1) * 128]
        entries += [FREESECT] * (128 - len(entries))
        out += struct.pack("<128I", *entries)
    for s in range(fat_sectors):
        out += struct.pack("<128I", *fat[s * 128:(s + 1) * 128])
    return bytes(out)


def _pack_entry(e) -> bytes:
    name = e.name.encode("utf-16-le") + b"\x00\x00"
    if len(name) > 64:
        raise ValueError("entry name too long: %r" % e.name)
    if isinstance(e, Stream):
        etype = 2
        if len(e.data) < MINI_CUTOFF:
            start = e._mini_start
        else:
            start = e._start
        size = len(e.data)
    elif e.name == "Root Entry":
        etype, start, size = 5, e._start, e._size
    else:
        etype, start, size = 1, 0, 0
    out = name.ljust(64, b"\x00")
    out += struct.pack("<H", len(name))
    out += struct.pack("<BB", etype, 1)       # type, colour black
    out += struct.pack("<III", e._left, e._right, e._child)
    out += b"\x00" * 16                       # CLSID
    out += struct.pack("<I", 0)               # state bits
    out += struct.pack("<QQ", 0, 0)           # creation / modify time
    out += struct.pack("<I", start)
    out += struct.pack("<Q", size)
    assert len(out) == 128
    return out
