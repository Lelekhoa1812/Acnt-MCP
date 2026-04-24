from __future__ import annotations

import re
from typing import Any

from app.agent.chunker import chunk_table_block, looks_like_bullets, looks_like_table
from app.schemas import ConversationTurn
from app.text.utils import lexical_overlap, normalize_text, significant_tokens


# Motivation vs Logic: the SummarizerAgent centralizes sentence/block selection and defers raw table/bullet
# chunking to the chunker, allowing downstream prompts to see only the most relevant excerpts without
# blowing the context window.
class SummarizerAgent:
    def summarize_history(
        self,
        turns: list[ConversationTurn],
        request: str,
        settings: dict[str, int],
    ) -> list[dict[str, Any]]:
        history_turns = settings.get("history_turns", 4)
        block_limit = settings.get("history_block_chars", 1400)
        table_chunk_size = settings.get("rows_per_table_chunk", 6)
        if not turns:
            return []

        selected_turns = self._select_turns(turns, request, history_turns)
        summaries: list[dict[str, Any]] = []
        for turn in selected_turns:
            blocks = self._split_text_blocks(turn.content, table_chunk_size)
            selected_blocks = self._select_blocks(blocks, request, block_limit)
            if not selected_blocks:
                continue
            summaries.append(
                {
                    "role": turn.role,
                    "blocks": selected_blocks,
                    "truncated": len(selected_blocks) < len(blocks),
                }
            )
        return summaries

    def _split_text_blocks(self, text: str, table_chunk_size: int) -> list[str]:
        compact = text.strip()
        if not compact:
            return []

        raw_blocks = [block.strip() for block in re.split(r"\n\s*\n", compact) if block.strip()]
        blocks: list[str] = []
        for block in raw_blocks:
            if looks_like_table(block):
                blocks.extend(chunk_table_block(block, table_chunk_size))
                continue
            if looks_like_bullets(block):
                blocks.append(self._summarize_bullets(block))
                continue
            if len(block) > 2400:
                blocks.append(self._summarize_paragraph(block))
                continue
            blocks.append(block)
        return blocks

    def _select_turns(
        self, turns: list[ConversationTurn], request: str, limit: int
    ) -> list[ConversationTurn]:
        if not turns:
            return []
        request_tokens = significant_tokens(request)
        scored: list[tuple[float, int, ConversationTurn]] = []
        total = len(turns)
        for index, turn in enumerate(turns):
            overlap = lexical_overlap(request, turn.content) if request_tokens else 0.0
            recency = (index + 1) / max(total, 1)
            score = overlap * 2.0 + recency * 0.2
            scored.append((score, index, turn))
        top = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[: limit]
        selected_indices = sorted(index for _, index, _ in top)
        return [turns[index] for index in selected_indices]

    def _select_blocks(self, blocks: list[str], request: str, block_limit: int) -> list[str]:
        if not blocks:
            return []
        request_tokens = significant_tokens(request)
        scored: list[tuple[float, int, str]] = []
        total = len(blocks)
        for index, block in enumerate(blocks):
            overlap = lexical_overlap(request, block) if request_tokens else 0.0
            recency = (index + 1) / max(total, 1)
            score = overlap * 2.0 + recency * 0.1
            scored.append((score, index, block))

        max_blocks = 2 if block_limit < 1200 else 3
        top = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[:max_blocks]
        selected_indices = sorted(index for _, index, _ in top)
        selected = []
        for block in (blocks[index] for index in selected_indices):
            if len(block) > block_limit:
                selected.append(self._truncate_block(block, block_limit))
                continue
            selected.append(block)
        return selected

    def _summarize_bullets(self, block: str) -> str:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) <= 5:
            return "\n".join(lines)
        selected = lines[:3] + [f"... {len(lines) - 3} more lines"]
        return "\n".join(selected)

    def _summarize_paragraph(self, block: str) -> str:
        paragraphs = [part.strip() for part in re.split(r"(?<=[.!?])\s+", block) if part.strip()]
        if not paragraphs:
            return self._truncate_block(block, 900)
        tokens = significant_tokens(block)
        if tokens:
            matching = [
                sentence
                for sentence in paragraphs
                if any(token in normalize_text(sentence) for token in tokens)
            ]
            if matching:
                joined = " ".join(matching[:4])
                return self._truncate_block(joined, 900)
        joined = " ".join(paragraphs[:2])
        return self._truncate_block(joined, 900)

    def _truncate_block(self, block: str, limit: int) -> str:
        if len(block) <= limit:
            return block
        return block[: max(0, limit - 1)].rstrip() + "…"


_DEFAULT_SUMMARIZER = SummarizerAgent()


def summarize_history(
    turns: list[ConversationTurn],
    request: str,
    settings: dict[str, int],
) -> list[dict[str, Any]]:
    return _DEFAULT_SUMMARIZER.summarize_history(turns, request, settings)
