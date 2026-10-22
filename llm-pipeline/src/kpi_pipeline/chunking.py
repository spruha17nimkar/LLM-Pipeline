"""Split a parsed filing into overlapping, offset-tracked chunks.

Chunks are built from whole lines; consecutive table rows are kept together so a
financial statement isn't split mid-table when it fits in one chunk. Each chunk
records its character span, so ``doc.text[c.start:c.end] == c.text`` always holds
and citations can point to exact offsets.
"""

from __future__ import annotations

from .models import Chunk
from .parsing import ParsedDocument


def _blocks(text: str) -> list[tuple[int, int]]:
    """Group lines into blocks: runs of table rows, or single prose lines."""
    blocks: list[tuple[int, int]] = []
    pos = 0
    cur_start, cur_end, in_table = None, None, False
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        is_table = "|" in line
        if cur_start is not None and is_table and in_table:
            cur_end = end
        else:
            if cur_start is not None:
                blocks.append((cur_start, cur_end))
            cur_start, cur_end, in_table = start, end, is_table
        pos = end + 1
    if cur_start is not None:
        blocks.append((cur_start, cur_end))
    return blocks


def chunk_document(doc: ParsedDocument, max_chars: int = 3000, overlap_blocks: int = 1) -> list[Chunk]:
    text = doc.text
    blocks = _blocks(text)

    # Split any oversized block (a huge table) on line boundaries.
    sized: list[tuple[int, int]] = []
    for s, e in blocks:
        while e - s > max_chars:
            cut = text.rfind("\n", s, s + max_chars)
            if cut <= s:
                cut = s + max_chars
            sized.append((s, cut))
            s = cut + 1 if text[cut:cut + 1] == "\n" else cut
        sized.append((s, e))

    section_starts = sorted(s.start for s in doc.sections)

    def crosses_section(a: int, b: int) -> bool:
        # True if a new section begins in (a, b] — chunks never span two sections.
        return any(a < s <= b for s in section_starts)

    chunks: list[Chunk] = []
    i = 0
    while i < len(sized):
        j = i
        start = sized[i][0]
        end = sized[i][1]
        while (
            j + 1 < len(sized)
            and sized[j + 1][1] - start <= max_chars
            and not crosses_section(start, sized[j + 1][0])
        ):
            j += 1
            end = sized[j][1]
        chunks.append(
            Chunk(
                id=f"c{len(chunks):04d}",
                text=text[start:end],
                start=start,
                end=end,
                section=doc.section_at(start),
            )
        )
        if j + 1 >= len(sized):
            break
        if crosses_section(sized[j][0], sized[j + 1][0]):
            i = j + 1  # no overlap across a section boundary
        else:
            i = max(j + 1 - overlap_blocks, i + 1)
    return chunks
