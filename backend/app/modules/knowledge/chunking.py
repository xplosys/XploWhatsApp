"""Split extracted sections into overlapping, heading-aware chunks.

Token counts here are estimates (about four characters per token); providers report
the real counts, which are what usage and cost use. Each chunk keeps its heading path
so a passage like "₹800" still says which service it belongs to.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.modules.knowledge.extract import Section

CHARS_PER_TOKEN = 4
_SENTENCE = re.compile(r"(?<=[.!?।])\s+")


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass
class Chunk:
    content: str
    heading: str | None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.content)


def _pieces(text: str, max_chars: int) -> list[str]:
    """Paragraphs, with paragraphs that are too long split by sentence, then by length."""
    out: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            out.append(paragraph)
            continue
        for sentence in _SENTENCE.split(paragraph):
            while len(sentence) > max_chars:
                cut = sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                out.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                out.append(sentence)
    return out


def _merge_meta(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Where a chunk came from: the pages it spans, or the sheet and rows."""
    merged = {k: v for k, v in a.items() if k != "pages"}
    pages = set(a.get("pages", []))
    if "page" in b:
        pages.add(b["page"])
    if pages:
        merged["pages"] = sorted(pages)
    for key in ("sheet", "rows"):
        if b.get(key) and key not in merged:
            merged[key] = b[key]
    return merged


def _common(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    prefix = paths[0] if paths else ()
    for path in paths[1:]:
        n = 0
        while n < min(len(prefix), len(path)) and prefix[n] == path[n]:
            n += 1
        prefix = prefix[:n]
    return prefix


def _render(pieces: list[tuple[tuple[str, ...], str]]) -> tuple[str, str | None]:
    """Body text and heading of a chunk. The heading is what all its pieces share; a
    piece under a deeper heading (small sections merged together) is labelled inline."""
    prefix = _common([path for path, _ in pieces])
    parts: list[str] = []
    previous: tuple[str, ...] | None = None
    for path, text in pieces:
        if path != prefix and path != previous:
            parts.append(f"{' > '.join(path)}\n{text}")
        else:
            parts.append(text)
        previous = path
    heading = " > ".join(prefix) or None
    body = "\n\n".join(parts).strip()
    return (f"{heading}\n{body}" if heading and body else body), heading


def chunk_sections(sections: list[Section], *, chunk_tokens: int, overlap_tokens: int) -> list[Chunk]:
    max_chars = chunk_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    current: list[tuple[tuple[str, ...], str]] = []
    size = 0
    path: tuple[str, ...] | None = None
    meta: dict[str, Any] = {}

    def emit(carry: bool) -> None:
        nonlocal current, size, meta
        content, heading = _render(current)
        if content and not (chunks and chunks[-1].content.endswith(content)):
            chunks.append(Chunk(content, heading, meta))
        # Carry the tail over so a fact split across chunks is still found whole.
        tail: list[tuple[tuple[str, ...], str]] = []
        carried = 0
        for piece in reversed(current if carry else []):
            if carried + len(piece[1]) > overlap_chars:
                break
            tail.insert(0, piece)
            carried += len(piece[1])
        current, size = tail, carried
        meta = {}

    for section in sections:
        section_path = tuple(section.headings)
        if section_path != path and current and size >= max_chars // 4:
            # A new heading starts a new chunk unless the current one is tiny.
            emit(carry=False)
        path = section_path
        meta = _merge_meta(meta, section.meta)
        for piece in _pieces(section.text, max_chars):
            if current and size + len(piece) > max_chars:
                emit(carry=True)
                meta = _merge_meta(meta, section.meta)
            current.append((section_path, piece))
            size += len(piece) + 2
    if current:
        emit(carry=False)
    return chunks
