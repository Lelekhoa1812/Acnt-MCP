from __future__ import annotations

import logging

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.config import InventoryNotFoundError, Settings, get_settings
from harmonise.source import MockService


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    logger = logging.getLogger("hth.harmonise")

    app = FastAPI(
        title="harmonise-local",
        version=resolved_settings.server_version,
    )
    app.state.service = MockService(settings=resolved_settings, logger=logger)

    @app.exception_handler(InventoryNotFoundError)
    async def handle_not_found(_: Request, exc: InventoryNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/v1/common/departments")
    async def get_departments(
        includeInactive: bool = Query(False),
        includeSubDepartments: bool = Query(False),
    ) -> list[dict[str, object]]:
        return app.state.service.get_departments(includeInactive, includeSubDepartments)

    @app.get("/api/v1/stock/categories")
    async def get_categories(
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1, le=100),
    ) -> dict[str, object]:
        return app.state.service.get_categories(page, pageSize)

    @app.get("/api/v1/stock/product-catalogue")
    async def search_catalogue(
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1, le=100),
        search: str | None = Query(None),
        departmentId: int | None = Query(None),
        categoryId: str | None = Query(None),
    ) -> dict[str, object]:
        return app.state.service.search_catalogue(page, pageSize, search, departmentId, categoryId)

    @app.get("/api/v1/products")
    async def list_products(
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1, le=100),
        search: str | None = Query(None),
        departmentId: int | None = Query(None),
        categoryId: str | None = Query(None),
    ) -> dict[str, object]:
        # Motivation vs Logic: cloud Harmonise discovery now uses `/api/v1/products`
        # as the primary catalogue contract, while legacy stock routes are kept as
        # compatibility aliases for existing local tooling and tests.
        return app.state.service.search_catalogue(page, pageSize, search, departmentId, categoryId)

    @app.get("/api/v1/stock/products")
    async def get_products(
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1, le=100),
        id: str | None = Query(None),
        sku: str | None = Query(None),
    ) -> dict[str, object]:
        return app.state.service.get_product(id, sku, page, pageSize)

    @app.get("/api/v1/products/{skuCode}")
    async def get_product_by_sku(
        skuCode: str,
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1, le=100),
    ) -> dict[str, object]:
        return app.state.service.get_product_by_sku_code(skuCode, page, pageSize)

    return app


app = create_app()
