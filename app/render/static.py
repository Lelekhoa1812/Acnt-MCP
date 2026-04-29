from __future__ import annotations

from pathlib import Path


def render_static_html(path: Path) -> str:
    # Motivation vs Logic: several browser-facing pages in this service are
    # delivered as authored HTML files, so a tiny shared loader keeps the
    # rendering path consistent without reimplementing file I/O in each route.
    return path.read_text(encoding="utf-8")
