from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import Settings
from app.errors import InventoryNotFoundError, ParameterMappingError
from app.inventory.source import HarmoniseInventorySource
from app.schemas import (
    NormalizedEvidence,
    PricingSnapshot,
    DimensionsSnapshot,
    LifecycleSnapshot,
    MediaSnapshot,
    ProductComponentAllocationDto,
    ProductListItemDto,
    ProductListItemDtoPagedResponse,
    ProductVariantDto,
    ProvenanceSnapshot,
    StockApiDepartmentDto,
    StockCategoryDtoPagedResponse,
    StockExtractVariantEvidenceArgs,
    StockGetCategoriesArgs,
    StockGetDepartmentsArgs,
    StockGetProductArgs,
    StockSearchCatalogueArgs,
    StockSnapshot,
)
from app.store import AppKeyValueStore


UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class InventoryService:
    def __init__(
        self,
        settings: Settings,
        source: HarmoniseInventorySource,
        key_value_store: AppKeyValueStore,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.source = source
        self.key_value_store = key_value_store
        self.logger = logger

    async def get_departments(
        self,
        args: StockGetDepartmentsArgs,
    ) -> tuple[list[StockApiDepartmentDto], str, list[str]]:
        cache_key = self._cache_key(args.model_dump(mode="json"))
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"stock.get_departments:{cache_key}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=lambda: self.source.get_departments(
                include_inactive=args.includeInactive,
                include_sub_departments=args.includeSubDepartments,
            ),
        )
        departments = [StockApiDepartmentDto.model_validate(item) for item in raw]
        return departments, cache_status, notes

    async def get_categories(
        self,
        args: StockGetCategoriesArgs,
    ) -> tuple[StockCategoryDtoPagedResponse, str, list[str]]:
        cache_key = self._cache_key(args.model_dump(mode="json"))
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"stock.get_categories:{cache_key}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=lambda: self.source.get_categories(page=args.page, page_size=args.pageSize),
        )
        return StockCategoryDtoPagedResponse.model_validate(raw), cache_status, notes

    async def search_catalogue(
        self,
        args: StockSearchCatalogueArgs,
    ) -> tuple[ProductListItemDtoPagedResponse, str, list[str]]:
        cache_key = self._cache_key(args.model_dump(mode="json"))
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"stock.search_catalogue:{cache_key}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=lambda: self.source.search_catalogue(
                page=args.page,
                page_size=args.pageSize,
                search=args.search,
                department_id=args.departmentId,
                category_id=args.categoryId,
            ),
        )
        return ProductListItemDtoPagedResponse.model_validate(raw), cache_status, notes

    async def get_product(
        self,
        args: StockGetProductArgs,
    ) -> tuple[ProductListItemDtoPagedResponse, str, list[str]]:
        cache_key = self._cache_key(args.model_dump(mode="json"))
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"stock.get_product:{cache_key}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=lambda: self.source.get_product(
                product_id=args.id,
                sku=args.sku,
                page=args.page,
                page_size=args.pageSize,
            ),
        )
        return ProductListItemDtoPagedResponse.model_validate(raw), cache_status, notes

    async def extract_variant_evidence(
        self,
        args: StockExtractVariantEvidenceArgs,
        matched_on: list[str] | None = None,
        confidence: float | None = None,
        tool_name: str = "stock.extract_variant_evidence",
    ) -> tuple[NormalizedEvidence, str, list[str]]:
        # Root Cause vs Logic: variant-only lookups were building StockGetProductArgs
        # before checking whether the upstream products endpoint had a resolvable id
        # or sku, so the caller saw a generic validation failure instead of clear
        # guidance to reuse the catalogue item's variants[].sku or product id.
        if args.variantId and not args.id and not args.sku:
            raise ParameterMappingError(
                "variantId alone cannot resolve product details. Reuse the matching catalogue "
                "item's variants[].sku or product id when calling stock.get_variant_evidence."
            )
        lookup_args = StockGetProductArgs(
            id=args.id if args.id and self.looks_like_uuid(args.id) else None,
            sku=args.sku if args.sku else None,
            page=1,
            pageSize=20,
        )
        product_response, cache_status, notes = await self.get_product(lookup_args)
        if not product_response.items:
            raise InventoryNotFoundError("No product record was returned for evidence extraction.")
        product = product_response.items[0]
        variant_index, variant = self._select_variant(product, args)
        evidence = self._normalize_variant_evidence(
            product=product,
            variant=variant,
            variant_index=variant_index,
            matched_on=matched_on or self._default_match_reasons(args),
            confidence=confidence or 0.99,
            tool_name=tool_name,
        )
        return evidence, cache_status, notes

    async def compare_variants(
        self,
        identifiers: list[str],
    ) -> tuple[list[NormalizedEvidence], str, list[str]]:
        evidence_items: list[NormalizedEvidence] = []
        cache_statuses: list[str] = []
        notes: list[str] = []
        for identifier in identifiers:
            extract_args = StockExtractVariantEvidenceArgs(
                id=identifier if self.looks_like_uuid(identifier) else None,
                sku=None if self.looks_like_uuid(identifier) else identifier,
            )
            evidence, cache_status, extract_notes = await self.extract_variant_evidence(
                args=extract_args,
                matched_on=["identifier"],
                confidence=0.99,
                tool_name="stock.compare_variants",
            )
            evidence_items.append(evidence)
            cache_statuses.append(cache_status)
            notes.extend(extract_notes)
        combined_status = "cache_mixed"
        if cache_statuses and len(set(cache_statuses)) == 1:
            combined_status = cache_statuses[0]
        return evidence_items, combined_status, notes

    @staticmethod
    def looks_like_uuid(value: str | None) -> bool:
        return bool(value and UUID_PATTERN.match(value))

    def _cache_key(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, default=str)

    def _default_match_reasons(self, args: StockExtractVariantEvidenceArgs) -> list[str]:
        reasons: list[str] = []
        if args.sku:
            reasons.append("sku")
        if args.id:
            reasons.append("product_id")
        if args.variantId:
            reasons.append("variant_id")
        return reasons or ["direct_lookup"]

    def _select_variant(
        self,
        product: ProductListItemDto,
        args: StockExtractVariantEvidenceArgs,
    ) -> tuple[int, ProductVariantDto]:
        for index, variant in enumerate(product.variants):
            if args.variantId and variant.id == args.variantId:
                return index, variant
            if args.sku and variant.sku == args.sku:
                return index, variant
        if len(product.variants) == 1:
            return 0, product.variants[0]
        raise ParameterMappingError(
            "The selected product has multiple variants. Provide a specific sku or variantId to continue safely."
        )

    def _normalize_variant_evidence(
        self,
        product: ProductListItemDto,
        variant: ProductVariantDto,
        variant_index: int,
        matched_on: list[str],
        confidence: float,
        tool_name: str,
    ) -> NormalizedEvidence:
        details = variant.details
        product_path = "items[0]"
        variant_path = f"{product_path}.variants[{variant_index}]"
        details_path = f"{variant_path}.details"
        evidence_paths = {
            "product_name": f"{product_path}.name",
            "variant_name": f"{variant_path}.name",
            "sku": f"{variant_path}.sku",
            "totalHirable": f"{variant_path}.totalHirable",
            "generalRate": f"{details_path}.generalRate",
            "expoRate": f"{details_path}.expoRate",
            "cost": f"{details_path}.cost",
            "dimensional": f"{details_path}.dimensional",
            "canBeSoldInPortions": f"{details_path}.canBeSoldInPortions",
            "length": f"{details_path}.length",
            "width": f"{details_path}.width",
            "height": f"{details_path}.height",
            "vicStock": f"{details_path}.vicStock",
            "vicHirable": f"{details_path}.vicHirable",
            "nswStock": f"{details_path}.nswStock",
            "nswHirable": f"{details_path}.nswHirable",
            "qldStock": f"{details_path}.qldStock",
            "qldHirable": f"{details_path}.qldHirable",
            "totalStock": f"{details_path}.totalStock",
            "lastUpdatedDate": f"{details_path}.lastUpdatedDate",
            "imageFileName": f"{details_path}.imageFileName",
            "isActive": f"{details_path}.isActive",
        }
        return NormalizedEvidence(
            entity_level="variant",
            product_id=product.id,
            product_name=(product.name or "").strip() or None,
            variant_id=variant.id,
            variant_name=(variant.name or "").strip() or None,
            sku=variant.sku,
            departmentId=details.departmentId if details else product.departmentId,
            subDepartmentId=details.subDepartmentId if details else product.subDepartmentId,
            categoryId=(details.assignedCategoryId if details else None) or product.categoryId,
            isActive=(details.isActive if details else None) if details else product.isActive,
            pricing=PricingSnapshot(
                generalRate=details.generalRate if details else None,
                expoRate=details.expoRate if details else None,
                cost=details.cost if details else None,
            ),
            dimensions=DimensionsSnapshot(
                dimensional=details.dimensional if details else None,
                canBeSoldInPortions=details.canBeSoldInPortions if details else None,
                length=details.length if details else None,
                width=details.width if details else None,
                height=details.height if details else None,
            ),
            stock=StockSnapshot(
                totalHirable=variant.totalHirable,
                vicStock=details.vicStock if details else None,
                vicHirable=details.vicHirable if details else None,
                nswStock=details.nswStock if details else None,
                nswHirable=details.nswHirable if details else None,
                qldStock=details.qldStock if details else None,
                qldHirable=details.qldHirable if details else None,
                totalStock=details.totalStock if details else None,
            ),
            lifecycle=LifecycleSnapshot(
                isActive=details.isActive if details else product.isActive,
                startDate=details.startDate if details else None,
                endDate=details.endDate if details else None,
                lastUpdatedDate=details.lastUpdatedDate if details else None,
            ),
            media=MediaSnapshot(imageFileName=details.imageFileName if details else None),
            components=list(details.components if details else []),
            provenance=ProvenanceSnapshot(
                tool=tool_name,
                matched_on=matched_on,
                confidence=round(confidence, 2),
                source_path=details_path if details else variant_path,
            ),
            evidence_paths=evidence_paths,
        )
