from __future__ import annotations


def trailing_label_after_separator(name: str | None) -> str | None:
    """Return the segment after a ' - ' style separator (e.g. colour from variant title)."""
    if not name or not (name := (name or "").strip()):
        return None
    for sep in (" - ", " – ", " — ", "—", "–"):
        if sep in name:
            tail = name.split(sep, 1)[-1].strip()
            if tail and tail.casefold() != name.casefold():
                return tail
    return None
