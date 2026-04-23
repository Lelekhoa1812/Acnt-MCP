from __future__ import annotations

from urllib.parse import urlparse


def build_harmonise_image_url(base_url: str | None, image_file_name: str | None) -> str | None:
    if not image_file_name:
        return None

    candidate = image_file_name.strip()
    if not candidate:
        return None

    # Root Cause vs Logic: imageFileName sometimes already contains an absolute
    # URL, so blindly prefixing CLOUD_HARMONISE_IMAGE produced broken URLs.
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        return candidate

    if not base_url:
        return None

    normalized_base = base_url.rstrip("/")
    normalized_path = candidate if candidate.startswith("/") else f"/{candidate}"
    return f"{normalized_base}{normalized_path}"
