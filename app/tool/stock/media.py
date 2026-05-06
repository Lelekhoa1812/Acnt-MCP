from __future__ import annotations

import html
import json
from urllib.parse import urlparse, urlunparse

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


def build_harmonise_image_fetch_candidates(image_url: str) -> list[str]:
    # Motivation vs Logic: Harmonise thumbnail URLs can use any extension, while
    # higher-resolution siblings are normalized to png/jpg/jpeg variants. Strip
    # `_thumb` and ignore the thumbnail extension before falling back to the original.
    parsed = urlparse(image_url)
    path = parsed.path
    slash_index = path.rfind("/")
    directory = path[: slash_index + 1] if slash_index >= 0 else ""
    filename = path[slash_index + 1 :] if slash_index >= 0 else path
    dot_index = filename.rfind(".")
    if dot_index <= 0:
        return [image_url]

    stem = filename[:dot_index]
    if not stem.endswith("_thumb"):
        return [image_url]

    image_id = stem[: -len("_thumb")]
    candidate_paths = [
        f"{directory}{image_id}.png",
        f"{directory}{image_id}.jpg",
        f"{directory}{image_id}.jpeg",
        path,
    ]

    candidates: list[str] = []
    seen: set[str] = set()
    for candidate_path in candidate_paths:
        candidate = urlunparse(parsed._replace(path=candidate_path))
        if candidate in seen:
            continue
        candidates.append(candidate)
        seen.add(candidate)
    return candidates


def build_stock_image_rendering_payload(
    image_url: str,
    *,
    default_thumbnail_url: str | None = None,
) -> dict[str, object]:
    # Motivation vs Logic: browser-side AI clients need an ordered rendering
    # contract that starts with encoded MCP content and only falls back to local
    # or scripted rendering after each user-visible preview attempt fails.
    fallback_uri = default_thumbnail_url or image_url
    escaped_attr_url = html.escape(image_url, quote=True)
    js_url = json.dumps(image_url)
    fallback_js_url = json.dumps(fallback_uri)
    markdown_url = image_url.replace(")", "%29")
    fallback_markdown_url = fallback_uri.replace(")", "%29")
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Harmonise Image Preview</title>
  <style>
    :root {{
      --display-width: min(38vw, 380px);
      --max-display-height: 86vh;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at center, #ffffff 0%, #eeeeee 72%);
      font-family: Arial, sans-serif;
    }}

    .frame {{
      width: min(92vw, 900px);
      height: min(92vh, 900px);
      display: grid;
      place-items: center;
      padding: 28px;
      background: #fff;
      border-radius: 28px;
      box-shadow: 0 18px 70px rgba(0, 0, 0, 0.14);
      overflow: hidden;
    }}

    .image-wrap {{
      display: grid;
      place-items: center;
      width: 100%;
      height: 100%;
    }}

    img {{
      width: var(--display-width);
      max-width: 88vw;
      max-height: var(--max-display-height);
      height: auto;
      object-fit: contain;
      filter:
        contrast(2.2)
        brightness(1.18)
        saturate(1.12)
        blur(0.08px)
        drop-shadow(0 12px 26px rgba(0, 0, 0, 0.16));
      image-rendering: -webkit-optimize-contrast;
      image-rendering: pixelated;
      transform: translateZ(0) scale(1.02);
    }}

    .status {{
      color: #444;
      font-size: 16px;
      text-align: center;
      line-height: 1.4;
    }}
  </style>
</head>
<body>
  <div class="frame">
    <div class="image-wrap" id="imageWrap">
      <img id="productImage" src="{escaped_attr_url}" alt="Harmonise product image" />
    </div>
  </div>

  <script>
    const imageUrl = {js_url};
    const fallbackImageUrl = {fallback_js_url};
    const productImage = document.getElementById("productImage");
    const imageWrap = document.getElementById("imageWrap");

    productImage.onerror = () => {{
      imageWrap.innerHTML = `
        <div class="status">
          Failed to load the remote image.<br />
          <a href="${{fallbackImageUrl}}" target="_blank" rel="noopener noreferrer">Open image URL</a>
        </div>
      `;
    }};
  </script>
</body>
</html>"""
    python_snippet = f"""python3 - <<'PY'
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse
from urllib.request import Request, urlopen

image_url = {js_url}
path_suffix = Path(urlparse(image_url).path).suffix or ".png"
request = Request(image_url, headers={{"User-Agent": "hth-mcp-stock-image-preview/1.0"}})
with urlopen(request, timeout=20) as response:
    image_bytes = response.read()

with NamedTemporaryFile(delete=False, suffix=path_suffix) as image_file:
    image_file.write(image_bytes)
    image_path = image_file.name

print(f"![Harmonise product image]({{image_path}})")
PY"""
    return {
        "fallbackOrder": [
            "encoded_mcp_image_content",
            "download_resolved_uri_local_file",
            "activated_environment_script_preview",
            "plain_best_uri",
        ],
        "steps": [
            (
                "Step 1: Use the encoded MCP image content returned outside this JSON response first. "
                "Render that image directly in the UI for user review. If the user cannot see it, continue to Step 2."
            ),
            (
                "Step 2: Download bestResolvedUri locally, then render the downloaded local image file to the user. "
                "If local download or local image rendering fails, continue to Step 3."
            ),
            (
                "Step 3: Run the provided script preview only after activating the script/runtime environment. "
                "Use the repo's documented environment pattern when available, for example `source .venv/bin/activate`, "
                "then run rendering.desktop.pythonSnippet with python3. If the script preview fails, continue to Step 4."
            ),
            (
                "Step 4: Show only uriOnly.bestUriToShow. Clearly explain that due to the AI client's technical "
                "rendering issue, the image cannot be rendered inline right now; do not blame the MCP tool."
            ),
        ],
        "bestResolvedUri": image_url,
        "defaultThumbnailUri": fallback_uri,
        "encoded": {
            "instruction": (
                "First render the encoded MCP image content returned as native image content by the tool. "
                "Do not run scripts before trying this encoded image path."
            ),
        },
        "localFile": {
            "instruction": (
                "If encoded image rendering is not visible, download bestResolvedUri to a local file and render "
                "that local image file in the UI for user review before trying the script preview."
            ),
        },
        "browser": {
            "instruction": (
                "Optional browser preview asset: if the host can render temporary HTML after the local-file attempt, "
                "render this HTML using bestResolvedUri as the img src. If it fails, keep following fallbackOrder."
            ),
            "htmlTemplate": html_template,
        },
        "desktop": {
            "instruction": (
                "Before running this snippet, always activate the script/runtime environment, using the repo's "
                "documented environment when available, for example `source .venv/bin/activate`. Then run the "
                "python3 snippet to download bestResolvedUri to a temporary local file and show the printed "
                "Markdown image path with its absolute file path."
            ),
            "pythonSnippet": python_snippet,
        },
        "uriOnly": {
            "bestUriToShow": image_url,
            "fallbackThumbnailUri": fallback_uri,
            "markdown": f"![Harmonise product image]({fallback_markdown_url})",
            "instruction": (
                "Use only this final link fallback if encoded rendering, local download rendering, and the "
                "activated-environment script preview all fail. Show bestUriToShow; if it is unavailable, show "
                "fallbackThumbnailUri. Explain that the AI client has a technical issue rendering the image inline."
            ),
        },
        "markdown": f"![Harmonise product image]({markdown_url})",
    }
