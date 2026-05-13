from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import get_container


router = APIRouter()


@router.get("/health")
async def health(container=Depends(get_container)) -> dict[str, object]:
    kvs = container.key_value_store
    return {
        "status": "ok",
        "service": container.settings.server_name,
        "version": container.settings.server_version,
        "session_cache_backend": kvs.persistence_backend,
    }
