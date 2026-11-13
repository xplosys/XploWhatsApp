"""Chunking, file type detection and web fetch checks, without services."""

from __future__ import annotations

import pytest

from app.modules.knowledge.chunking import chunk_sections, estimate_tokens
from app.modules.knowledge.extract import (
    ExtractionError,
    Section,
    clean,
    extract_csv,
    extract_html,
    extract_text,
)
from app.modules.knowledge.filetypes import UnsupportedFile, decode_text, detect
from app.modules.knowledge.models import FileType
from app.modules.knowledge.webfetch import FetchError, _parse_content_type, is_public, validate_url
from tests.support.files import make_docx, make_macro_xlsx, make_pdf, make_xlsx, make_zip_bomb_docx


def test_small_sections_merge_and_keep_their_headings() -> None:
    sections = [
        Section("Scaling costs ₹800.", ["Treatments", "Cleaning"]),
        Section("Root canal from ₹4,500.", ["Treatments", "Root canal"]),
    ]
    [chunk] = chunk_sections(sections, chunk_tokens=600, overlap_tokens=80)
    assert chunk.heading == "Treatments"
    assert chunk.content == (
        "Treatments\nTreatments > Cleaning\nScaling costs ₹800.\n\n"
        "Treatments > Root canal\nRoot canal from ₹4,500."
    )


def test_long_text_is_split_with_overlap() -> None:
    paragraphs = [f"Paragraph {i} " + "word " * 60 for i in range(30)]
    chunks = chunk_sections(
        [Section("\n\n".join(paragraphs), ["Guide"])], chunk_tokens=200, overlap_tokens=80
    )
    assert len(chunks) > 3
    assert all(c.heading == "Guide" and c.content.startswith("Guide\n") for c in chunks)
    assert all(c.token_estimate <= 200 + 100 for c in chunks)
    # The last paragraph of one chunk starts the next.
    first_tail = chunks[0].content.split("\n\n")[-1]
    assert chunks[1].content.split("\n", 1)[1].startswith(first_tail)


def test_big_section_starts_a_new_chunk_at_a_heading() -> None:
    sections = [Section("a " * 700, ["One"]), Section("Short.", ["Two"])]
    chunks = chunk_sections(sections, chunk_tokens=600, overlap_tokens=80)
    assert [c.heading for c in chunks] == ["One", "Two"]
    assert chunks[1].content == "Two\nShort."


def test_pages_and_rows_are_kept() -> None:
    sections = [Section("First page.", [], {"page": 1}), Section("Second page.", [], {"page": 2})]
    [chunk] = chunk_sections(sections, chunk_tokens=600, overlap_tokens=80)
    assert chunk.meta == {"pages": [1, 2]} and chunk.heading is None
    assert estimate_tokens("abcd" * 10) == 10


def test_queries_use_the_latest_message_and_recent_context() -> None:
    from app.modules.ai.providers.base import Turn
    from app.modules.knowledge.retrieval import queries_from_turns

    assert queries_from_turns([]) == []
    assert queries_from_turns([Turn("user", " Price? ")]) == ["Price?"]
    turns = [
        Turn("user", "a"),
        Turn("assistant", "x"),
        Turn("user", "b"),
        Turn("user", "c"),
        Turn("user", "d"),
    ]
    assert queries_from_turns(turns) == ["d", "b\nc\nd"]


def test_file_types_are_detected_by_content_and_name() -> None:
    assert detect("a.pdf", make_pdf(["hi"])) == FileType.PDF
    assert detect("a.DOCX", make_docx()) == FileType.DOCX
    assert detect("a.xlsx", make_xlsx()) == FileType.XLSX
    assert detect("a.csv", b"a,b\n1,2\n") == FileType.CSV
    assert detect("a.md", b"# Hi") == FileType.MARKDOWN
    assert detect("a.txt", "₹800".encode("utf-16")) == FileType.TXT
    for name, data in (
        ("a.docx", make_pdf(["hi"])),
        ("a.xlsx", make_macro_xlsx()),
        ("a.docx", make_zip_bomb_docx()),
        ("a.txt", b"MZ\x90\x00binary\x00\x00"),
        ("a.exe", b"hello"),
        ("a.pdf", b"hello"),
        ("noext", b"hello"),
    ):
        with pytest.raises(UnsupportedFile):
            detect(name, data)


def test_text_decoding() -> None:
    assert decode_text("नमस्ते".encode()) == "नमस्ते"
    assert decode_text(b"\xef\xbb\xbfhello") == "hello"
    with pytest.raises(UnsupportedFile):  # legacy code pages are refused, not guessed
        decode_text("hé".encode("cp1252"))


def test_extract_text_csv_and_html() -> None:
    assert clean("a \t b\r\n\n\n\nc\x00") == "a b\n\nc"
    md = extract_text(b"intro\n# A\none\n## B\ntwo\n# C\nthree", markdown=True)
    assert [(s.headings, s.text) for s in md.sections] == [
        ([], "intro"),
        (["A"], "one"),
        (["A", "B"], "two"),
        (["C"], "three"),
    ]
    rows = extract_csv(b"Name;Price\nCrown;8000\n;\n")
    assert rows.sections[0].text == "Name: Crown; Price: 8000"
    html = extract_html(
        b"<title>T</title><nav>menu</nav><h1>Head</h1><p>Body &amp; more</p><script>x()</script>", "utf-8"
    )
    assert html.title == "T" and [(s.headings, s.text) for s in html.sections] == [(["Head"], "Body & more")]
    with pytest.raises(ExtractionError) as err:
        extract_text(b"   \n\n ", markdown=False)
    assert err.value.code == "no_text"


@pytest.mark.parametrize(
    "address,public",
    [
        ("93.184.215.14", True),
        ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fc00::1", False),
        ("::ffff:10.0.0.1", False),
        ("224.0.0.1", False),
    ],
)
def test_only_public_addresses_are_allowed(address: str, public: bool) -> None:
    assert is_public(address) is public


def test_url_validation() -> None:
    assert validate_url(" https://Example.com/a?b=1 ") == "https://Example.com/a?b=1"
    assert validate_url("http://example.com:80/") == "http://example.com:80/"
    for bad in (
        "ftp://x.com",
        "https://u:p@x.com",
        "https://x.com:22",
        "http:///nohost",
        "file:///etc/passwd",
    ):
        with pytest.raises(FetchError):
            validate_url(bad)


def test_content_type_parsing() -> None:
    assert _parse_content_type('text/html; charset="ISO-8859-1"') == ("text/html", "ISO-8859-1")
    assert _parse_content_type("Application/PDF") == ("application/pdf", None)
