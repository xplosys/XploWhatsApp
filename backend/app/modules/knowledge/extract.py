"""Text extraction. Every extractor returns sections: a heading path, the text under it
and where it came from (page, sheet, rows), which later becomes a chunk's source note.

Extraction runs in the knowledge worker on files that already passed type checks and
the virus scan, with caps on pages, rows and total characters.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from app.core.config import get_settings
from app.modules.knowledge.filetypes import check_zip, decode_text
from app.modules.knowledge.models import FileType

MAX_PDF_PAGES = 2000
MAX_SHEET_ROWS = 50_000
MAX_CELL_CHARS = 2000


class ExtractionError(Exception):
    def __init__(self, message: str, code: str = "extraction_failed"):
        super().__init__(message)
        self.code = code


@dataclass
class Section:
    text: str
    headings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Extracted:
    sections: list[Section]
    title: str | None = None

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.sections)


_SPACES = re.compile(r"[ \t\u00a0]+")
_BLANKS = re.compile(r"\n{3,}")


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def _cap(sections: list[Section]) -> list[Section]:
    limit = get_settings().knowledge_max_extracted_chars
    out, total = [], 0
    for s in sections:
        if not s.text:
            continue
        if total + len(s.text) > limit:
            raise ExtractionError(
                f"The document has more than {limit:,} characters of text. Split it into smaller files.",
                "too_large",
            )
        total += len(s.text)
        out.append(s)
    if not out:
        raise ExtractionError("No text could be found in this document.", "no_text")
    return out


# ---- PDF --------------------------------------------------------------------------


def extract_pdf(data: bytes) -> Extracted:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError(
                "The PDF is password-protected. Upload a copy without a password.", "encrypted"
            )
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ExtractionError(f"The PDF has more than {MAX_PDF_PAGES} pages.", "too_large")
        sections = []
        for number, page in enumerate(reader.pages, start=1):
            text = clean(page.extract_text() or "")
            if text:
                sections.append(Section(text, [], {"page": number}))
        title = None
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)[:200]
    except ExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise ExtractionError(f"The PDF could not be read: {exc}"[:300]) from exc
    if not sections and len(reader.pages) > 0:
        raise ExtractionError(
            "This PDF has no selectable text (it looks scanned). "
            "Text recognition (OCR) is not supported yet.",
            "no_text",
        )
    return Extracted(_cap(sections), title)


# ---- Word -------------------------------------------------------------------------


def _heading_level(style_name: str) -> int | None:
    name = style_name.lower()
    if name == "title":
        return 1
    match = re.match(r"heading (\d)", name)
    return int(match.group(1)) if match else None


def extract_docx(data: bytes) -> Extracted:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    check_zip(data)
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises a range of parser errors
        raise ExtractionError(f"The Word document could not be read: {exc}"[:300]) from exc
    sections: list[Section] = []
    path: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        text = clean("\n".join(buffer))
        if text:
            sections.append(Section(text, list(path)))
        buffer.clear()

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            level = _heading_level(block.style.name if block.style is not None else "")
            if level and text:
                flush()
                path[:] = [*path[: level - 1], text]
            elif text:
                buffer.append(text)
        elif isinstance(block, Table):
            rows = []
            for row in block.rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = cell.text.strip()[:MAX_CELL_CHARS]
                    if not cells or cells[-1] != value:  # merged cells repeat
                        cells.append(value)
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                buffer.append("\n".join(rows))
    flush()
    title = document.core_properties.title or None
    return Extracted(_cap(sections), title[:200] if title else None)


# ---- spreadsheets -----------------------------------------------------------------


def _rows_to_sections(rows: list[list[str]], sheet: str | None) -> list[Section]:
    """First non-empty row is the header; each later row becomes `Header: value` lines,
    which keeps prices next to the item they belong to."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    lines: list[str] = []
    start = 2
    sections: list[Section] = []
    for index, row in enumerate(rows[1:], start=2):
        pairs = []
        for col, value in enumerate(row):
            value = value.strip()[:MAX_CELL_CHARS]
            if not value:
                continue
            name = header[col] if col < len(header) and header[col] else f"Column {col + 1}"
            pairs.append(f"{name}: {value}")
        if pairs:
            lines.append("; ".join(pairs))
        if len(lines) >= 40:
            sections.append(
                Section(
                    "\n".join(lines), [sheet] if sheet else [], {"sheet": sheet, "rows": f"{start}-{index}"}
                )
            )
            lines, start = [], index + 1
    if lines:
        sections.append(
            Section(
                "\n".join(lines), [sheet] if sheet else [], {"sheet": sheet, "rows": f"{start}-{len(rows)}"}
            )
        )
    if len(rows) == 1:  # a header-only sheet is still text
        sections.append(Section("; ".join(header), [sheet] if sheet else [], {"sheet": sheet}))
    return sections


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def extract_xlsx(data: bytes) -> Extracted:
    from openpyxl import load_workbook

    check_zip(data)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises a range of parser errors
        raise ExtractionError(f"The Excel file could not be read: {exc}"[:300]) from exc
    sections: list[Section] = []
    try:
        for sheet in workbook.worksheets:
            rows: list[list[str]] = []
            for count, row in enumerate(sheet.iter_rows(values_only=True)):
                if count >= MAX_SHEET_ROWS:
                    raise ExtractionError(
                        f"Sheet {sheet.title} has more than {MAX_SHEET_ROWS:,} rows.", "too_large"
                    )
                rows.append([_cell(v) for v in row])
            sections += _rows_to_sections(rows, sheet.title)
    finally:
        workbook.close()
    return Extracted(_cap(sections))


def extract_csv(data: bytes) -> Extracted:
    text = decode_text(data)
    try:
        dialect: Any = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for count, row in enumerate(csv.reader(io.StringIO(text), dialect)):
        if count >= MAX_SHEET_ROWS:
            raise ExtractionError(f"The CSV file has more than {MAX_SHEET_ROWS:,} rows.", "too_large")
        rows.append(row)
    return Extracted(_cap(_rows_to_sections(rows, None)))


# ---- plain text and Markdown ------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def extract_text(data: bytes, markdown: bool) -> Extracted:
    text = decode_text(data)
    if not markdown:
        return Extracted(_cap([Section(clean(text))]))
    sections: list[Section] = []
    path: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        body = clean("\n".join(buffer))
        if body:
            sections.append(Section(body, list(path)))
        buffer.clear()

    for line in text.splitlines():
        match = _MD_HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            path[:] = [*path[: level - 1], match.group(2).strip()]
        else:
            buffer.append(line)
    flush()
    return Extracted(_cap(sections))


# ---- HTML (web pages) -------------------------------------------------------------

_SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer", "form", "iframe", "head", "title"}
_BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "li",
    "ul",
    "ol",
    "tr",
    "table",
    "br",
    "dd",
    "dt",
    "blockquote",
    "pre",
    "address",
    "aside",
    "header",
}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self.path: list[str] = []
        self.buffer: list[str] = []
        self.heading: list[str] | None = None
        self.heading_level = 0
        self.skip_depth = 0
        self.title: str | None = None
        self.in_title = False

    def flush(self) -> None:
        text = clean("".join(self.buffer))
        if text:
            self.sections.append(Section(text, list(self.path)))
        self.buffer.clear()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in _SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.heading, self.heading_level = [], int(tag[1])
        elif tag in _BLOCK or tag in ("td", "th"):
            (self.heading if self.heading is not None else self.buffer).append(
                "\n" if tag in _BLOCK else " | "
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in _SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag) and self.heading is not None:
            text = clean(" ".join("".join(self.heading).split()))
            if text:
                self.path[:] = [*self.path[: self.heading_level - 1], text]
            self.heading = None
        elif tag in _BLOCK:
            self.buffer.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title and not self.title:
            self.title = data.strip()[:200] or None
        if self.skip_depth:
            return
        (self.heading if self.heading is not None else self.buffer).append(data)


def extract_html(data: bytes, charset: str | None = None) -> Extracted:
    try:
        text = data.decode(charset or "utf-8", "replace")
    except LookupError:
        text = data.decode("utf-8", "replace")
    parser = _HTMLText()
    parser.feed(text)
    parser.close()
    parser.flush()
    return Extracted(_cap(parser.sections), parser.title)


def extract(file_type: str, data: bytes, charset: str | None = None) -> Extracted:
    match FileType(file_type):
        case FileType.PDF:
            return extract_pdf(data)
        case FileType.DOCX:
            return extract_docx(data)
        case FileType.XLSX:
            return extract_xlsx(data)
        case FileType.CSV:
            return extract_csv(data)
        case FileType.TXT:
            return extract_text(data, markdown=False)
        case FileType.MARKDOWN:
            return extract_text(data, markdown=True)
        case FileType.HTML:
            return extract_html(data, charset)
