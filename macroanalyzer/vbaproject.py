"""Parsing and building of vbaProject.bin structures (MS-OVBA).

A vbaProject.bin is a CFB container holding a ``VBA`` storage with the
compressed ``dir`` stream and one stream per module. OLE documents embed
the same layout under a ``Macros`` storage.
"""

import io
import struct

import olefile

from .vbacompress import compress_container, decompress_container

# dir-stream record ids (subset of MS-OVBA 2.3.4.2)
REC_SYSKIND = 0x0001
REC_CODEPAGE = 0x0003
REC_PROJECTNAME = 0x0004
REC_MODULES = 0x000F
REC_TERMINATOR = 0x0010
REC_MODULENAME = 0x0019
REC_MODULENAMEUNICODE = 0x0047
REC_MODULESTREAMNAME = 0x001A
REC_MODULESTREAMNAMEUNICODE = 0x0032
REC_MODULEDOCSTRING = 0x001C
REC_MODULEDOCSTRINGUNICODE = 0x0048
REC_MODULEHELPCONTEXT = 0x001E
REC_MODULECOOKIE = 0x002C
REC_MODULETYPESTD = 0x0021
REC_MODULETYPECLASS = 0x0022
REC_MODULEOFFSET = 0x0031
REC_MODULEEND = 0x002B

MODULE_TYPE_STD = REC_MODULETYPESTD
MODULE_TYPE_CLASS = REC_MODULETYPECLASS

VBA_PREFIXES = ("VBA", "Macros/VBA")


class VBAProjectError(Exception):
    """Raised when the vbaProject container or dir stream is malformed."""


# ---------------------------------------------------------------- building

def build_dir_stream(modules) -> bytes:
    """Build the (uncompressed) dir stream for *modules*.

    Each module is a dict with keys: name, stream_name, offset, type.
    """
    out = bytearray()

    def rec(rid, data):
        out.extend(struct.pack("<HI", rid, len(data)))
        out.extend(data)

    rec(REC_SYSKIND, struct.pack("<I", 1))            # Win32
    rec(REC_CODEPAGE, struct.pack("<H", 1252))
    rec(REC_PROJECTNAME, b"VBAProject")
    rec(REC_MODULES, struct.pack("<H", len(modules)))
    for m in modules:
        name = m["name"].encode("cp1252")
        sname = m["stream_name"].encode("cp1252")
        rec(REC_MODULENAME, name)
        rec(REC_MODULENAMEUNICODE, m["name"].encode("utf-16-le"))
        rec(REC_MODULESTREAMNAME, sname)
        rec(REC_MODULESTREAMNAMEUNICODE, m["stream_name"].encode("utf-16-le"))
        rec(REC_MODULEDOCSTRING, b"")
        rec(REC_MODULEDOCSTRINGUNICODE, b"")
        rec(REC_MODULEOFFSET, struct.pack("<I", m["offset"]))
        rec(REC_MODULEHELPCONTEXT, struct.pack("<I", 0))
        rec(REC_MODULECOOKIE, struct.pack("<H", 0xFFFF))
        rec(m.get("type", MODULE_TYPE_STD), b"")
        rec(REC_MODULEEND, b"")
    rec(REC_TERMINATOR, b"")
    return bytes(out)


def build_module_stream(source: bytes, offset: int = 0) -> bytes:
    """Module stream = *offset* bytes of performance cache + compressed source."""
    return b"\x00" * offset + compress_container(source)


# ---------------------------------------------------------------- parsing

def parse_dir_stream(data: bytes):
    """Parse a decompressed dir stream. Returns a list of module dicts."""
    modules = []
    pos = 0
    declared = None
    current = None
    while pos + 6 <= len(data):
        rid, size = struct.unpack_from("<HI", data, pos)
        pos += 6
        if pos + size > len(data):
            raise VBAProjectError(
                "truncated dir record 0x%04X at offset %d" % (rid, pos - 6))
        payload = data[pos:pos + size]
        pos += size
        if rid == REC_MODULES:
            if size < 2:
                raise VBAProjectError("malformed PROJECTMODULES record")
            declared = struct.unpack("<H", payload[:2])[0]
        elif rid == REC_MODULENAME:
            current = {"name": payload.decode("cp1252", "replace")}
        elif rid == REC_MODULESTREAMNAME and current is not None:
            current["stream_name"] = payload.decode("cp1252", "replace")
        elif rid == REC_MODULEOFFSET and current is not None:
            if size < 4:
                raise VBAProjectError("malformed MODULEOFFSET record")
            current["offset"] = struct.unpack("<I", payload[:4])[0]
        elif rid == REC_MODULEEND and current is not None:
            modules.append(current)
            current = None
        elif rid == REC_TERMINATOR:
            break
    if declared is not None and len(modules) != declared:
        raise VBAProjectError(
            "dir stream declares %d modules but %d were parsed"
            % (declared, len(modules)))
    return modules


def find_vba_prefix(ole) -> "str | None":
    """Return the storage prefix containing the VBA dir stream, or None."""
    streams = {"/".join(p) for p in ole.listdir()}
    for prefix in VBA_PREFIXES:
        if prefix + "/dir" in streams:
            return prefix
    return None


def open_container(container_bytes: bytes):
    """Open a vbaProject.bin / OLE document. Returns (ole, prefix).

    Raises VBAProjectError when the container is not a valid CFB or has no
    VBA dir stream.
    """
    try:
        ole = olefile.OleFileIO(io.BytesIO(container_bytes))
    except Exception as exc:
        raise VBAProjectError("not a valid OLE compound file: %s" % exc)
    try:
        prefix = find_vba_prefix(ole)
    except Exception as exc:
        ole.close()
        raise VBAProjectError("failed to enumerate streams: %s" % exc)
    if prefix is None:
        ole.close()
        raise VBAProjectError("no VBA dir stream found")
    return ole, prefix


def list_modules(ole, prefix: str):
    """Decompress and parse the dir stream. Returns module metadata list."""
    try:
        raw = ole.openstream(prefix + "/dir").read()
    except Exception as exc:
        raise VBAProjectError("cannot read dir stream: %s" % exc)
    try:
        dir_data = decompress_container(raw)
    except Exception as exc:
        raise VBAProjectError("cannot decompress dir stream: %s" % exc)
    return parse_dir_stream(dir_data)


def read_module_source(ole, prefix: str, meta) -> bytes:
    """Read and decompress one module's source. Raises on failure."""
    stream_path = prefix + "/" + meta["stream_name"]
    raw = ole.openstream(stream_path).read()
    offset = meta.get("offset", 0)
    if offset > len(raw):
        raise VBAProjectError(
            "module source offset %d beyond stream size %d"
            % (offset, len(raw)))
    return decompress_container(raw[offset:])
