from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.config import Settings
from app.errors import InventoryNotFoundError
from app.schemas import (
    ProductListItemDto,
    ProductListItemDtoPagedResponse,
    StockApiDepartmentDto,
    StockApiSubDepartmentDto,
    StockCategoryDto,
    StockCategoryDtoPagedResponse,
)
from app.text.utils import fuzzy_ratio, lexical_overlap, normalize_text, significant_tokens


class MockService:
    # Motivation vs Logic: the MCP application now always talks to Harmonise,
    # so this simulator reproduces the Harmonise contract locally while keeping
    # the mock JSON recovery logic isolated behind the same HTTP boundary.
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._catalogue_payload: dict[str, Any] | None = None
        self._details_payload: dict[str, Any] | None = None
        self._departments_payload: list[dict[str, Any]] | None = None
        self._categories_payload: dict[str, Any] | None = None

    def get_departments(self, include_inactive: bool, include_sub_departments: bool) -> list[dict[str, Any]]:
        raw_departments = self._load_departments()
        if raw_departments is None:
            raw_departments = self._derive_departments()
        departments = [StockApiDepartmentDto.model_validate(item) for item in raw_departments]
        if not include_inactive:
            departments = [department for department in departments if department.isActive]
        if not include_sub_departments:
            departments = [department.model_copy(update={"subDepartments": []}) for department in departments]
        return [department.model_dump(mode="json") for department in departments]

    def get_categories(self, page: int, page_size: int) -> dict[str, Any]:
        raw_categories = self._load_categories()
        if raw_categories is None:
            raw_categories = self._derive_categories()
        categories = [StockCategoryDto.model_validate(item) for item in raw_categories]
        paged = self._paginate(categories, page, page_size)
        response = StockCategoryDtoPagedResponse(
            items=list(paged),
            page=page,
            pageSize=page_size,
            totalCount=len(categories),
            totalPages=max(1, (len(categories) + page_size - 1) // page_size),
        )
        return response.model_dump(mode="json")

    def search_catalogue(
        self,
        page: int,
        page_size: int,
        search: str | None,
        department_id: int | None,
        category_id: str | None,
    ) -> dict[str, Any]:
        products = self._catalogue_items()
        filtered: list[ProductListItemDto] = []
        for product in products:
            if department_id is not None and product.departmentId != department_id:
                continue
            if category_id is not None and product.categoryId != category_id:
                continue
            filtered.append(product)

        if search:
            ranked = [(self._catalogue_search_score(search, product), product) for product in filtered]
            filtered = [product for score, product in ranked if score >= 0.2]
            filtered.sort(key=lambda product: self._catalogue_search_score(search, product), reverse=True)

        paged = self._paginate(filtered, page, page_size)
        response = ProductListItemDtoPagedResponse(
            items=list(paged),
            page=page,
            pageSize=page_size,
            totalCount=len(filtered),
            totalPages=max(1, (len(filtered) + page_size - 1) // page_size),
        )
        return response.model_dump(mode="json")

    def get_product(
        self,
        product_id: str | None,
        sku: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        matched: list[ProductListItemDto] = []
        for item in self._details_items():
            product_id_matches = product_id is not None and item.id == product_id
            sku_matches = sku is not None and any(variant.sku == sku for variant in item.variants)
            if product_id_matches or sku_matches:
                matched.append(item)

        if not matched:
            raise InventoryNotFoundError("No exact product record matched the supplied id or sku.")

        paged = self._paginate(matched, page, page_size)
        response = ProductListItemDtoPagedResponse(
            items=list(paged),
            page=page,
            pageSize=page_size,
            totalCount=len(matched),
            totalPages=max(1, (len(matched) + page_size - 1) // page_size),
        )
        return response.model_dump(mode="json")

    def _catalogue_items(self) -> list[ProductListItemDto]:
        if self._catalogue_payload is None:
            self._catalogue_payload = self._load_required_json(self.settings.resolve_path(self.settings.mock_catalog_path))
        return [ProductListItemDto.model_validate(item) for item in self._catalogue_payload.get("items", [])]

    def _details_items(self) -> list[ProductListItemDto]:
        if self._details_payload is None:
            self._details_payload = self._load_required_json(self.settings.resolve_path(self.settings.mock_details_path))
        return [ProductListItemDto.model_validate(item) for item in self._details_payload.get("items", [])]

    def _load_departments(self) -> list[dict[str, Any]] | None:
        if self._departments_payload is not None:
            return self._departments_payload
        path = self.settings.resolve_path(self.settings.mock_departments_path)
        if not path.exists():
            self.logger.debug("Local Harmonise simulator deriving departments because %s is missing.", path)
            return None
        loaded = self._load_optional_json(path)
        if loaded is None:
            return None
        self._departments_payload = loaded if isinstance(loaded, list) else loaded.get("items", [])
        return self._departments_payload

    def _load_categories(self) -> list[dict[str, Any]] | None:
        if self._categories_payload is not None:
            return self._categories_payload.get("items", [])
        path = self.settings.resolve_path(self.settings.mock_categories_path)
        if not path.exists():
            self.logger.debug("Local Harmonise simulator deriving categories because %s is missing.", path)
            return None
        loaded = self._load_optional_json(path)
        if loaded is None:
            return None
        self._categories_payload = {"items": loaded} if isinstance(loaded, list) else loaded
        return self._categories_payload.get("items", [])

    def _derive_departments(self) -> list[dict[str, Any]]:
        grouped_sub_departments: dict[int, set[int]] = defaultdict(set)
        for item in self._catalogue_items():
            if item.subDepartmentId is not None:
                grouped_sub_departments[item.departmentId].add(item.subDepartmentId)

        departments: list[StockApiDepartmentDto] = []
        for index, department_id in enumerate(sorted(grouped_sub_departments.keys()), start=1):
            sub_departments = [
                StockApiSubDepartmentDto(
                    id=sub_department_id,
                    name=f"Sub-department {sub_department_id}",
                    isActive=True,
                    sortOrder=offset,
                )
                for offset, sub_department_id in enumerate(sorted(grouped_sub_departments[department_id]), start=1)
            ]
            departments.append(
                StockApiDepartmentDto(
                    id=department_id,
                    name=f"Department {department_id}",
                    isActive=True,
                    sortOrder=index,
                    subDepartments=sub_departments,
                )
            )
        return [department.model_dump(mode="json") for department in departments]

    def _derive_categories(self) -> list[dict[str, Any]]:
        grouped_products: dict[str, list[ProductListItemDto]] = defaultdict(list)
        for item in self._catalogue_items():
            grouped_products[item.categoryId].append(item)

        categories: list[StockCategoryDto] = []
        for index, (category_id, products) in enumerate(sorted(grouped_products.items()), start=1):
            categories.append(
                StockCategoryDto(
                    id=category_id,
                    name=self._derive_category_label(products),
                    departmentId=products[0].departmentId,
                    parentStockCategoryId=None,
                    categoryType="derived",
                    sortOrder=index,
                )
            )
        return [category.model_dump(mode="json") for category in categories]

    def _derive_category_label(self, products: list[ProductListItemDto]) -> str:
        token_counter: Counter[str] = Counter()
        prefix_counter: Counter[str] = Counter()
        for product in products:
            name = product.name or ""
            prefix = normalize_text(name.split("-")[0]).strip()
            if prefix:
                prefix_counter[prefix] += 1
            token_counter.update(significant_tokens(name))
        if prefix_counter:
            label, count = prefix_counter.most_common(1)[0]
            if count > 1 or len(products) == 1:
                return label.title()
        shared_tokens = [token for token, count in token_counter.most_common(2) if count > 1]
        if shared_tokens:
            return " ".join(shared_tokens).title()
        fallback_name = products[0].name or products[0].categoryId
        trimmed = " ".join(significant_tokens(fallback_name)[:2]) or f"Category {products[0].categoryId[-4:]}"
        return trimmed.title()

    def _catalogue_search_score(self, search: str, product: ProductListItemDto) -> float:
        candidate_parts = [product.name or ""]
        candidate_parts.extend(variant.name or "" for variant in product.variants)
        candidate_parts.extend(variant.sku or "" for variant in product.variants)
        for variation in product.variations:
            candidate_parts.append(variation.name or "")
            candidate_parts.extend(option.name or "" for option in variation.options)
        combined = " ".join(candidate_parts)
        substring_bonus = 0.35 if normalize_text(search) in normalize_text(combined) else 0
        overlap = lexical_overlap(search, combined)
        fuzzy = max(
            fuzzy_ratio(search, product.name or ""),
            *(fuzzy_ratio(search, variant.name or "") for variant in product.variants),
            0.0,
        )
        return min(0.99, substring_bonus + (overlap * 0.45) + (fuzzy * 0.35))

    def _paginate(self, items: list[Any], page: int, page_size: int) -> list[Any]:
        start = (page - 1) * page_size
        end = start + page_size
        return items[start:end]

    def _load_optional_json(self, path: Path) -> dict[str, Any] | list[dict[str, Any]] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_required_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Required mock data file not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
