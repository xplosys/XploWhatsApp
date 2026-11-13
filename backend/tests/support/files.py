"""Small real documents for extraction tests: PDF with selectable text, DOCX with
headings and a table, XLSX with a header row, and a few hostile files."""

from __future__ import annotations

import io
import zipfile


def make_pdf(pages: list[str], title: str | None = None) -> bytes:
    """A valid PDF (with xref table) whose pages contain the given lines as text."""
    objects: list[bytes] = []
    page_ids = []
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        page_ids.append(3 + 2 * i)
        lines = []
        for n, line in enumerate(text.split("\n")):
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"BT /F1 12 Tf 72 {720 - 18 * n} Td ({safe}) Tj ET")
        stream = "\n".join(lines).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    all_objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        *objects,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    info_id = None
    if title:
        all_objects.append(f"<< /Title ({title}) >>".encode())
        info_id = len(all_objects)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(all_objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(all_objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    trailer = f"<< /Size {len(all_objects) + 1} /Root 1 0 R"
    if info_id:
        trailer += f" /Info {info_id} 0 R"
    out.write(f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_docx() -> bytes:
    import docx

    document = docx.Document()
    document.core_properties.title = "Clinic handbook"
    document.add_heading("Treatments", level=1)
    document.add_heading("Teeth whitening", level=2)
    document.add_paragraph("Professional teeth whitening takes 60 minutes and costs ₹6,000.")
    document.add_heading("Policies", level=1)
    document.add_paragraph("Cancel at least 24 hours before your appointment.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Service"
    table.cell(0, 1).text = "Price"
    table.cell(1, 0).text = "Dental X-ray"
    table.cell(1, 1).text = "₹350"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Price list"
    sheet.append(["Service", "Price (₹)", "Duration"])
    sheet.append(["Braces consultation", 500, "30 min"])
    sheet.append(["Crown", 8000, "2 visits"])
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


def make_macro_xlsx() -> bytes:
    """An .xlsx-named file whose content types declare macros."""
    original = zipfile.ZipFile(io.BytesIO(make_xlsx()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"spreadsheetml.sheet.main+xml", b"sheet.macroEnabled.main+xml")
            out.writestr(item, data)
        out.writestr("xl/vbaProject.bin", b"\0" * 64)
    return buf.getvalue()


def make_zip_bomb_docx() -> bytes:
    """A .docx whose one part expands to 300 MB."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("[Content_Types].xml", "<Types/>")
        out.writestr("word/document.xml", b"0" * (300 * 1024 * 1024))
    return buf.getvalue()
