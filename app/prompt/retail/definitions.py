from __future__ import annotations

RETAIL_PROMPT = {
    "domain": "retail",
    "intent_classes": ["retail"],
    "tools": ["openlibrary_book_search", "openlibrary_isbn_lookup", "openlibrary_subject_list"],
    "examples": """
Retail Example:
User: Find books by Octavia Butler about climate fiction.
Assistant: Use `openlibrary_book_search` with author/title/subject filters, then summarize the strongest matches and edition metadata.
""".strip(),
    "rules": [
        "Use Open Library for public book and subject catalog discovery.",
        "Prefer ISBN lookup when the user provides an ISBN; otherwise search by title, author, or subject.",
        "Use subject browsing to explore catalog categories or classifications.",
    ],
}
