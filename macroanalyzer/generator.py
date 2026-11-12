"""Deterministic sample generator.

All documents, rule files and the legacy report are built from byte
templates with fixed seeds -- no external samples, no network.
"""

import io
import json
import os
import random
import zipfile

from .cfbwrite import Storage, Stream, build_cfb
from .vbacompress import compress_container
from .vbaproject import (MODULE_TYPE_CLASS, MODULE_TYPE_STD, build_dir_stream,
                         build_module_stream)

_ZIP_DATE = (2020, 1, 1, 0, 0, 0)

# ------------------------------------------------------------------ sources

FEATURE_RICH_MODULE = '''Attribute VB_Name = "ThisDocument"
Sub AutoOpen()
    Dim payload As String
    payload = Chr(65) + Chr(66)
    Shell("cmd")
    Dim note As String
    note = "Shell( AutoOpen CreateObject are inert inside a string"
    ' Shell("this_comment_must_not_count")
    Dim joined As String
    joined = "abc" _
        & "def"
End Sub
'''

FILE_MODULE = '''Attribute VB_Name = "Module1"
Sub WriteFile()
    Dim f As Integer
    f = FreeFile
    Open "C:\\temp\\out.txt" For Output As #f
    Print #f, "data"
    Close #f
End Sub
'''

PLAIN_MODULE = '''Attribute VB_Name = "Module2"
Option Explicit
Sub Helper()
    Dim x As Long
    x = 42
End Sub
'''

OLE_MODULE = '''Attribute VB_Name = "Module1"
Sub Auto_Open()
    Dim http As Object
    Set http = CreateObject("MSXML2.XMLHTTP")
    Dim stream As Object
    Set stream = CreateObject("ADODB.Stream")
    stream.SaveToFile "C:\\temp\\dl.exe"
End Sub
'''

DUP_MODULE_A = '''Attribute VB_Name = "Module1"
Sub AutoOpen()
    Shell("calc")
End Sub
'''

DUP_MODULE_B = '''Attribute VB_Name = "Module1"
Sub Workbook_Open()
    Dim u As String
    u = Chr(104) & Chr(116) & Chr(116) & Chr(112)
End Sub
'''


def _slow_module_source(idx: int, lines: int) -> str:
    parts = ['Attribute VB_Name = "Slow%02d"' % idx,
             "Sub Work_%d()" % idx]
    row = ["Dim v%05d As Long ' filler comment to parse" % i
           for i in range(lines)]
    parts.extend(row)
    parts.append("End Sub")
    return "\n".join(parts) + "\n"


# ----------------------------------------------------------------- builders

def build_vbaproject(modules) -> bytes:
    """modules: list of dicts {name, source (str|None), truncate_to (int|None),
    type}. source=None -> stream holds garbage."""
    dir_modules = []
    streams = []
    for m in modules:
        dir_modules.append({
            "name": m["name"],
            "stream_name": m["name"],
            "offset": 0,
            "type": m.get("type", MODULE_TYPE_STD),
        })
        source = m.get("source")
        if source is None:
            data = b"\xff" * 64
        else:
            data = build_module_stream(source.encode("cp1252"))
            cut = m.get("truncate_to")
            if cut is not None:
                data = data[:cut]
        streams.append(Stream(m["name"], data))
    dir_stream = compress_container(build_dir_stream(dir_modules))
    vba_children = [Stream("dir", dir_stream),
                    Stream("_VBA_PROJECT", b"\x01" * 128)] + streams
    return build_cfb([
        Storage("VBA", vba_children),
        Stream("PROJECT", b"Name=VBAProject\r\n"),
        Stream("PROJECTwm", b"\x00" * 32),
    ])


def build_ooxml(vbaproject: "bytes | None", extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        def put(name, data, compress=zipfile.ZIP_DEFLATED):
            zi = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            zi.compress_type = compress
            zf.writestr(zi, data)
        put("[Content_Types].xml",
            b'<?xml version="1.0"?><Types '
            b'xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        put("_rels/.rels", b'<?xml version="1.0"?><Relationships/>')
        put("word/document.xml", b'<?xml version="1.0"?><w:document/>')
        if vbaproject is not None:
            put("word/vbaProject.bin", vbaproject)
        for name, data in (extra or []):
            put(name, data, zipfile.ZIP_STORED)
    return buf.getvalue()


def build_ole_doc(modules) -> bytes:
    """OLE compound document with macros under Macros/VBA."""
    dir_modules = [{"name": m["name"], "stream_name": m["name"],
                    "offset": 0, "type": MODULE_TYPE_STD} for m in modules]
    dir_stream = compress_container(build_dir_stream(dir_modules))
    vba_children = [Stream("dir", dir_stream),
                    Stream("_VBA_PROJECT", b"\x01" * 128)]
    for m in modules:
        vba_children.append(
            Stream(m["name"],
                   build_module_stream(m["source"].encode("cp1252"))))
    return build_cfb([
        Storage("Macros", [Storage("VBA", vba_children),
                           Stream("PROJECT", b"Name=VBAProject\r\n")]),
        Stream("WordDocument", b"\x00" * 512),
    ])


def build_ole_nomacro() -> bytes:
    return build_cfb([Stream("WordDocument", b"\x00" * 512)])


# ------------------------------------------------------------------ samples

def _modules_feature_rich():
    return [
        {"name": "ThisDocument", "source": FEATURE_RICH_MODULE,
         "type": MODULE_TYPE_CLASS},
        {"name": "Module1", "source": FILE_MODULE},
        {"name": "Module2", "source": PLAIN_MODULE},
    ]


def generate_samples(out_dir: str):
    """Write all deterministic samples into *out_dir*. Returns file list."""
    os.makedirs(out_dir, exist_ok=True)
    written = []

    def put(name, data):
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        written.append(path)
        return path

    # 1. OOXML with macros (feature-rich: AutoOpen, Shell, Chr concat, ...)
    put("ooxml_macro.docm", build_ooxml(build_vbaproject(
        _modules_feature_rich())))

    # 2. OOXML without macros
    put("ooxml_nomacro.docx", build_ooxml(None))

    # 3. OLE document with macros
    put("ole_macro.doc", build_ole_doc([{"name": "Module1",
                                         "source": OLE_MODULE}]))

    # 4. random bytes (corrupt)
    rng = random.Random(1337)
    corrupt = bytes(rng.getrandbits(8) for _ in range(2048))
    put("corrupt.bin", corrupt)

    # 5. three modules, second module's compressed stream truncated
    trunc_src = PLAIN_MODULE.encode("cp1252")
    trunc_full = len(compress_container(trunc_src))
    put("truncated_modules.docm", build_ooxml(build_vbaproject([
        {"name": "ModA", "source": FEATURE_RICH_MODULE},
        {"name": "ModB", "source": PLAIN_MODULE,
         "truncate_to": max(4, trunc_full - 3)},
        {"name": "ModC", "source": FILE_MODULE},
    ])))

    # 6. slow document for the timeout test (many long modules)
    slow_modules = [{"name": "Slow%02d" % i,
                     "source": _slow_module_source(i, 1500)}
                    for i in range(40)]
    put("slow_timeout.docm", build_ooxml(build_vbaproject(slow_modules)))

    # 7. oversized document (> 1 MB)
    pad_rng = random.Random(7)
    padding = pad_rng.randbytes(2 * 1024 * 1024)
    put("oversize.docm", build_ooxml(
        build_vbaproject([{"name": "Module1", "source": PLAIN_MODULE}]),
        extra=[("word/padding.bin", padding)]))

    # 8. duplicate payload documents (identical bytes, different names)
    dup_bytes = build_ooxml(build_vbaproject(
        [{"name": "Module1", "source": DUP_MODULE_A}]))
    for name in ("dup_a.docm", "dup_b.docm", "dup_c.docm"):
        put(name, dup_bytes)
    put("dup_different.docm", build_ooxml(build_vbaproject(
        [{"name": "Module1", "source": DUP_MODULE_B}])))

    # 9. valid rule set (concat sample hits exactly R100 + R101)
    rules_valid = {
        "ruleset_id": "local-rules-1.0",
        "rules": [
            {"id": "R100", "name": "auto-exec + process creation",
             "severity": "high", "weight": 0.6,
             "features": ["auto_exec", "process_create"]},
            {"id": "R101", "name": "string concat obfuscation",
             "severity": "medium", "weight": 0.4,
             "features": ["obfuscation_string_concat"]},
            {"id": "R102", "name": "download + drop",
             "severity": "critical", "weight": 0.5,
             "features": ["network_download", "file_write"]},
        ],
    }
    put("rules_valid.json",
        json.dumps(rules_valid, indent=2).encode("utf-8"))

    # 10. syntactically broken rule set
    put("rules_broken.json", b'{"rules": [ {"id": "R1", }')

    # 11. legacy report (old schema: no evidence in features, missing fields)
    legacy = {
        "schema_version": "0.9",
        "documents": [
            {"id": "old1", "path": "old1.docm", "format": "ooxml",
             "status": "analyzed",
             "features": [{"type": "auto_exec"}, {"type": "process_create"}],
             "confidence": 0.5},
            {"id": "old2", "path": "old2.doc", "format": "ole",
             "status": "no_macro"},
        ],
    }
    put("legacy_report.json",
        json.dumps(legacy, indent=2).encode("utf-8"))

    return written
