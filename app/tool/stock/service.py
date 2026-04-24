from __future__ import annotations

import json
import logging
import re
from typing import Any

import anyio
from app.config import (
    Settings,
    InventoryNotFoundError,
    ParameterMappingError,
    UpstreamServiceError,
)
from app.text.utils import lexical_overlap, significant_tokens
from app.tool.stock.media import build_harmonise_image_url
from app.tool.stock.source import HarmoniseInventorySource
from app.schemas import (
    InventorySnapshotCoverage,
    InventorySnapshotResponse,
    InventorySnapshotRow,
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
    StockInventorySnapshotArgs,
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
        # Motivation vs Logic: compare requests with multiple identifiers should
        # resolve each variant concurrently to reduce end-to-end latency while
        # still preserving deterministic output order for the response payload.
        parallelism = self._parallel_stock_requests_limit(len(identifiers))
        semaphore = anyio.Semaphore(parallelism)
        results: list[tuple[NormalizedEvidence, str, list[str]] | None] = [None] * len(identifiers)

        async def resolve_identifier(index: int, identifier: str) -> None:
            extract_args = StockExtractVariantEvidenceArgs(
                id=identifier if self.looks_like_uuid(identifier) else None,
                sku=None if self.looks_like_uuid(identifier) else identifier,
            )
            async with semaphore:
                results[index] = await self.extract_variant_evidence(
                    args=extract_args,
                    matched_on=["identifier"],
                    confidence=0.99,
                    tool_name="stock.compare_variants",
                )

        async with anyio.create_task_group() as task_group:
            for index, identifier in enumerate(identifiers):
                task_group.start_soon(resolve_identifier, index, identifier)

        resolved_results = [result for result in results if result is not None]
        evidence_items = [result[0] for result in resolved_results]
        cache_statuses = [result[1] for result in resolved_results]
        notes: list[str] = []
        for _, _, extract_notes in resolved_results:
            notes.extend(extract_notes)
        return evidence_items, self._combine_cache_statuses(cache_statuses), notes

    async def inventory_snapshot(
        self,
        args: StockInventorySnapshotArgs,
    ) -> tuple[InventorySnapshotResponse, str, list[str]]:
        # Motivation vs Logic: broad inventory questions were forcing the model
        # to chain dozens of raw `stock.get_product` calls, which ballooned the
        # context window and often ended with an empty synthesis turn. This
        # composition path keeps tool choice LLM-driven while returning a single
        # compact, answer-ready evidence bundle for large table requests.
        catalogue_args = StockSearchCatalogueArgs(
            page=args.page,
            pageSize=args.pageSize,
            search=args.search,
            departmentId=args.departmentId,
            categoryId=args.categoryId,
        )
        catalogue_response, catalogue_cache_status, notes = await self.search_catalogue(catalogue_args)

        # Motivation vs Logic: broad inventory questions often need complete
        # coverage, and relying on a single page caused under-counted snapshots.
        # We now continue scanning remaining matched pages so enrichment can
        # include every matched catalogue product in the requested slice.
        catalogue_items = list(catalogue_response.items)
        cache_statuses = [catalogue_cache_status]
        if catalogue_response.totalPages > args.page:
            extra_items, extra_cache_statuses, extra_notes = await self._fetch_catalogue_pages(
                page_numbers=list(range(args.page + 1, catalogue_response.totalPages + 1)),
                page_size=args.pageSize,
                search=args.search,
                department_id=args.departmentId,
                category_id=args.categoryId,
            )
            cache_statuses.extend(extra_cache_statuses)
            notes.extend(extra_notes)
            catalogue_items.extend(extra_items)

        # Root Cause vs Logic: cloud catalogue search can under-return broad
        # family queries such as "chair" even though additional matching
        # products exist in the same department. We widen the scan using the
        # inferred department, then locally filter by the original query so the
        # snapshot can hydrate every matching chair variant by SKU.
        (
            catalogue_items,
            expansion_cache_statuses,
            expansion_notes,
            matched_pages,
        ) = await self._expand_catalogue_matches_for_snapshot(
            args=args,
            initial_items=catalogue_items,
            initial_total_pages=catalogue_response.totalPages,
        )
        cache_statuses.extend(expansion_cache_statuses)
        notes.extend(expansion_notes)
        catalogue_items = self._dedupe_products(catalogue_items)

        evidence_items: list[NormalizedEvidence] = []
        coverage_limitations: list[str] = []
        enriched_products = 0
        detail_lookup_failures: list[str] = []
        # Motivation vs Logic: enriching snapshot variants requires many product
        # detail calls, so we parallelize these lookups with bounded concurrency
        # to reduce runtime without overwhelming upstream inventory endpoints.
        parallelism = self._parallel_stock_requests_limit(len(catalogue_items))
        semaphore = anyio.Semaphore(parallelism)
        detail_results: list[
            tuple[
                ProductListItemDto,
                ProductListItemDtoPagedResponse | None,
                str | None,
                list[str],
                UpstreamServiceError | None,
            ]
            | None
        ] = [None] * len(catalogue_items)

        async def fetch_detail(index: int, product: ProductListItemDto) -> None:
            sku = self._detail_lookup_sku(product)
            # Root Cause vs Logic: Harmonise's products endpoint consistently
            # returns 500 for id-based lookups; sku-based lookups succeed.
            # We never fall back to product.id — if no variant carries a sku,
            # the product is recorded as a coverage gap and skipped cleanly.
            if not sku:
                detail_results[index] = (product, None, None, [], None)
                return
            detail_args = StockGetProductArgs(
                id=None,
                sku=sku,
                page=1,
                pageSize=min(100, max(20, len(product.variants) or 1)),
            )
            try:
                async with semaphore:
                    product_response, product_cache_status, product_notes = await self.get_product(detail_args)
                detail_results[index] = (product, product_response, product_cache_status, product_notes, None)
            except UpstreamServiceError as exc:
                self.logger.warning(
                    "Detail lookup failed for sku %s: %s (status %s)",
                    sku,
                    exc.detail,
                    exc.status_code,
                )
                detail_results[index] = (product, None, None, [], exc)

        async with anyio.create_task_group() as task_group:
            for index, product in enumerate(catalogue_items):
                task_group.start_soon(fetch_detail, index, product)

        for detail_result in detail_results:
            if detail_result is None:
                continue
            product, product_response, product_cache_status, product_notes, detail_error = detail_result
            result_sku = self._detail_lookup_sku(product) or f"(no-sku:{product.id})"
            if detail_error is not None:
                detail_lookup_failures.append(
                    f"sku={result_sku} (status {detail_error.status_code}): {detail_error.detail}"
                )
                continue

            if product_cache_status is not None:
                cache_statuses.append(product_cache_status)
            notes.extend(product_notes)

            if product_response is None or not product_response.items:
                coverage_limitations.append(
                    f"No detail payload was returned for sku {result_sku}; its variants were skipped."
                )
                continue

            detail_product = product_response.items[0]
            enriched_products += 1
            for variant_index, variant in enumerate(detail_product.variants):
                evidence_items.append(
                    self._normalize_variant_evidence(
                        product=detail_product,
                        variant=variant,
                        variant_index=variant_index,
                        matched_on=["catalogue_snapshot", "product_id"],
                        confidence=0.96,
                        tool_name="stock.inventory_snapshot",
                    )
                )

        if detail_lookup_failures:
            failure_count = len(detail_lookup_failures)
            coverage_limitations.append(
                (
                    f"Detail lookups failed for {failure_count} sku"
                    f"{'s' if failure_count != 1 else ''}; last failure was "
                    f"{detail_lookup_failures[-1]}. Some specs may be incomplete."
                )
            )

        response = InventorySnapshotResponse(
            rows=[self._build_inventory_row(item) for item in evidence_items],
            evidence=evidence_items,
            coverage=InventorySnapshotCoverage(
                requestedPage=args.page,
                requestedPageSize=args.pageSize,
                matchedProducts=len(catalogue_items),
                matchedPages=matched_pages,
                enrichedProducts=enriched_products,
                enrichedVariants=len(evidence_items),
                isPartial=bool(coverage_limitations),
                limitations=coverage_limitations,
            ),
        )
        return response, self._combine_cache_statuses(cache_statuses), notes

    @staticmethod
    def looks_like_uuid(value: str | None) -> bool:
        return bool(value and UUID_PATTERN.match(value))

    def _cache_key(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, default=str)

    def _detail_lookup_sku(self, product: ProductListItemDto) -> str | None:
        # Root Cause vs Logic: Harmonise detail lookups fail when using a
        # catalogue product id directly, so prefer the first variant sku instead.
        for variant in product.variants:
            if variant.sku:
                return variant.sku
        return None

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
        option_labels = self._resolve_option_labels(product, variant)
        evidence_paths = {
            "product_name": f"{product_path}.name",
            "variant_name": f"{variant_path}.name",
            "sku": f"{variant_path}.sku",
            "variation_options": f"{product_path}.variations[*].options[*].name",
            "salesNote": f"{details_path}.salesNote",
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
            "imageUrl": "derived: CLOUD_HARMONISE_IMAGE + imageFileName",
            "isActive": f"{details_path}.isActive",
        }
        image_file_name = details.imageFileName if details else None
        return NormalizedEvidence(
            entity_level="variant",
            product_id=product.id,
            product_name=(product.name or "").strip() or None,
            variant_id=variant.id,
            variant_name=(variant.name or "").strip() or None,
            sku=variant.sku,
            variation_options=option_labels,
            salesNote=details.salesNote if details else None,
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
            media=MediaSnapshot(
                imageFileName=image_file_name,
                imageUrl=build_harmonise_image_url(self.settings.cloud_harmonise_image, image_file_name),
            ),
            components=list(details.components if details else []),
            provenance=ProvenanceSnapshot(
                tool=tool_name,
                matched_on=matched_on,
                confidence=round(confidence, 2),
                source_path=details_path if details else variant_path,
            ),
            evidence_paths=evidence_paths,
        )

    def _resolve_option_labels(self, product: ProductListItemDto, variant: ProductVariantDto) -> list[str]:
        option_lookup = {
            option.id: (option.name or "").strip()
            for variation in product.variations
            for option in variation.options
            if (option.name or "").strip()
        }
        return [option_lookup[option_id] for option_id in variant.optionIds if option_id in option_lookup]

    def _build_inventory_row(self, evidence: NormalizedEvidence) -> InventorySnapshotRow:
        return InventorySnapshotRow(
            product=evidence.product_name,
            variant=evidence.variant_name,
            sku=evidence.sku,
            attributeEvidence=self._dedupe_text(
                [
                    evidence.variant_name,
                    evidence.product_name,
                    *evidence.variation_options,
                ]
            ),
            size=self._format_dimensions(evidence),
            stock=self._format_stock(evidence),
            knownSpecs=self._build_known_specs(evidence),
        )

    def _format_dimensions(self, evidence: NormalizedEvidence) -> str | None:
        parts = [
            value
            for value in [
                evidence.dimensions.length,
                evidence.dimensions.width,
                evidence.dimensions.height,
            ]
            if value is not None
        ]
        if not parts:
            return None
        rendered = " x ".join(f"{value:g}" for value in parts)
        return f"{rendered} m"

    def _format_stock(self, evidence: NormalizedEvidence) -> str | None:
        # Motivation vs Logic: snapshot rows are rendered directly into user-facing
        # answers, so stock details are translated into plain language instead of
        # exposing telemetry-like `key=value` fragments.
        overview = self._describe_stock_scope(
            scope="Overall",
            stock_value=evidence.stock.totalStock,
            hirable_value=evidence.stock.totalHirable,
        )
        regional_summaries = [
            summary
            for summary in [
                self._describe_stock_scope(
                    scope="VIC",
                    stock_value=evidence.stock.vicStock,
                    hirable_value=evidence.stock.vicHirable,
                ),
                self._describe_stock_scope(
                    scope="NSW",
                    stock_value=evidence.stock.nswStock,
                    hirable_value=evidence.stock.nswHirable,
                ),
                self._describe_stock_scope(
                    scope="QLD",
                    stock_value=evidence.stock.qldStock,
                    hirable_value=evidence.stock.qldHirable,
                ),
            ]
            if summary
        ]

        segments: list[str] = []
        if overview:
            segments.append(overview)
        if regional_summaries:
            segments.append("By location: " + "; ".join(regional_summaries))
        if not segments:
            return None
        return ". ".join(segments) + "."

    def _describe_stock_scope(
        self,
        scope: str,
        stock_value: int | None,
        hirable_value: int | None,
    ) -> str | None:
        if stock_value is None and hirable_value is None:
            return None
        if stock_value is not None and hirable_value is not None:
            if stock_value == 0 and hirable_value == 0:
                return f"{scope} is currently out of stock and unavailable for hire"
            if stock_value == hirable_value:
                return f"{scope} has {stock_value} in stock, with all {hirable_value} available for hire"
            return f"{scope} has {stock_value} in stock, with {hirable_value} available for hire"
        if stock_value is not None:
            if stock_value == 0:
                return f"{scope} is currently out of stock"
            return f"{scope} has {stock_value} in stock"
        if hirable_value == 0:
            return f"{scope} currently has none available for hire"
        return f"{scope} has {hirable_value} available for hire"

    def _build_known_specs(self, evidence: NormalizedEvidence) -> list[str]:
        # Motivation vs Logic: Non-technical consumers read the table more easily when
        # the known specs are spelled out in plain language instead of raw keys+values.
        specs: list[str] = [
            self._describe_activation(evidence.isActive),
            self._describe_dimensionality(evidence.dimensions.dimensional),
            self._describe_portions(evidence.dimensions.canBeSoldInPortions),
            self._describe_pricing("generalRate", evidence.pricing.generalRate),
            self._describe_pricing("expoRate", evidence.pricing.expoRate),
            self._describe_pricing("cost", evidence.pricing.cost),
            self._describe_components(evidence.components),
            self._describe_sales_note(evidence.salesNote),
        ]
        return [spec for spec in specs if spec]

    def _combine_cache_statuses(self, cache_statuses: list[str]) -> str:
        if cache_statuses and len(set(cache_statuses)) == 1:
            return cache_statuses[0]
        return "cache_mixed"

    def _parallel_stock_requests_limit(self, item_count: int) -> int:
        # Motivation vs Logic: variant-rich catalogue requests were bottlenecked by
        # a tiny fan-out, so we now allow up to the configurable concurrency limit
        # (default 50). The semaphore still caps active workers per session,
        # sending any remaining items to sequential retries.
        limit = max(1, self.settings.stock_parallel_requests_limit)
        return max(1, min(limit, max(1, item_count)))

    async def _fetch_catalogue_pages(
        self,
        *,
        page_numbers: list[int],
        page_size: int,
        search: str | None,
        department_id: int | None,
        category_id: str | None,
    ) -> tuple[list[ProductListItemDto], list[str], list[str]]:
        if not page_numbers:
            return [], [], []

        # Motivation vs Logic: catalogue pagination is I/O-bound, so a shared
        # helper keeps page hydration concurrent and reusable instead of
        # repeating bespoke loops for each snapshot-broadening path.
        page_results: list[tuple[ProductListItemDtoPagedResponse, str, list[str]] | None] = [None] * len(page_numbers)
        parallelism = max(1, min(self.settings.snapshot_expand_parallel_pages_limit, len(page_numbers)))
        semaphore = anyio.Semaphore(parallelism)

        async def fetch_page(index: int, page_number: int) -> None:
            page_args = StockSearchCatalogueArgs(
                page=page_number,
                pageSize=page_size,
                search=search,
                departmentId=department_id,
                categoryId=category_id,
            )
            async with semaphore:
                page_results[index] = await self.search_catalogue(page_args)

        async with anyio.create_task_group() as task_group:
            for index, page_number in enumerate(page_numbers):
                task_group.start_soon(fetch_page, index, page_number)

        items: list[ProductListItemDto] = []
        cache_statuses: list[str] = []
        notes: list[str] = []
        for item in page_results:
            if item is None:
                continue
            page_response, page_cache_status, page_notes = item
            cache_statuses.append(page_cache_status)
            notes.extend(page_notes)
            items.extend(page_response.items)
        return items, cache_statuses, notes

    async def _expand_catalogue_matches_for_snapshot(
        self,
        *,
        args: StockInventorySnapshotArgs,
        initial_items: list[ProductListItemDto],
        initial_total_pages: int,
    ) -> tuple[list[ProductListItemDto], list[str], list[str], int]:
        if not args.search:
            return initial_items, [], [], initial_total_pages

        department_id = args.departmentId or self._dominant_department_id(initial_items)
        if department_id is None:
            return initial_items, [], [], initial_total_pages

        matched_tokens = significant_tokens(args.search)
        if not matched_tokens:
            return initial_items, [], [], initial_total_pages
        if len(initial_items) > self.settings.snapshot_expand_max_initial_items:
            return initial_items, [], [], initial_total_pages
        if self._query_specificity_score(args.search, initial_items) >= self.settings.snapshot_specificity_threshold:
            return initial_items, [], [], initial_total_pages

        broadened_args = StockSearchCatalogueArgs(
            page=1,
            pageSize=max(args.pageSize, 100),
            search=None,
            departmentId=department_id,
            categoryId=args.categoryId,
        )
        broadened_response, broadened_cache_status, broadened_notes = await self.search_catalogue(broadened_args)
        broadened_items = list(broadened_response.items)
        broadened_cache_statuses = [broadened_cache_status]
        if broadened_response.totalPages > 1:
            extra_items, extra_cache_statuses, extra_notes = await self._fetch_catalogue_pages(
                page_numbers=list(range(2, broadened_response.totalPages + 1)),
                page_size=broadened_args.pageSize,
                search=None,
                department_id=department_id,
                category_id=args.categoryId,
            )
            broadened_cache_statuses.extend(extra_cache_statuses)
            broadened_notes.extend(extra_notes)
            broadened_items.extend(extra_items)

        filtered_broadened = [
            product
            for product in self._dedupe_products(broadened_items)
            if self._product_matches_query_tokens(product, matched_tokens)
        ]
        if len(filtered_broadened) <= len(initial_items):
            return initial_items, broadened_cache_statuses, broadened_notes, initial_total_pages

        broadened_notes.append(
            (
                "Expanded catalogue coverage via department scan because the initial "
                f"search for '{args.search}' returned fewer products than the inferred department catalogue."
            )
        )
        merged_items = self._dedupe_products([*initial_items, *filtered_broadened])
        return merged_items, broadened_cache_statuses, broadened_notes, max(initial_total_pages, broadened_response.totalPages)

    def _query_specificity_score(self, query: str | None, products: list[ProductListItemDto]) -> float:
        if not query or not products:
            return 0.0
        query_tokens = significant_tokens(query)
        # Motivation vs Logic: single-token queries (for example, broad family
        # nouns) are usually underspecified and should not suppress expansion.
        if len(query_tokens) <= 1:
            return 0.0

        best = 0.0
        for product in products:
            candidate_text = " ".join(
                part
                for part in [
                    product.name or "",
                    *(variant.name or "" for variant in product.variants),
                ]
                if part
            )
            score = lexical_overlap(query, candidate_text)
            if score > best:
                best = score
        return best

    def _dominant_department_id(self, products: list[ProductListItemDto]) -> int | None:
        counts: dict[int, int] = {}
        for product in products:
            counts[product.departmentId] = counts.get(product.departmentId, 0) + 1
        if not counts:
            return None
        return max(sorted(counts), key=lambda department_id: counts[department_id])

    def _product_matches_query_tokens(self, product: ProductListItemDto, query_tokens: list[str]) -> bool:
        haystack = " ".join(
            value
            for value in [
                product.name or "",
                *(variant.name or "" for variant in product.variants),
                *(variant.sku or "" for variant in product.variants),
            ]
            if value
        ).lower()
        return all(token in haystack for token in query_tokens)

    def _dedupe_products(self, products: list[ProductListItemDto]) -> list[ProductListItemDto]:
        deduped: list[ProductListItemDto] = []
        seen: set[str] = set()
        for product in products:
            if product.id in seen:
                continue
            seen.add(product.id)
            deduped.append(product)
        return deduped

    def _dedupe_text(self, values: list[str | None]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = (value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    @staticmethod
    def _describe_activation(is_active: bool | None) -> str | None:
        if is_active is None:
            return None
        return "This variant is active on the current catalogue." if is_active else "This variant is currently inactive."

    @staticmethod
    def _describe_dimensionality(dimensional: bool | None) -> str | None:
        if dimensional is None:
            return None
        return "Dimensional handling is required for this item." if dimensional else "Dimensional handling does not apply."

    @staticmethod
    def _describe_portions(can_be_sold: bool | None) -> str | None:
        if can_be_sold is None:
            return None
        return "Can be sold in portions." if can_be_sold else "Must be sold as a whole unit."

    def _describe_pricing(self, key: str, value: float | None) -> str | None:
        if value is None:
            return None
        return f"{self._humanize_label(key)} is {value:g}."

    @staticmethod
    def _describe_components(components: list[ProductComponentAllocationDto]) -> str | None:
        if not components:
            return None
        parts = ", ".join(f"{component.componentId} ×{component.quantity}" for component in components)
        return f"Components included: {parts}."

    @staticmethod
    def _describe_sales_note(note: str | None) -> str | None:
        if not note:
            return None
        return f"Sales note: {note}."

    def _humanize_label(self, label: str) -> str:
        words = label.replace("_", " ")
        words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", words)
        return words.strip().title()
