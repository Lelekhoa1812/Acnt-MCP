from __future__ import annotations

import re


# Motivation vs Logic: the chunker isolates table/bullet detection so the summarizer can focus on
# selecting the most relevant blocks without duplicating low-level parsing heuristics.


def looks_like_table(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    if not looks_like_table_row(lines[0]) or not looks_like_table_separator(lines[1]):
        return False
    return all(looks_like_table_row(line) or looks_like_table_separator(line) for line in lines)


def chunk_table_block(block: str, rows_per_chunk: int) -> list[str]:
    lines = [line.rstrip() for line in block.splitlines() if line.strip()]
    if len(lines) < 2:
        return [block]

    header = lines[0]
    separator = lines[1]
    rows = [line for line in lines[2:] if looks_like_table_row(line)]
    if not rows:
        return [block]

    chunk_size = max(1, rows_per_chunk)
    total_chunks = max(1, (len(rows) + chunk_size - 1) // chunk_size)
    chunks: list[str] = []
    for chunk_index, start in enumerate(range(0, len(rows), chunk_size), start=1):
        chunk_rows = rows[start : start + chunk_size]
        rendered = [f"table chunk {chunk_index}/{total_chunks}:", header, separator, *chunk_rows]
        chunks.append("\n".join(rendered))
    return chunks


def looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def looks_like_table_separator(line: str) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return False
    cells = [cell.strip().replace(" ", "") for cell in stripped.strip("|").split("|")]
    return bool(cells) and all(cell and set(cell) <= {"-", ":"} for cell in cells)


def looks_like_bullets(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    return all(
        line.startswith(("- ", "* ", "• ")) or re.match(r"^\d+[.)]\s+", line) is not None
        for line in lines
    )
