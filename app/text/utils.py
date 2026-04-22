from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.prompt.registry import STOPWORDS


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def significant_tokens(value: str | None) -> list[str]:
    normalized = normalize_text(value)
    return [token for token in normalized.split() if token and token not in STOPWORDS]


def lexical_overlap(query: str, candidate: str) -> float:
    query_tokens = set(significant_tokens(query))
    candidate_tokens = set(significant_tokens(candidate))
    if not query_tokens or not candidate_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)


def fuzzy_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()
