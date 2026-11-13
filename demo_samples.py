"""Deterministic PDF sample construction for the preflight scanner demo/tests.

Every PDF is assembled byte-for-byte in memory, so repeated runs produce
identical samples and reports.  No external attachment or network access is
required.

Samples are intentionally small but structurally valid PDF 1.7 files with a
classic cross-reference table, trailer and %%EOF marker.
"""

from __future__ import annotations

import zlib
from pathlib import Path


class PdfBuilder:
    """Assemble a minimal classic-xref PDF from raw object bodies."""

    def __init__(self) -> None:
        self._objects: dict[int, bytes] = {}

    def add(self, number: int, body: bytes) -> "PdfBuilder":
        if number <= 0:
            raise ValueError("object number must be positive")
        self._objects[number] = body
        return self

    def build(self, *, root: int = 1, encrypt: int | None = None) -> bytes:
        if root not in self._objects:
            raise ValueError("root object is missing")

        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}

        for number in sorted(self._objects):
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode("ascii")
            out += self._objects[number]
            if not out.endswith(b"\n"):
                out += b"\n"
            out += b"endobj\n"

        xref_offset = len(out)
        size = max(self._objects) + 1
        out += b"xref\n"
        out += f"0 {size}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for number in range(1, size):
            if number in offsets:
                out += f"{offsets[number]:010d} 00000 n \n".encode("ascii")
            else:
                out += b"0000000000 65535 f \n"

        trailer = f"<< /Size {size} /Root {root} 0 R".encode("ascii")
        if encrypt is not None:
            trailer += f" /Encrypt {encrypt} 0 R".encode("ascii")
        trailer += b" >>"
        out += b"trailer\n" + trailer + b"\n"
        out += f"startxref\n{xref_offset}\n".encode("ascii")
        out += b"%%EOF\n"
        return bytes(out)

    def build_xref_stream(self, *, root: int = 1) -> bytes:
        """Build a PDF 1.5-style file whose only cross-reference is a stream."""

        if root not in self._objects:
            raise ValueError("root object is missing")

        out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for number in sorted(self._objects):
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode("ascii")
            out += self._objects[number]
            if not out.endswith(b"\n"):
                out += b"\n"
            out += b"endobj\n"

        xref_number = max(self._objects) + 1
        xref_offset = len(out)
        size = xref_number + 1

        entries = bytearray()
        entries += bytes([0]) + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big")
        for number in range(1, size):
            if number in offsets:
                entries += (
                    bytes([1])
                    + offsets[number].to_bytes(4, "big")
                    + (0).to_bytes(2, "big")
                )
            else:
                entries += (
                    bytes([0])
                    + (0).to_bytes(4, "big")
                    + (65535).to_bytes(2, "big")
                )

        out += (
            f"{xref_number} 0 obj\n".encode("ascii")
            + f"<< /Type /XRef /Size {size} /Root {root} 0 R "
            f"/W [1 4 2] /Length {len(entries)} >>\nstream\n".encode("ascii")
            + bytes(entries)
            + b"\nendstream\nendobj\n"
        )
        out += f"startxref\n{xref_offset}\n".encode("ascii")
        out += b"%%EOF\n"
        return bytes(out)


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    """Return a stream object body with a correct direct /Length."""

    return (
        b"<< /Length "
        + str(len(data)).encode("ascii")
        + extra
        + b" >>\nstream\n"
        + data
        + b"\nendstream"
    )


def _base_objects(builder: PdfBuilder) -> None:
    """Add catalog/pages/page/content/font with no risky constructs."""

    builder.add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    builder.add(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    builder.add(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    )
    builder.add(
        4,
        _stream(b"BT /F1 12 Tf 20 100 Td (Preflight demo page) Tj ET"),
    )
    builder.add(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")


def make_normal_pdf() -> bytes:
    """Low risk: well-formed PDF without scripts/actions/attachments."""

    builder = PdfBuilder()
    _base_objects(builder)
    return builder.build()


def make_script_pdf(markers: int = 1) -> bytes:
    """Medium risk: document-level JavaScript actions (no auto action).

    Each JavaScript action contributes both a /JavaScript name-tree entry and
    a /S /JavaScript action dictionary, which exercises feature de-duplication.
    """

    if markers < 1:
        raise ValueError("markers must be >= 1")

    builder = PdfBuilder()
    _base_objects(builder)
    entries: list[bytes] = []
    next_number = 6
    for index in range(markers):
        entries.append(f"(script{index}) {next_number} 0 R".encode("ascii"))
        builder.add(
            next_number,
            b"<< /Type /Action /S /JavaScript /JS "
            + f"(app.alert\\('demo-{index}'\\);)".encode("ascii")
            + b" >>",
        )
        next_number += 1
    builder.add(
        1,
        b"<< /Type /Catalog /Pages 2 0 R /Names << /JavaScript << /Names ["
        + b" ".join(entries)
        + b"] >> >> >>",
    )
    return builder.build()


def make_auto_action_pdf() -> bytes:
    """Medium risk: OpenAction runs a harmless GoTo action automatically."""

    builder = PdfBuilder()
    _base_objects(builder)
    builder.add(6, b"<< /Type /Action /S /GoTo /D [3 0 R /Fit] >>")
    builder.add(1, b"<< /Type /Catalog /Pages 2 0 R /OpenAction 6 0 R >>")
    return builder.build()


def make_script_and_auto_pdf() -> bytes:
    """High risk: JavaScript plus an automatically executed action."""

    builder = PdfBuilder()
    _base_objects(builder)
    builder.add(
        6,
        b"<< /Type /Action /S /JavaScript /JS (app.alert\\('opened'\\);) >>",
    )
    builder.add(
        1,
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/Names << /JavaScript << /Names [(init) 6 0 R] >> >> "
        b"/OpenAction 6 0 R >>",
    )
    return builder.build()


def make_embedded_pdf(filename: str = "A.EXE") -> bytes:
    """Embed a file under /EmbeddedFiles with the given attachment name."""

    name = filename.encode("latin-1")
    builder = PdfBuilder()
    _base_objects(builder)
    builder.add(
        6,
        b"<< /Type /Filespec /F ("
        + name
        + b") /UF ("
        + name
        + b") /EF << /F 7 0 R >> >>",
    )
    builder.add(
        7,
        _stream(b"DEMO-PAYLOAD", b" /Type /EmbeddedFile"),
    )
    builder.add(
        1,
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles "
        b"<< /Names [(" + name + b") 6 0 R] >> >> >>",
    )
    return builder.build()


def make_all_features_pdf() -> bytes:
    """High risk: javascript + auto_action + embedded_executable."""

    builder = PdfBuilder()
    _base_objects(builder)
    builder.add(6, b"<< /Type /Action /S /JavaScript /JS (app.alert\\(1\\);) >>")
    builder.add(
        7,
        b"<< /Type /Filespec /F (payload.DLL) /UF (payload.DLL) "
        b"/EF << /F 8 0 R >> >>",
    )
    builder.add(8, _stream(b"DEMO-PAYLOAD", b" /Type /EmbeddedFile"))
    builder.add(9, b"<< /Type /Action /S /GoTo /D [3 0 R /Fit] >>")
    builder.add(
        1,
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/Names << /JavaScript << /Names [(init) 6 0 R] >> "
        b"/EmbeddedFiles << /Names [(payload.DLL) 7 0 R] >> >> "
        b"/OpenAction 9 0 R >>",
    )
    return builder.build()


def make_xref_stream_pdf(*, with_script: bool = True) -> bytes:
    """PDF 1.5 style file cross-referenced only by an /XRef stream."""

    builder = PdfBuilder()
    _base_objects(builder)
    if with_script:
        builder.add(
            6,
            b"<< /Type /Action /S /JavaScript /JS (app.alert\\(1\\);) >>",
        )
        builder.add(
            1,
            b"<< /Type /Catalog /Pages 2 0 R "
            b"/Names << /JavaScript << /Names [(init) 6 0 R] >> >> "
            b"/OpenAction 6 0 R >>",
        )
    return builder.build_xref_stream()


def make_compressed_objects_pdf() -> bytes:
    """Features stored in a FlateDecode /ObjStm object stream (PDF 1.5+).

    Objects 10 (JavaScript action) and 11 (file specification for pay.EXE)
    live only inside object 7's compressed stream, so a raw text scan of the
    file cannot see them.
    """

    objects = [
        (10, b"<< /Type /Action /S /JavaScript /JS (app.alert\\(1\\);) >>"),
        (
            11,
            b"<< /Type /Filespec /F (pay.EXE) /UF (pay.EXE) "
            b"/EF << /F 12 0 R >> >>",
        ),
    ]
    header_parts: list[str] = []
    body = bytearray()
    for number, payload in objects:
        header_parts.append(f"{number} {len(body)}")
        body += payload + b"\n"
    header = (" ".join(header_parts) + "\n").encode("ascii")
    compressed = zlib.compress(header + bytes(body))

    builder = PdfBuilder()
    _base_objects(builder)
    builder.add(
        7,
        _stream(
            compressed,
            b" /Type /ObjStm /N 2 /First "
            + str(len(header)).encode("ascii")
            + b" /Filter /FlateDecode",
        ),
    )
    builder.add(12, _stream(b"DEMO-PAYLOAD", b" /Type /EmbeddedFile"))
    builder.add(
        1,
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/Names << /JavaScript << /Names [(init) 10 0 R] >> "
        b"/EmbeddedFiles << /Names [(pay.EXE) 11 0 R] >> >> >>",
    )
    return builder.build()


def make_multi_subsection_xref_pdf() -> bytes:
    """High risk PDF whose xref table uses two subsections.

    ``xref 0 1`` holds the free-list head and ``xref 1 N`` holds the in-use
    objects.  Both are legal; a validator that assumes a single subsection
    would wrongly classify the file as damaged.
    """

    base = PdfBuilder()
    _base_objects(base)
    base.add(6, b"<< /Type /Action /S /JavaScript /JS (app.alert\\(1\\);) >>")
    base.add(9, b"<< /Type /Action /S /GoTo /D [3 0 R /Fit] >>")
    base.add(
        1,
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/Names << /JavaScript << /Names [(init) 6 0 R] >> >> "
        b"/OpenAction 9 0 R >>",
    )
    body = base.build()
    xref_offset = body.find(b"xref\n")
    after_header = xref_offset + len(b"xref\n")
    line_end = body.find(b"\n", after_header)
    count = int(body[after_header:line_end].split()[1])
    entries = body[line_end + 1 : line_end + 1 + count * 20]
    tail = body[line_end + 1 + count * 20 :]

    xref = (
        b"xref\n"
        + b"0 1\n"
        + entries[:20]
        + f"1 {count - 1}\n".encode("ascii")
        + entries[20:]
    )
    # The prefix before the table is unchanged, so startxref stays valid.
    return body[:xref_offset] + xref + tail


def make_encrypted_pdf(*, with_script: bool = True) -> bytes:
    """Structurally valid but encrypted PDF; content must not be inspected."""

    builder = PdfBuilder()
    _base_objects(builder)
    builder.add(
        6,
        b"<< /Filter /Standard /V 1 /R 2 "
        b"/O <00112233445566778899AABBCCDDEEFF> "
        b"/U <FFEEDDCCBBAA99887766554433221100> /P -4 >>",
    )
    if with_script:
        builder.add(
            7,
            b"<< /Type /Action /S /JavaScript /JS (app.alert\\(1\\);) >>",
        )
        builder.add(
            1,
            b"<< /Type /Catalog /Pages 2 0 R /OpenAction 7 0 R >>",
        )
    return builder.build(encrypt=6)


def make_broken_pdf() -> bytes:
    """Truncated PDF: header, but no xref/trailer/startxref/EOF."""

    return (
        b"%PDF-1.7\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R /OpenAction 6 0 R >>\nendobj\n"
        b"6 0 obj\n<< /Type /Action /S /JavaScript /JS (alert\\(1\\);) >>\nendobj\n"
    )


def make_not_a_pdf() -> bytes:
    return b"This is a plain text file with a .pdf extension.\n"


def _write(root: Path, relative: str, data: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def write_demo_tree(workdir: Path) -> Path:
    """Create the deterministic demo input tree and return its root."""

    incoming = workdir / "incoming"
    _write(incoming, "a.PDF", make_normal_pdf())
    _write(incoming, "b.txt", b"not a PDF and must be ignored\n")
    _write(incoming, "sub/c.pdf", make_normal_pdf())
    _write(incoming, "sub/notes.txt", b"ignored nested text file\n")
    _write(incoming, "script_only.pdf", make_script_pdf(markers=2))
    _write(incoming, "auto_action.pdf", make_auto_action_pdf())
    _write(incoming, "embedded_exe.pdf", make_embedded_pdf("A.EXE"))
    _write(incoming, "embedded_txt.pdf", make_embedded_pdf("readme.txt"))
    _write(incoming, "script_and_auto.pdf", make_script_and_auto_pdf())
    _write(incoming, "all_three.pdf", make_all_features_pdf())
    _write(incoming, "modern_xref_stream.pdf", make_xref_stream_pdf())
    _write(incoming, "packed_object_stream.pdf", make_compressed_objects_pdf())
    _write(incoming, "broken.pdf", make_broken_pdf())
    _write(incoming, "encrypted.pdf", make_encrypted_pdf(with_script=True))
    return incoming


if __name__ == "__main__":  # small manual helper
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_workspace")
    created = write_demo_tree(target)
    print(f"created demo samples in {created}")
