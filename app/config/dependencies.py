from __future__ import annotations

from fastapi import Request

from app.config.container import AppContainer, build_container
from app.config.settings import get_settings


async def get_container(request: Request) -> AppContainer:
    # On serverless platforms, lifespan may not have run yet
    # Initialize container on-demand if not already in state
    if not hasattr(request.app.state, "container"):
        settings = get_settings()
        request.app.state.container = await build_container(settings)
    return request.app.state.container
