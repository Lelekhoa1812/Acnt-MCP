from __future__ import annotations

from urllib.parse import urlparse

from app.schemas import ProductVariantDto


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


def resolve_variant_harmonise_image(
    base_url: str | None,
    variant: ProductVariantDto,
) -> tuple[str | None, str | None, str]:
    # Motivation vs Logic: Harmonise may expose thumbnails on the variant
    # (`imageThumbnailUri`), under `details` as alternate keys, or only as
    # `details.imageFileName`; unify resolution so `stock_image` matches cloud.
    raw: str | None = None
    source = "none"

    v_uri = (variant.harmonise_image_uri or "").strip() if variant.harmonise_image_uri else None
    if v_uri:
        raw = v_uri
        source = "variant_harmonise_uri"
    elif variant.details:
        d_uri = (
            (variant.details.harmonise_image_uri or "").strip()
            if variant.details.harmonise_image_uri
            else None
        )
        if d_uri:
            raw = d_uri
            source = "details_harmonise_uri"
        elif variant.details.imageFileName:
            raw = variant.details.imageFileName.strip() or None
            source = "details_imageFileName"

    if not raw:
        return None, None, "none"

    url = build_harmonise_image_url(base_url, raw)
    return url, raw, source
