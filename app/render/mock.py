from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings


def render_mock(path: Path, settings: Settings) -> str:
    # Motivation vs Logic: the mock UI is now a real frontend shell that the
    # user opens directly, so the backend serves a template plus runtime config
    # instead of generating a one-off HTML snapshot for each query response.
    html = path.read_text(encoding="utf-8")
    config = json.dumps(
        {
            "apiPrefix": settings.api_prefix,
            "serviceName": settings.server_name,
            "serviceVersion": settings.server_version,
            "simulationEnabled": settings.enable_mock_ui_simulation,
            "queryEndpoint": f"{settings.api_prefix}/query",
            "systemSpecEndpoint": f"{settings.api_prefix}/system/spec",
            "toolsEndpoint": f"{settings.api_prefix}/tools",
            "uiEndpoint": f"{settings.api_prefix}/ui",
            "logoUrl": settings.resolved_server_logo_url,
            # Root Cause vs Logic: callers now navigate via `/ui`; we keep
            # `mockUiEndpoint` in config for client backward compatibility.
            "mockUiEndpoint": f"{settings.api_prefix}/ui",
            "assetBaseUrl": f"{settings.api_prefix}/ui/assets",
            "publicBaseUrl": f"{settings.api_prefix}/ui/public",
        }
    )
    replacements = {
        "__HTH_MOCK_UI_LOGO_HREF__": settings.resolved_server_logo_url,
        "__HTH_MOCK_UI_STYLE_HREF__": f"{settings.api_prefix}/ui/assets/styles.css",
        "__HTH_MOCK_UI_SCRIPT_SRC__": f"{settings.api_prefix}/ui/assets/app.js",
        "__HTH_MOCK_UI_CONFIG__": config,
    }
    for marker, replacement in replacements.items():
        html = html.replace(marker, replacement)
    return html
