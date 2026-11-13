"""Decide what an uploaded file really is from its bytes, not its name or the browser.

The extension must agree with the content. Office files are ZIP containers: they are
checked for the right parts, refused when macro-enabled, and refused when they would
expand to an unreasonable size (zip bombs).
"""

from __future__ import annotations

import io
import zipfile

from app.core.errors import ValidationFailed
from app.modules.knowledge.models import FileType

EXTENSIONS = {
    ".pdf": FileType.PDF,
    ".docx": FileType.DOCX,
    ".xlsx": FileType.XLSX,
    ".csv": FileType.CSV,
    ".txt": FileType.TXT,
    ".md": FileType.MARKDOWN,
    ".markdown": FileType.MARKDOWN,
}

MIME_TYPES = {
    FileType.PDF: "application/pdf",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    FileType.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    FileType.CSV: "text/csv",
    FileType.TXT: "text/plain",
    FileType.MARKDOWN: "text/markdown",
    FileType.HTML: "text/html",
}

MAX_ZIP_ENTRIES = 5000
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_RATIO = 200


class UnsupportedFile(ValidationFailed):
    code = "unsupported_file"


def _extension(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def check_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFile("The file is damaged or not a real Office document.") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UnsupportedFile("The document has too many parts to process safely.")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED_BYTES or total > max(len(data), 1) * MAX_RATIO:
        raise UnsupportedFile("The document expands to an unsafe size and was refused.")
    return archive


def _office_type(data: bytes) -> FileType:
    archive = check_zip(data)
    names = set(archive.namelist())
    try:
        content_types = archive.read("[Content_Types].xml").decode("utf-8", "replace").lower()
    except KeyError as exc:
        raise UnsupportedFile("The file is not a Word or Excel document.") from exc
    if "macroenabled" in content_types or "vbaproject.bin" in {n.split("/")[-1].lower() for n in names}:
        raise UnsupportedFile("Files with macros are not accepted. Save it as a normal .docx or .xlsx.")
    if "word/document.xml" in names:
        return FileType.DOCX
    if "xl/workbook.xml" in names:
        return FileType.XLSX
    raise UnsupportedFile("The file is not a Word or Excel document.")


def decode_text(data: bytes) -> str:
    """Text files must be UTF-8 (with or without BOM) or UTF-16 with a BOM."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise UnsupportedFile("The text file's encoding could not be read.") from exc
    if b"\0" in data[:8192]:
        raise UnsupportedFile("This looks like a binary file, not text.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedFile("Save the text file as UTF-8 and upload it again.") from exc


def detect(filename: str, data: bytes) -> FileType:
    """The file's real type, or UnsupportedFile with a reason the user can act on."""
    if not data:
        raise UnsupportedFile("The file is empty.")
    expected = EXTENSIONS.get(_extension(filename))
    if expected is None:
        raise UnsupportedFile("Supported files are PDF, Word (.docx), Excel (.xlsx), CSV, TXT and Markdown.")
    if data[:1024].lstrip().startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        actual = FileType.PDF
    elif data.startswith(b"PK\x03\x04"):
        actual = _office_type(data)
    elif data.startswith(b"\xd0\xcf\x11\xe0"):
        raise UnsupportedFile("Old Office formats (.doc, .xls) are not supported. Save as .docx or .xlsx.")
    else:
        decode_text(data)
        if expected not in (FileType.CSV, FileType.TXT, FileType.MARKDOWN):
            raise UnsupportedFile(f"The file's content does not match its {_extension(filename)} extension.")
        actual = expected
    if actual != expected:
        raise UnsupportedFile(f"The file's content does not match its {_extension(filename)} extension.")
    return actual
