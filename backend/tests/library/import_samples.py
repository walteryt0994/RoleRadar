"""Synthetic resume files for the import tests (U2-B1). No real resume is used.

make_pdf builds a text PDF from scratch: its font maps every 2-byte code to
the same Unicode code point, so Chinese, English and mixed lines come back
from pypdf exactly as written. A page given as None has a drawing but no
text (like a scanned page); UNREADABLE gives a page whose content uses a
filter no reader knows; INFLATING one whose content inflates to more than
any reading limit. make_docx builds a minimal Word package around a body;
p() and table() write its paragraphs and tables.
"""
import io
import zipfile
import zlib

from pypdf import PdfReader, PdfWriter

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"synthetic compound file" * 4
UNREADABLE = "unreadable page"
INFLATING = "inflating page"

_TO_UNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def _stream(content: bytes) -> bytes:
    data = zlib.compress(content)

    return b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream"


def make_pdf(pages: list[list[str] | None]) -> bytes:
    """A PDF with one page per entry: its lines of text, or None for a page without text."""
    objects: list[bytes | None] = []

    def add(body: bytes | None) -> int:
        objects.append(body)
        return len(objects)

    to_unicode = add(_stream(_TO_UNICODE))
    descendant = add(
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SyntheticSans "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 1000 /CIDToGIDMap /Identity >>"
    )
    font = add(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /SyntheticSans /Encoding /Identity-H "
        b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (descendant, to_unicode)
    )
    pages_id = add(None)
    page_ids = []

    for lines in pages:
        if lines == UNREADABLE:
            junk = b"synthetic damaged stream"
            page_ids.append(_page(add, pages_id, font, b"<< /Length %d /Filter /SyntheticUnknownFilter >>\nstream\n" % len(junk) + junk + b"\nendstream"))
            continue

        if lines == INFLATING:
            # 21 MiB of spaces: a few kilobytes on disk.
            page_ids.append(_page(add, pages_id, font, _stream(b" " * (21 * 1024 * 1024))))
            continue

        if lines is None:
            content = b"0 0 m 200 200 l S"
        else:
            ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
            ops += [f"<{''.join(f'{ord(ch):04X}' for ch in line)}> Tj T*" for line in lines]
            ops.append("ET")
            content = "\n".join(ops).encode("ascii")

        page_ids.append(_page(add, pages_id, font, _stream(content)))

    kids = b" ".join(b"%d 0 R" % number for number in page_ids)
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []

    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")

    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))

    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)

    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref))

    return out.getvalue()


def _page(add, pages_id: int, font: int, stream: bytes) -> int:
    content_id = add(stream)

    return add(
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> "
        b"/Contents %d 0 R >>" % (pages_id, font, content_id)
    )


def encrypted_pdf() -> bytes:
    writer = PdfWriter()

    for page in PdfReader(io.BytesIO(make_pdf([["合成加密简历"]]))).pages:
        writer.add_page(page)

    writer.encrypt(user_password="synthetic-password", owner_password="synthetic-owner")
    out = io.BytesIO()
    writer.write(out)

    return out.getvalue()


def p(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def table(rows: list[list[str]]) -> str:
    cells = "".join("<w:tr>" + "".join(f"<w:tc>{p(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows)

    return f"<w:tbl>{cells}</w:tbl>"


def make_docx(body: str = "", *, main_type: str = DOCX_MAIN, extra: dict | None = None, document: str | None = None) -> bytes:
    """A Word package; its body, or a whole document part of its own."""
    document = document or (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{WORD}" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body}</w:body></w:document>"
    )
    out = io.BytesIO()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{main_type}"/></Types>',
        )
        package.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        # An outside link that a reader must never follow.
        package.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            'Target="http://isolation-probe.invalid/resume" TargetMode="External"/></Relationships>',
        )
        package.writestr("word/document.xml", document)

        for name, data in (extra or {}).items():
            package.writestr(name, data)

    return out.getvalue()


def bomb_docx(inflated: int) -> bytes:
    """A DOCX whose body part inflates to about inflated bytes from a few kilobytes."""
    filler = p("合成") * (inflated // len(p("合成")) + 1)

    return make_docx(filler)


def many_members_docx(count: int) -> bytes:
    return make_docx(p("合成"), extra={f"word/media/image{number}.png": b"x" for number in range(count)})
