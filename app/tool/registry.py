from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.auth.models import UserContext
from app.auth.context import get_user_context
from app.auth.gateway import IdentityAuthError
from app.tool.currency import (
    CurrencyConvertArgs,
    CurrencyFluctuationArgs,
    CurrencyHistoryArgs,
    CurrencyLatestArgs,
    CurrencyService,
    CurrencySymbolsArgs,
    CurrencyTimeseriesArgs,
)
from app.config import ParameterMappingError, UnsupportedToolError
from app.tool.stock.intelligence import rank_evidence_with_filters
from app.tool.stock.media import build_harmonise_image_url
from app.tool.stock.service import InventoryService
from app.tool.news import NewsHeadlinesArgs, NewsSearchArgs, NewsService, NewsSourcesArgs
from app.tool.news.formatter import format_news_articles, format_news_sources
from app.resolver import ResolverService
from app.schemas import (
    InventorySnapshotResponse,
    McpImageContent,
    NormalizedEvidence,
    ProductListItemDto,
    ProductListItemDtoPagedResponse,
    ResolverDisambiguateCandidatesArgs,
    SessionToolArgs,
    StockAggregateArgs,
    StockCompareVariantsArgs,
    StockCountItemsArgs,
    StockExtractVariantEvidenceArgs,
    StockGetCategoriesArgs,
    StockGetDepartmentsArgs,
    StockGetProductArgs,
    StockGetSupportedScopeArgs,
    StockHirableByStateArgs,
    StockImageArgs,
    StockInventorySnapshotArgs,
    StockProductFamilyInventoryArgs,
    StockSearchCatalogueArgs,
    StockSpecsRankArgs,
    StockVariantRankArgs,
    ToolDefinition,
    ToolResult,
    ToolTrace,
    VariantCapMetadata,
)
from app.prompt.context import render_session_context, summarize_session_state
from app.prompt.stock.furniture import furniture_capability_summary
from app.session.store import SessionStore
from app.text.utils import lexical_overlap
from app.tool.weather import WeatherCurrentArgs, WeatherForecastArgs, WeatherHistoryArgs, WeatherResolveArgs, WeatherService
from app.mcp.tool import McpToolNameMap, is_mcp_safe_tool_name, normalize_mcp_tool_name


@dataclass
class ToolSpec:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[BaseModel, str | None, str], Awaitable[ToolResult]]
    visible: bool = True
    plugin: str | None = None


# Motivation vs Logic: debug logs are for shape and non-sensitive query params; strip
# stable IDs and session handles so local traces stay readable without leaking long UUIDs.
_TOOL_LOG_REDACT_KEYS: frozenset[str] = frozenset({"id", "sessionId"})


def _redact_args_for_tool_log(raw_args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw_args.items() if k not in _TOOL_LOG_REDACT_KEYS}


class ToolRegistry:
    def __init__(
        self,
        inventory_service: InventoryService | None,
        resolver_service: ResolverService,
        session_store: SessionStore,
        news_service: NewsService,
        weather_service: WeatherService,
        currency_service: CurrencyService,
        logger: logging.Logger,
        *,
        inventory_tools_enabled: bool = True,
    ) -> None:
        self.inventory_service = inventory_service
        self.resolver_service = resolver_service
        self.session_store = session_store
        self.news_service = news_service
        self.weather_service = weather_service
        self.currency_service = currency_service
        self.logger = logger
        self.inventory_tools_enabled = inventory_tools_enabled and inventory_service is not None
        self._tools: dict[str, ToolSpec] = {}
        if self.inventory_tools_enabled:
            # Motivation vs Logic: inventory-backed tools should disappear cleanly
            # when Harmonise is unavailable, so stock, resolver, and session
            # registrations are kept behind one runtime capability gate.
            self._register_stock()
            self._register_resolver()
            self._register_session()
        self._register_news()
        self._register_weather()
        self._register_currency()
        self._tool_name_map = McpToolNameMap(list(self._tools))

    def list_tools(
        self,
        *,
        include_hidden: bool = True,
        user_context: UserContext | None = None,
    ) -> list[ToolDefinition]:
        user_context = user_context or get_user_context()
        tools: list[ToolDefinition] = []
        for spec in self._tools.values():
            if not include_hidden and not spec.visible:
                continue
            if user_context is not None and not self._is_plugin_authorized(spec, user_context):
                continue
            public_name = self._tool_name_map.to_public(spec.name)
            # Root Cause vs Logic: Claude.ai rejects dotted tool identifiers, so we
            # reuse the MCP map to keep front-end names compliant while preserving
            # the same mapping that `_tool_name_map` already tracks for reverse resolution.
            if not is_mcp_safe_tool_name(public_name):  # pragma: no cover - defensive invariant
                raise UnsupportedToolError(
                    f"Configured tool '{spec.name}' produced unsafe MCP name '{public_name}'."
                )
            tools.append(
                ToolDefinition(
                    name=public_name,
                    description=spec.description,
                    input_schema=spec.model.model_json_schema(),
                )
            )
        return tools

    def tool_payloads(
        self,
        *,
        include_hidden: bool = True,
        user_context: UserContext | None = None,
    ) -> list[dict[str, Any]]:
        user_context = user_context or get_user_context()
        payloads: list[dict[str, Any]] = []
        for spec in self._tools.values():
            if not include_hidden and not spec.visible:
                continue
            if user_context is not None and not self._is_plugin_authorized(spec, user_context):
                continue
            public_name = self._tool_name_map.to_public(spec.name)
            # Root Cause vs Logic: keep the REST function payload signature aligned with
            # the MCP-safe name so callers sharing these definitions stay compliant.
            if not is_mcp_safe_tool_name(public_name):  # pragma: no cover - defensive invariant
                raise UnsupportedToolError(f"Configured tool '{spec.name}' produced unsafe MCP name '{public_name}'.")
            payloads.append(
                {
                    "type": "function",
                    "function": {
                        "name": public_name,
                        "description": spec.description,
                        "parameters": spec.model.model_json_schema(),
                    },
                }
            )
        return payloads

    async def call_tool(
        self,
        tool_name: str,
        raw_args: dict[str, Any],
        session_id: str | None = None,
        thought: str = "",
        user_context: UserContext | None = None,
    ) -> ToolResult:
        tool_name = self.resolve_tool_name(tool_name)
        self.logger.debug("tool_call tool=%s args=%s", tool_name, _redact_args_for_tool_log(raw_args))
        spec = self._tools.get(tool_name)
        if spec is None:
            raise UnsupportedToolError(f"Unsupported tool '{tool_name}'.")
        user_context = user_context or get_user_context()
        if user_context is not None:
            self._authorize_tool_access(spec, raw_args, user_context)
        try:
            validated = spec.model.model_validate(raw_args)
        except ValidationError as exc:
            raise ParameterMappingError(self._format_validation_error(tool_name, exc)) from exc
        return await spec.handler(validated, session_id, thought)

    def resolve_tool_name(self, tool_name: str) -> str:
        # Root Cause vs Logic: `/query`, REST function payloads, and MCP all need
        # validator-safe tool names. Resolve through the shared map at the registry
        # boundary so future aliases still execute the same implementation.
        if tool_name in self._tools:
            return tool_name
        return self._tool_name_map.to_internal(tool_name)

    def _format_validation_error(self, tool_name: str, exc: ValidationError) -> str:
        parts: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "Invalid value."))
            if location:
                parts.append(f"{location}: {message}")
            else:
                parts.append(message)
        rendered = "; ".join(parts) if parts else "Invalid arguments."
        return f"Invalid arguments for '{tool_name}': {rendered}"

    def _register(
        self,
        name: str,
        description: str,
        model: type[BaseModel],
        handler,
        *,
        visible: bool = True,
        plugin: str | None = None,
    ) -> None:
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            model=model,
            handler=handler,
            visible=visible,
            plugin=plugin or self._infer_plugin_name(name),
        )

    def _is_plugin_authorized(self, spec: ToolSpec, user_context: UserContext) -> bool:
        if not spec.plugin:
            return True
        return spec.plugin in set(user_context.plugin_permissions)

    def _authorize_tool_access(self, spec: ToolSpec, raw_args: dict[str, Any], user_context: UserContext) -> None:
        if not self._is_plugin_authorized(spec, user_context):
            raise IdentityAuthError(
                code="tool_access_denied",
                message=(
                    f"Tool '{spec.name}' requires access to the '{spec.plugin}' plugin; "
                    f"user plugin permissions were {user_context.plugin_permissions}."
                ),
                status_code=403,
            )
        # Department-based access is disabled for now. We keep the earlier
        # department gate commented out below as a reference for re-enable work.
        # department_id = raw_args.get("departmentId")
        # if department_id is None:
        #     return
        # if not user_context.department_claim:
        #     raise IdentityAuthError(
        #         code="missing_claims",
        #         message="departmentId was supplied but no department claim is present in the token.",
        #         status_code=403,
        #         missing_claims=["extension_departmentId|extension_department|officeLocation"],
        #     )

    @staticmethod
    def _infer_plugin_name(tool_name: str) -> str | None:
        if tool_name.startswith(("stock_", "resolver_", "session_")):
            return "stock"
        if tool_name.startswith("news_"):
            return "news"
        if tool_name.startswith("weather_"):
            return "weather"
        if tool_name.startswith(("fx_", "currency_")):
            return "currency"
        return None

    def _register_stock(self) -> None:
        async def get_departments(validated: StockGetDepartmentsArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.get_departments(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock_get_departments",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> departments[*]",
                result_count=len(data),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_get_departments",
                data=[item.model_dump(mode="json") for item in data],
                normalization_notes=notes,
                trace=trace,
            )

        async def get_categories(validated: StockGetCategoriesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.get_categories(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock_get_categories",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> categories.items[*]",
                result_count=len(data.items),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_get_categories",
                data=data.model_dump(mode="json"),
                normalization_notes=notes,
                trace=trace,
            )

        async def search_catalogue(validated: StockSearchCatalogueArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.search_catalogue(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock_search",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> products.items[*]",
                result_count=len(data.items),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_search",
                data=data.model_dump(mode="json"),
                llm_content=self._catalogue_model_view(data),
                normalization_notes=notes,
                trace=trace,
            )

        async def get_product(validated: StockGetProductArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.get_product(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock_detail",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> products.items[*]",
                result_count=len(data.items),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_detail",
                data=data.model_dump(mode="json"),
                llm_content=self._product_model_view(data),
                normalization_notes=notes,
                trace=trace,
            )

        async def extract_variant(validated: StockExtractVariantEvidenceArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.extract_variant_evidence(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock_extract_variant_evidence",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data=f"harmonise -> {data.provenance.source_path}",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_extract_variant_evidence",
                data=data.model_dump(mode="json"),
                llm_content=self._evidence_model_view(data),
                normalization_notes=notes,
                trace=trace,
            )

        async def compare_variants(validated: StockCompareVariantsArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.compare_variants(validated.identifiers)
            trace = ToolTrace(
                thought=thought,
                tool="stock_compare",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> normalized variant evidence[*]",
                result_count=len(data),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_compare",
                data=[item.model_dump(mode="json") for item in data],
                llm_content=[self._evidence_model_view(item) for item in data],
                normalization_notes=notes,
                trace=trace,
            )

        async def inventory_snapshot(validated: StockInventorySnapshotArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.inventory_snapshot(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock_snapshot",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> inventory_snapshot.rows[*]",
                result_count=len(data.rows),
                normalization_notes=notes + data.coverage.limitations,
            )
            return ToolResult(
                tool="stock_snapshot",
                data=data.model_dump(mode="json"),
                llm_content=self._inventory_snapshot_model_view(data),
                normalization_notes=notes + data.coverage.limitations,
                trace=trace,
            )

        async def aggregate_stock(validated: StockAggregateArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.aggregate_stock(validated)
            ranked_groups = self._aggregate_snapshot_evidence(
                data.evidence,
                region=validated.region,
                measure=validated.measure,
                group_by=validated.groupBy,
                direction=validated.direction,
                limit=validated.limit,
            )
            payload = {
                "query": validated.search,
                "region": validated.region,
                "measure": validated.measure,
                "groupBy": validated.groupBy,
                "direction": validated.direction,
                "rows": ranked_groups,
                "coverage": data.coverage.model_dump(mode="json"),
                "guidance": (
                    "Rows are grouped totals, not individual variant rankings. Use product grouping for user wording "
                    "such as type, family, line, or all inventory unless the user explicitly asks for SKU/variant grain."
                ),
            }
            trace = ToolTrace(
                thought=thought,
                tool="stock_aggregate",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> inventory_snapshot.evidence[*] -> grouped regional stock totals",
                result_count=len(ranked_groups),
                normalization_notes=notes + data.coverage.limitations,
            )
            return ToolResult(
                tool="stock_aggregate",
                data=payload,
                llm_content=payload,
                normalization_notes=notes + data.coverage.limitations,
                trace=trace,
            )

        async def stock_specs_rank(
            validated: StockSpecsRankArgs, _: str | None, thought: str
        ) -> ToolResult:
            snapshot_args = StockInventorySnapshotArgs(
                page=validated.page,
                search=validated.search,
                departmentId=validated.departmentId,
                categoryId=validated.categoryId,
            )
            data, cache_status, notes = await self.inventory_service.inventory_snapshot(snapshot_args)
            filtered_evidence, ranked_rows, filter_notes = rank_evidence_with_filters(
                data.evidence,
                metric=validated.metric,
                region=validated.region,
                group_by=validated.groupBy,
                direction=validated.direction,
                limit=validated.limit,
                attribute_filters=validated.attributeFilters,
            )
            limitations = list(data.coverage.limitations) + filter_notes
            if not ranked_rows:
                limitations.append(
                    "No ranked stock specs rows were produced. Try a narrower product/category phrase, "
                    "or call stock_scope first to pass a departmentId/categoryId filter."
                )
            payload = {
                "query": validated.search,
                "region": validated.region,
                "metric": validated.metric,
                "groupBy": validated.groupBy,
                "direction": validated.direction,
                "attributeFilters": [item.model_dump(mode="json") for item in validated.attributeFilters],
                "rows": ranked_rows,
                "coverage": {
                    **data.coverage.model_dump(mode="json"),
                    "filteredVariants": len(filtered_evidence),
                    "limitations": limitations,
                    "isPartial": data.coverage.isPartial or bool(limitations),
                },
                "guidance": (
                    "Rows are ranked from Harmonise normalized evidence. Stock and hirable metrics are summed across "
                    "the requested hierarchy; physical and financial metrics rank by the best contributing variant for "
                    "the requested direction."
                ),
            }
            trace = ToolTrace(
                thought=thought,
                tool="stock_specs_rank",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> inventory_snapshot.evidence[*] -> stock specs ranking",
                result_count=len(ranked_rows),
                normalization_notes=notes + limitations,
            )
            return ToolResult(
                tool="stock_specs_rank",
                data=payload,
                llm_content=payload,
                normalization_notes=notes + limitations,
                trace=trace,
            )

        async def stock_image(validated: StockImageArgs, _: str | None, thought: str) -> ToolResult:
            image_file_name = validated.imageFileName
            resolved_source = "imageFileName" if image_file_name else None
            resolved_product: ProductListItemDto | None = None
            resolved_variant = None
            cache_statuses: list[str] = []
            notes: list[str] = []
            coverage_limitations: list[str] = []

            # Motivation vs Logic: image retrieval now lives in its own MCP tool so
            # specs ranking stays focused on ranking evidence while image-specific
            # resolution and binary rendering happen in one explicit place.
            if validated.sku and not image_file_name:
                product_response, product_cache_status, product_notes = await self.inventory_service.get_product(
                    StockGetProductArgs(sku=validated.sku, page=1)
                )
                cache_statuses.append(product_cache_status)
                notes.extend(product_notes)
                if product_response.items:
                    resolved_product = product_response.items[0]
                    for variant in resolved_product.variants:
                        if variant.sku == validated.sku:
                            resolved_variant = variant
                            break
                    if resolved_variant is None and resolved_product.variants:
                        resolved_variant = resolved_product.variants[0]
                    if resolved_variant and resolved_variant.details:
                        image_file_name = resolved_variant.details.imageFileName
                        resolved_source = "sku"
                else:
                    coverage_limitations.append(f"No product detail payload was returned for sku {validated.sku}.")

            if validated.search and not image_file_name:
                catalogue_scan = await self.inventory_service.scan_catalogue_with_recovery(
                    StockSearchCatalogueArgs(
                        page=validated.page,
                        search=validated.search,
                        departmentId=validated.departmentId,
                        categoryId=validated.categoryId,
                    )
                )
                cache_statuses.extend(catalogue_scan.cache_statuses)
                notes.extend(catalogue_scan.notes)
                matched_products = self._filter_products_for_query(catalogue_scan.items, validated.search)
                if not matched_products:
                    matched_products = list(catalogue_scan.items)

                for product in matched_products:
                    for variant in product.variants:
                        if not variant.sku:
                            continue
                        product_response, product_cache_status, product_notes = await self.inventory_service.get_product(
                            StockGetProductArgs(sku=variant.sku, page=1)
                        )
                        cache_statuses.append(product_cache_status)
                        notes.extend(product_notes)
                        if not product_response.items:
                            continue
                        detail_product = product_response.items[0]
                        detail_variant = next(
                            (candidate for candidate in detail_product.variants if candidate.sku == variant.sku),
                            detail_product.variants[0] if detail_product.variants else None,
                        )
                        if detail_variant and detail_variant.details and detail_variant.details.imageFileName:
                            resolved_product = detail_product
                            resolved_variant = detail_variant
                            image_file_name = detail_variant.details.imageFileName
                            resolved_source = "search"
                            break
                    if image_file_name:
                        break

                if not image_file_name:
                    coverage_limitations.append(
                        "No imageFileName was resolved from the matched Harmonise product variants for the supplied search."
                    )

            image_url = build_harmonise_image_url(
                self.inventory_service.settings.cloud_harmonise_image,
                image_file_name,
            )
            if image_file_name and not image_url:
                coverage_limitations.append(
                    "An imageFileName was resolved, but a renderable Harmonise HTTP image URL could not be built."
                )

            image_content: list[McpImageContent] = []
            if image_url:
                content, note = await self._fetch_mcp_image_content(image_url)
                if content is not None:
                    image_content.append(content)
                elif note:
                    coverage_limitations.append(note)

            cache_status = self.inventory_service._combine_cache_statuses(cache_statuses) if cache_statuses else "not_applicable"
            payload = {
                "source": resolved_source or "unresolved",
                "query": validated.search,
                "sku": getattr(resolved_variant, "sku", None) or validated.sku,
                "product": resolved_product.name if resolved_product else None,
                "variant": resolved_variant.name if resolved_variant else None,
                "imageFileName": image_file_name,
                "imageUrl": image_url,
                "resolutionNotes": notes,
                "coverage": {
                    "requestedPage": validated.page,
                    "requestedPageSize": self.inventory_service._catalogue_page_size_cap(),
                    "isPartial": bool(coverage_limitations),
                    "limitations": coverage_limitations,
                },
                "guidance": (
                    "Use this tool when the user explicitly needs a Harmonise product image. It can resolve from an "
                    "exact image path, exact SKU, or a product-family search, and it returns both the HTTP image URL "
                    "and MCP-native image content when binary fetch succeeds."
                ),
            }
            trace = ToolTrace(
                thought=thought,
                tool="stock_image",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise image path resolution -> optional MCP image fetch",
                result_count=len(image_content),
                normalization_notes=notes + coverage_limitations,
            )
            return ToolResult(
                tool="stock_image",
                data=payload,
                llm_content=payload,
                mcp_content=image_content,
                normalization_notes=notes + coverage_limitations,
                trace=trace,
            )

        async def get_supported_scope(
            validated: StockGetSupportedScopeArgs, _: str | None, thought: str
        ) -> ToolResult:
            summary = furniture_capability_summary()
            data = {
                **summary,
                "guidance": {
                    "purpose": (
                        "Use this tool for supported stock scope, department/category counts, and categoryId "
                        "routing. It is the MCP-visible source of truth for supported inventory capability."
                    ),
                    "live_inventory": (
                        "For products, variants, or availability inside a category, use stock_snapshot or "
                        "stock_aggregate with the returned department/category ids."
                    ),
                },
            }
            trace = ToolTrace(
                thought=thought,
                tool="stock_scope",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status="policy",
                source_data="prompt_policy -> furniture_capability_summary",
                result_count=int(summary.get("mapped_furniture_category_count", 0)),
            )
            return ToolResult(tool="stock_scope", data=data, llm_content=data, trace=trace)

        async def collect_family_evidence(
            validated: StockProductFamilyInventoryArgs | StockVariantRankArgs,
        ) -> tuple[list[NormalizedEvidence], str, list[str], dict[str, Any]]:
            catalogue_scan = await self.inventory_service.scan_catalogue_with_recovery(
                StockSearchCatalogueArgs(
                    page=validated.page,
                    search=validated.search,
                    departmentId=validated.departmentId,
                    categoryId=validated.categoryId,
                )
            )
            matched_products = self._filter_products_for_query(catalogue_scan.items, validated.search)
            if not matched_products and catalogue_scan.items:
                matched_products = list(catalogue_scan.items)

            recovery_limitations = [
                note
                for note in catalogue_scan.notes
                if "recovery" in note.casefold() or "pagesize" in note.casefold()
            ]

            evidence_items: list[NormalizedEvidence] = []
            cache_statuses = list(catalogue_scan.cache_statuses)
            notes = list(catalogue_scan.notes)
            skipped_variants = 0
            variant_caps = []
            for product in matched_products:
                capped_product, cap_metadata = self.inventory_service.cap_product_variants(product)
                if cap_metadata is not None:
                    variant_caps.append(cap_metadata)
                for variant in capped_product.variants:
                    if not variant.sku:
                        skipped_variants += 1
                        continue
                    evidence, cache_status, extract_notes = await self.inventory_service.extract_variant_evidence(
                        StockExtractVariantEvidenceArgs(sku=variant.sku),
                        matched_on=["catalogue_family", "sku"],
                        confidence=0.98,
                        tool_name="stock_get_product_family_inventory",
                    )
                    evidence_items.append(evidence)
                    cache_statuses.append(cache_status)
                    notes.extend(extract_notes)

            limitations: list[str] = list(recovery_limitations)
            limitations.extend(self.inventory_service.variant_cap_limitations(variant_caps))
            if skipped_variants:
                limitations.append(f"Skipped {skipped_variants} catalogue variant(s) without SKU identifiers.")
            if not evidence_items:
                limitations.append("No variant-level stock evidence was returned for the requested family.")
            coverage = {
                "requestedPage": validated.page,
                "requestedPageSize": self.inventory_service._catalogue_page_size_cap(),
                "matchedProducts": len(matched_products),
                "matchedPages": catalogue_scan.matched_pages,
                "enrichedProducts": len({item.product_id for item in evidence_items if item.product_id}),
                "enrichedVariants": len(evidence_items),
                "isPartial": catalogue_scan.is_partial or bool(limitations),
                "limitations": limitations,
                "variantCaps": [item.model_dump(mode="json") for item in variant_caps],
            }
            return evidence_items, self.inventory_service._combine_cache_statuses(cache_statuses), notes, coverage

        async def product_family_inventory(
            validated: StockProductFamilyInventoryArgs, _: str | None, thought: str
        ) -> ToolResult:
            evidence_items, cache_status, notes, coverage = await collect_family_evidence(validated)
            payload = {
                "query": validated.search,
                "rows": [self.inventory_service._build_inventory_row(item).model_dump(mode="json") for item in evidence_items],
                "evidence": [self._compact_snapshot_evidence(item) for item in evidence_items],
                "coverage": coverage,
                "guidance": " ".join(
                    part
                    for part in [
                        "Treat this as product-family inventory: answer availability across every returned variant/SKU, "
                        "not only the first row.",
                        self.inventory_service.variant_cap_guidance(
                            [VariantCapMetadata.model_validate(item) for item in coverage.get("variantCaps", [])]
                        ),
                    ]
                    if part
                ),
            }
            trace = ToolTrace(
                thought=thought,
                tool="stock_get_product_family_inventory",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> catalogue variants -> variant evidence[*]",
                result_count=len(evidence_items),
                normalization_notes=notes + coverage["limitations"],
            )
            return ToolResult(
                tool="stock_get_product_family_inventory",
                data=payload,
                llm_content=payload,
                normalization_notes=notes + coverage["limitations"],
                trace=trace,
            )

        async def stock_variant_rank(
            validated: StockVariantRankArgs, _: str | None, thought: str
        ) -> ToolResult:
            evidence_items, cache_status, notes, coverage = await collect_family_evidence(validated)
            filtered_evidence, ranked, filter_notes = rank_evidence_with_filters(
                evidence_items,
                metric=validated.metric,
                region=validated.region,
                group_by="variant",
                direction=validated.direction,
                limit=validated.limit,
                attribute_filters=validated.attributeFilters,
            )
            limitations = list(coverage["limitations"]) + filter_notes
            if not ranked:
                limitations.append(
                    "No variant ranking rows were produced. Try a narrower family phrase or resolve the family with stock_search first."
                )
            payload = {
                "query": validated.search,
                "region": validated.region,
                "metric": validated.metric,
                "direction": validated.direction,
                "attributeFilters": [item.model_dump(mode="json") for item in validated.attributeFilters],
                "rows": ranked,
                "coverage": {
                    **coverage,
                    "filteredVariants": len(filtered_evidence),
                    "limitations": limitations,
                    "isPartial": coverage["isPartial"] or bool(limitations),
                },
                "guidance": " ".join(
                    part
                    for part in [
                        "Rows are ranked only at variant grain within the resolved product family/families. Use this tool "
                        "to resolve which variant best matches the requested stock/spec metric after the family is known.",
                        self.inventory_service.variant_cap_guidance(
                            [VariantCapMetadata.model_validate(item) for item in coverage.get("variantCaps", [])]
                        ),
                    ]
                    if part
                ),
            }
            trace = ToolTrace(
                thought=thought,
                tool="stock_variant_rank",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> family variant evidence[*] -> variant-only ranking",
                result_count=len(ranked),
                normalization_notes=notes + limitations,
            )
            return ToolResult(
                tool="stock_variant_rank",
                data=payload,
                llm_content=payload,
                normalization_notes=notes + limitations,
                trace=trace,
            )

        async def count_items(validated: StockCountItemsArgs, _: str | None, thought: str) -> ToolResult:
            cat_args = StockSearchCatalogueArgs(
                page=1,
                search=validated.search,
                departmentId=validated.departmentId,
                categoryId=validated.categoryId,
            )
            data, cache_status, notes = await self.inventory_service.search_catalogue(cat_args)
            result: dict[str, Any] = {
                "product_count": data.totalCount,
                "filters": {
                    "search": validated.search,
                    "departmentId": validated.departmentId,
                    "categoryId": validated.categoryId,
                },
            }
            if validated.countVariants and validated.search:
                full_args = StockSearchCatalogueArgs(
                    page=1,
                    search=validated.search,
                    departmentId=validated.departmentId,
                    categoryId=validated.categoryId,
                )
                full_data, full_cache_status, full_notes = await self.inventory_service.search_catalogue(full_args)
                cache_status = self.inventory_service._combine_cache_statuses([cache_status, full_cache_status])
                notes = notes + full_notes
                matched = self._filter_products_for_query(full_data.items, validated.search)
                if matched:
                    result["variant_count"] = sum(len(p.variants) for p in matched)
                    result["matched_product_names"] = [p.name for p in matched]
                else:
                    result["variant_count"] = None
                    result["matched_product_names"] = []
            result["guidance"] = (
                "product_count is the total catalogue products matching the applied filters. "
                "variant_count (present when countVariants=True and search is provided) is the "
                "total SKU variants across matched product families."
            )
            trace = ToolTrace(
                thought=thought,
                tool="stock_count_items",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> products.totalCount",
                result_count=data.totalCount,
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_count_items",
                data=result,
                llm_content=result,
                normalization_notes=notes,
                trace=trace,
            )

        async def hirable_by_state(validated: StockHirableByStateArgs, _: str | None, thought: str) -> ToolResult:
            family_args = StockProductFamilyInventoryArgs(
                search=validated.search,
                departmentId=validated.departmentId,
                categoryId=validated.categoryId,
            )
            evidence_items, cache_status, notes, coverage = await collect_family_evidence(family_args)
            target_states = validated.states or ["VIC", "NSW", "QLD"]
            state_fields: dict[str, tuple[str, str]] = {
                "VIC": ("vicStock", "vicHirable"),
                "NSW": ("nswStock", "nswHirable"),
                "QLD": ("qldStock", "qldHirable"),
            }
            state_summary: dict[str, Any] = {}
            for state in target_states:
                sf, hf = state_fields[state]
                state_summary[state] = {
                    "total_stock": sum((getattr(e.stock, sf) or 0) for e in evidence_items),
                    "total_hirable": sum((getattr(e.stock, hf) or 0) for e in evidence_items),
                    "variants_with_stock": sum(1 for e in evidence_items if (getattr(e.stock, sf) or 0) > 0),
                }
            variant_breakdown: list[dict[str, Any]] = []
            for ev in evidence_items:
                row: dict[str, Any] = {
                    "product": ev.product_name,
                    "variant": ev.variant_name,
                    "sku": ev.sku,
                    "variationOptions": ev.variation_options,
                    "isActive": ev.isActive,
                    "overall": {"stock": ev.stock.totalStock, "hirable": ev.stock.totalHirable},
                }
                for state in target_states:
                    sf, hf = state_fields[state]
                    row[state] = {"stock": getattr(ev.stock, sf), "hirable": getattr(ev.stock, hf)}
                variant_breakdown.append(row)
            payload: dict[str, Any] = {
                "query": validated.search,
                "states_queried": target_states,
                "state_summary": state_summary,
                "overall": {
                    "total_stock": sum((e.stock.totalStock or 0) for e in evidence_items),
                    "total_hirable": sum((e.stock.totalHirable or 0) for e in evidence_items),
                },
                "variant_breakdown": variant_breakdown,
                "coverage": coverage,
                "guidance": (
                    "state_summary aggregates stock and hirable across every resolved variant per state. "
                    "variant_breakdown shows per-SKU numbers for each state. "
                    "Use this to answer state-level availability questions for a named product or family."
                ),
            }
            trace = ToolTrace(
                thought=thought,
                tool="stock_hirable_by_state",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> catalogue variants -> variant evidence[*] -> state stock fields",
                result_count=len(evidence_items),
                normalization_notes=notes + coverage["limitations"],
            )
            return ToolResult(
                tool="stock_hirable_by_state",
                data=payload,
                llm_content=payload,
                normalization_notes=notes + coverage["limitations"],
                trace=trace,
            )

        if self.inventory_service.settings.local_harmonise:
            # Motivation vs Logic: the cloud Harmonise contract currently exposes
            # product endpoints only, so metadata tools are local-dev only.
            self._register(
                "stock_get_departments",
                (
                    "Retrieve raw department metadata and optional sub-departments for inventory "
                    "narrowing. Do not use for supported-scope counts; stock prompt policy defines the assistant's "
                    "canonical supported departments."
                ),
                StockGetDepartmentsArgs,
                get_departments,
                visible=False,
            )
            self._register(
                "stock_get_categories",
                (
                    "Retrieve raw local Harmonise category metadata pages. Do not use for supported-scope counts; "
                    "stock prompt policy defines the assistant's canonical supported furniture category routes."
                ),
                StockGetCategoriesArgs,
                get_categories,
                visible=False,
            )
        self._register(
            "stock_scope",
            (
                "Supported stock scope and filter IDs. Use for questions about how many departments or categories are "
                "supported, which categoryId maps to a given category, or before filtering inventory by department or "
                "category. Returns the canonical supported departments and mapped category routes with their IDs."
            ),
            StockGetSupportedScopeArgs,
            get_supported_scope,
        )
        self._register(
            "stock_get_supported_scope",
            "Deprecated alias for stock_scope. Hidden from normal MCP discovery.",
            StockGetSupportedScopeArgs,
            get_supported_scope,
            visible=False,
        )
        self._register(
            "stock_search",
            (
                "Harmonise catalogue discovery by product/family text plus supported filters: page, "
                "search, departmentId, categoryId. Use to find product ids/SKUs and variants; for availability of a "
                "named family, follow with stock_snapshot so every variant is covered."
            ),
            StockSearchCatalogueArgs,
            search_catalogue,
        )
        self._register(
            "stock_search_catalogue",
            "Deprecated alias for stock_search. Hidden from normal MCP discovery.",
            StockSearchCatalogueArgs,
            search_catalogue,
            visible=False,
        )
        self._register(
            "stock_detail",
            (
                "Exact product-family or SKU detail. Use when a product id or SKU is already known. Detail includes "
                "variants, dimensions, pricing, image metadata, and VIC/NSW/QLD stock fields; generic family "
                "availability still needs all returned variants summarized."
            ),
            StockGetProductArgs,
            get_product,
        )
        self._register(
            "stock_get_product",
            "Deprecated alias for stock_detail. Hidden from normal MCP discovery.",
            StockGetProductArgs,
            get_product,
            visible=False,
        )
        self._register(
            "stock_extract_variant_evidence",
            (
                "Deprecated exact-variant evidence alias. Hidden from normal MCP discovery; use stock_detail for exact SKU "
                "detail or stock_snapshot for product-family stock questions."
            ),
            StockExtractVariantEvidenceArgs,
            extract_variant,
            visible=False,
        )
        self._register(
            "stock_get_variant_evidence",
            (
                "Deprecated alias for stock_extract_variant_evidence. Direct calls still work, but MCP clients should "
                "prefer stock_extract_variant_evidence."
            ),
            StockExtractVariantEvidenceArgs,
            extract_variant,
            visible=False,
        )
        self._register(
            "stock_compare",
            (
                "Side-by-side comparison of 2-20 already-resolved variant SKUs/identifiers. Use for explicit compare "
                "requests, not broad family availability; for many variants or stock tables use stock_snapshot."
            ),
            StockCompareVariantsArgs,
            compare_variants,
        )
        self._register(
            "stock_compare_variants",
            "Deprecated alias for stock_compare. Hidden from normal MCP discovery.",
            StockCompareVariantsArgs,
            compare_variants,
            visible=False,
        )
        self._register(
            "stock_snapshot",
            (
                "Answer-ready inventory snapshot: enriches catalogue matches into variant rows with size, known specs, "
                "overall stock, and VIC/NSW/QLD availability text. Best default for broad/multi-variant stock, category "
                "inventory, and named-family availability when every variant must be included."
            ),
            StockInventorySnapshotArgs,
            inventory_snapshot,
        )
        self._register(
            "stock_inventory_snapshot",
            "Deprecated alias for stock_snapshot. Hidden from normal MCP discovery.",
            StockInventorySnapshotArgs,
            inventory_snapshot,
            visible=False,
        )
        self._register(
            "stock_aggregate",
            (
                "Grouped stock and hirable totals from a full inventory snapshot. Use for most/least questions by "
                "type, product family, category, or region; product grouping answers broad wording like all inventory "
                "or chair type. This returns summed groups, not single-variant rankings. Use stock_specs_rank "
                "when the question also needs dimensions, pricing, attribute/style filters, or department grouping."
            ),
            StockAggregateArgs,
            aggregate_stock,
        )
        self._register(
            "stock_specs_rank",
            (
                "Rank and filter Harmonise products or variants by stock, hirable availability, physical dimensions, "
                "derived area/volume, replacement cost, hire rates, hierarchy, state, and LLM-supplied aesthetic "
                "attributes. Use for complex stock/spec ranking questions; use stock_image for Harmonise image retrieval and rendering."
            ),
            StockSpecsRankArgs,
            stock_specs_rank,
        )
        self._register(
            "stock_image",
            (
                "Resolve a Harmonise product image from an exact image path, exact SKU, or product-family search, "
                "then return the HTTP image URL plus MCP-native image content when it can be fetched and rendered."
            ),
            StockImageArgs,
            stock_image,
        )
        self._register(
            "stock_get_product_family_inventory",
            (
                "Deprecated alias for stock_snapshot with a required search phrase. Hidden from normal MCP discovery."
            ),
            StockProductFamilyInventoryArgs,
            product_family_inventory,
            visible=False,
        )
        self._register(
            "stock_variant_rank",
            (
                "Rank variants within a named product/family by stock, hirable, dimensions, derived area/volume, or "
                "pricing metrics. Use only for intra-family variant or SKU resolution once the question is about which "
                "specific variant best matches the requested metric."
            ),
            StockVariantRankArgs,
            stock_variant_rank,
        )
        self._register(
            "stock_count_items",
            (
                "Deprecated count helper. Hidden from normal MCP discovery; use stock_scope for supported counts and "
                "stock_aggregate for grouped inventory totals."
            ),
            StockCountItemsArgs,
            count_items,
            visible=False,
        )
        self._register(
            "stock_hirable_by_state",
            (
                "Deprecated per-state family helper. Hidden from normal MCP discovery; use stock_aggregate for grouped "
                "stock or hirable totals by state."
            ),
            StockHirableByStateArgs,
            hirable_by_state,
            visible=False,
        )

    def _catalogue_model_view(self, data: ProductListItemDtoPagedResponse) -> dict[str, Any]:
        return {
            "page": data.page,
            "pageSize": data.pageSize,
            "totalCount": data.totalCount,
            "totalPages": data.totalPages,
            "items": [self._product_catalogue_model_view(item) for item in data.items],
        }

    def _product_catalogue_model_view(self, item: ProductListItemDto) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": item.name,
            "departmentId": item.departmentId,
            "subDepartmentId": item.subDepartmentId,
            "categoryId": item.categoryId,
            "isActive": item.isActive,
            "variationNames": [
                variation.name for variation in item.variations if (variation.name or "").strip()
            ],
            "variantCap": item.variantCap.model_dump(mode="json") if item.variantCap else None,
            "variants": [
                {
                    "id": variant.id,
                    "name": variant.name,
                    "sku": variant.sku,
                    "totalHirable": variant.totalHirable,
                }
                for variant in item.variants
            ],
        }

    def _product_model_view(self, data: ProductListItemDtoPagedResponse) -> dict[str, Any]:
        variant_caps = [item.variantCap for item in data.items if item.variantCap is not None]
        payload = {
            "page": data.page,
            "pageSize": data.pageSize,
            "totalCount": data.totalCount,
            "totalPages": data.totalPages,
            "items": [
                {
                    **self._product_catalogue_model_view(item),
                    "variants": [self._product_variant_model_view(variant) for variant in item.variants],
                }
                for item in data.items
            ],
        }
        guidance = self.inventory_service.variant_cap_guidance(variant_caps) if self.inventory_service else None
        if guidance:
            payload["guidance"] = guidance
        return payload

    def _product_variant_model_view(self, variant) -> dict[str, Any]:
        details = variant.details
        image_file_name = details.imageFileName if details else None
        return {
            "id": variant.id,
            "name": variant.name,
            "sku": variant.sku,
            "totalHirable": variant.totalHirable,
            "details": {
                "isActive": details.isActive if details else None,
                "length": details.length if details else None,
                "width": details.width if details else None,
                "height": details.height if details else None,
                "totalStock": details.totalStock if details else None,
                "vicStock": details.vicStock if details else None,
                "nswStock": details.nswStock if details else None,
                "qldStock": details.qldStock if details else None,
                "salesNote": details.salesNote if details else None,
                "generalRate": details.generalRate if details else None,
                "expoRate": details.expoRate if details else None,
                "cost": details.cost if details else None,
                "dimensional": details.dimensional if details else None,
                "canBeSoldInPortions": details.canBeSoldInPortions if details else None,
                "imageFileName": image_file_name,
                "imageUrl": build_harmonise_image_url(
                    self.inventory_service.settings.cloud_harmonise_image,
                    image_file_name,
                ),
            },
        }

    def _filter_products_for_query(self, products: list[ProductListItemDto], query: str) -> list[ProductListItemDto]:
        # Motivation vs Logic: MCP family helpers must not degrade into broad page
        # scans when a backend ignores text search; local filtering keeps the
        # helper focused on the named family before variant-level fan-out.
        matched: list[ProductListItemDto] = []
        for product in products:
            candidate = " ".join(
                value
                for value in [
                    product.name,
                    " ".join(variant.name or "" for variant in product.variants),
                    " ".join(variant.sku or "" for variant in product.variants),
                ]
                if value
            )
            if lexical_overlap(query, candidate) >= 0.6:
                matched.append(product)
        return matched

    def _evidence_model_view(self, evidence: NormalizedEvidence) -> dict[str, Any]:
        return {
            "product": evidence.product_name,
            "variant": evidence.variant_name,
            "sku": evidence.sku,
            "variationOptions": evidence.variation_options,
            "salesNote": evidence.salesNote,
            "dimensions": evidence.dimensions.model_dump(mode="json"),
            "stock": evidence.stock.model_dump(mode="json"),
            "pricing": evidence.pricing.model_dump(mode="json"),
            "media": evidence.media.model_dump(mode="json"),
            "isActive": evidence.isActive,
            "provenance": evidence.provenance.model_dump(mode="json"),
        }

    def _inventory_snapshot_model_view(self, data: InventorySnapshotResponse) -> dict[str, Any]:
        payload = {
            "rows": [
                self._compact_snapshot_row(
                    row.model_dump(mode="json"),
                    evidence=data.evidence[index] if index < len(data.evidence) else None,
                )
                for index, row in enumerate(data.rows)
            ],
            "coverage": data.coverage.model_dump(mode="json"),
            "evidence": [self._compact_snapshot_evidence(item) for item in data.evidence],
        }
        if data.guidance:
            payload["guidance"] = data.guidance
        return payload

    def _compact_snapshot_row(
        self,
        row: dict[str, Any],
        *,
        evidence: NormalizedEvidence | None,
    ) -> dict[str, Any]:
        compact_row = {
            "product": row.get("product"),
            "variant": row.get("variant"),
            "sku": row.get("sku"),
            "attributeEvidence": self._compact_attribute_evidence(row),
            "size": row.get("size"),
            "stock": row.get("stock"),
            "knownSpecs": self._compact_known_specs(row.get("knownSpecs", [])),
        }
        if evidence is None:
            return compact_row
        compact_row["variationOptions"] = evidence.variation_options
        compact_row["pricing"] = evidence.pricing.model_dump(mode="json")
        compact_row["stockNumbers"] = evidence.stock.model_dump(mode="json")
        return compact_row

    def _compact_snapshot_evidence(self, evidence: NormalizedEvidence) -> dict[str, Any]:
        return {
            "product": evidence.product_name,
            "variant": evidence.variant_name,
            "sku": evidence.sku,
            "variationOptions": evidence.variation_options,
            "dimensions": evidence.dimensions.model_dump(mode="json"),
            "stock": evidence.stock.model_dump(mode="json"),
            "pricing": evidence.pricing.model_dump(mode="json"),
            "salesNote": evidence.salesNote,
            "media": evidence.media.model_dump(mode="json"),
        }

    async def _fetch_mcp_image_content(self, image_url: str) -> tuple[McpImageContent | None, str | None]:
        # Motivation vs Logic: MCP clients can render protocol-native image blocks,
        # while structured JSON still carries imageUrl as a fallback for hosts that
        # do not fetch binary assets from tool responses.
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(image_url)
        except httpx.HTTPError as exc:
            return None, f"Image could not be fetched for MCP rendering ({image_url}): {exc.__class__.__name__}."

        if response.status_code >= 400:
            return None, f"Image could not be fetched for MCP rendering ({image_url}): HTTP {response.status_code}."

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        mime_type = content_type if content_type.startswith("image/") else self._guess_image_mime_type(image_url)
        if not mime_type:
            return None, f"Image response did not include a supported image content type ({image_url})."

        max_bytes = 5 * 1024 * 1024
        if len(response.content) > max_bytes:
            return None, f"Image was too large for inline MCP rendering ({image_url})."

        encoded = base64.b64encode(response.content).decode("ascii")
        return McpImageContent(data=encoded, mimeType=mime_type), None

    def _guess_image_mime_type(self, image_url: str) -> str | None:
        lowered = image_url.split("?", 1)[0].lower()
        if lowered.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        if lowered.endswith(".png"):
            return "image/png"
        if lowered.endswith(".gif"):
            return "image/gif"
        if lowered.endswith(".webp"):
            return "image/webp"
        return None

    def _aggregate_snapshot_evidence(
        self,
        evidence_items: list[NormalizedEvidence],
        *,
        region: str,
        measure: str,
        group_by: str,
        direction: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        stock_fields = {
            "VIC": ("vicStock", "vicHirable"),
            "NSW": ("nswStock", "nswHirable"),
            "QLD": ("qldStock", "qldHirable"),
            "overall": ("totalStock", "totalHirable"),
        }
        stock_field, hirable_field = stock_fields[region]

        groups: dict[str, dict[str, Any]] = {}
        for evidence in evidence_items:
            key, label = self._aggregate_group_key(evidence, group_by)
            if key not in groups:
                groups[key] = {
                    "group": label,
                    "groupBy": group_by,
                    "productIds": set(),
                    "categoryIds": set(),
                    "variantCount": 0,
                    "stock": {"overall": 0, "VIC": 0, "NSW": 0, "QLD": 0},
                    "hirable": {"overall": 0, "VIC": 0, "NSW": 0, "QLD": 0},
                    "missingStockFields": [],
                    "variants": [],
                }
            group = groups[key]
            if evidence.product_id:
                group["productIds"].add(evidence.product_id)
            if evidence.categoryId:
                group["categoryIds"].add(evidence.categoryId)
            group["variantCount"] += 1
            self._add_aggregate_quantity(group["stock"], evidence, "overall", "totalStock", group["missingStockFields"])
            self._add_aggregate_quantity(group["stock"], evidence, "VIC", "vicStock", group["missingStockFields"])
            self._add_aggregate_quantity(group["stock"], evidence, "NSW", "nswStock", group["missingStockFields"])
            self._add_aggregate_quantity(group["stock"], evidence, "QLD", "qldStock", group["missingStockFields"])
            self._add_aggregate_quantity(group["hirable"], evidence, "overall", "totalHirable", group["missingStockFields"])
            self._add_aggregate_quantity(group["hirable"], evidence, "VIC", "vicHirable", group["missingStockFields"])
            self._add_aggregate_quantity(group["hirable"], evidence, "NSW", "nswHirable", group["missingStockFields"])
            self._add_aggregate_quantity(group["hirable"], evidence, "QLD", "qldHirable", group["missingStockFields"])
            group["variants"].append(
                {
                    "product": evidence.product_name,
                    "variant": evidence.variant_name,
                    "sku": evidence.sku,
                    "stock": getattr(evidence.stock, stock_field),
                    "hirable": getattr(evidence.stock, hirable_field),
                }
            )

        reverse = direction == "most"
        ranked_groups = sorted(
            groups.values(),
            key=lambda group: group[measure][region],
            reverse=reverse,
        )
        rows: list[dict[str, Any]] = []
        for rank, group in enumerate(ranked_groups[:limit], start=1):
            rows.append(
                {
                    "rank": rank,
                    "group": group["group"],
                    "groupBy": group["groupBy"],
                    "region": region,
                    "measure": measure,
                    "rankValue": group[measure][region],
                    "stock": group["stock"],
                    "hirable": group["hirable"],
                    "variantCount": group["variantCount"],
                    "productIds": sorted(group["productIds"]),
                    "categoryIds": sorted(group["categoryIds"]),
                    "variants": group["variants"],
                    "missingStockFields": sorted(set(group["missingStockFields"])),
                }
            )
        return rows

    def _aggregate_group_key(self, evidence: NormalizedEvidence, group_by: str) -> tuple[str, str]:
        if group_by == "category":
            label = evidence.categoryId or "Uncategorised"
            return f"category:{label}", label
        if group_by == "variant":
            label = evidence.variant_name or evidence.sku or "Unnamed variant"
            return f"variant:{evidence.variant_id or evidence.sku or label}", label
        label = evidence.product_name or "Unnamed product"
        return f"product:{evidence.product_id or label}", label

    def _add_aggregate_quantity(
        self,
        totals: dict[str, int],
        evidence: NormalizedEvidence,
        label: str,
        stock_field: str,
        missing_fields: list[str],
    ) -> None:
        value = getattr(evidence.stock, stock_field)
        if value is None:
            missing_fields.append(f"{evidence.sku or evidence.variant_name or 'unknown'}:{stock_field}")
            return
        totals[label] += value

    def _compact_attribute_evidence(self, row: dict[str, Any]) -> list[str]:
        product = (row.get("product") or "").strip()
        variant = (row.get("variant") or "").strip()
        compact: list[str] = []
        for value in row.get("attributeEvidence", []):
            normalized = (value or "").strip()
            if not normalized:
                continue
            # Root Cause vs Logic: previously we skipped *both* product and variant
            # when compact was non-empty, so a short label (e.g. option) could cause
            # the full variant name to be dropped; colour then looked "missing".
            if normalized == product and compact:
                continue
            if normalized not in compact:
                compact.append(normalized)
        if not compact and (variant or product):
            for fallback in (variant, product):
                if fallback and fallback not in compact:
                    compact.append(fallback)
        return compact[:2]

    def _compact_known_specs(self, specs: list[str]) -> list[str]:
        compact: list[str] = []
        for spec in specs:
            if spec.startswith("salesNote="):
                compact.append(self._truncate_spec(spec, 96))
                continue
            if spec.startswith("components="):
                component_count = spec.count(",") + 1
                compact.append(f"components={component_count} items")
                continue
            compact.append(spec)
        return compact[:6]

    def _truncate_spec(self, spec: str, limit: int) -> str:
        if len(spec) <= limit:
            return spec
        return spec[: limit - 3].rstrip() + "..."

    def _register_resolver(self) -> None:
        async def disambiguate(validated: ResolverDisambiguateCandidatesArgs, _: str | None, thought: str) -> ToolResult:
            search_args = StockSearchCatalogueArgs(
                page=1,
                search=validated.query,
                departmentId=validated.departmentId,
                categoryId=validated.categoryId,
            )
            search_result, _, notes = await self.inventory_service.search_catalogue(search_args)
            ranked = self.resolver_service.rank_candidates(validated.query, search_result.items, limit=validated.limit)
            ranked_product_ids = {candidate.option.product_id for candidate in ranked if candidate.option.product_id}
            if len(ranked_product_ids) <= 1 and ranked:
                top = ranked[0]
                payload = {
                    "status": "resolved_product_family",
                    "query": validated.query,
                    "product_id": top.option.product_id or top.product.id,
                    "product_name": top.product.name,
                    "variant_count": len(top.product.variants),
                    "candidate_count": len(ranked),
                }
                trace = ToolTrace(
                    thought=thought,
                    tool="stock_disambiguate",
                    args=validated.model_dump(exclude_none=True),
                    status="ok",
                    cache_status="resolver",
                    source_data="harmonise -> ranked_candidates[*]",
                    result_count=len(ranked),
                    normalization_notes=notes,
                )
                return ToolResult(
                    tool="stock_disambiguate",
                    data=payload,
                    llm_content=payload,
                    normalization_notes=notes,
                    trace=trace,
                )

            clarification = self.resolver_service.build_clarification(
                validated.query,
                ranked,
                option_limit=validated.limit,
                total_matches=search_result.totalCount,
                selection_threshold=validated.limit,
            )
            trace = ToolTrace(
                thought=thought,
                tool="stock_disambiguate",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status="resolver",
                source_data="harmonise -> ranked_candidates[*]",
                result_count=len(clarification.options),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock_disambiguate",
                data=clarification.model_dump(mode="json"),
                normalization_notes=notes,
                trace=trace,
            )

        self._register(
            "stock_disambiguate",
            (
                "Rank ambiguous catalogue candidates for a user phrase and return either a resolved product family or "
                "clarification options. Use only when search results could mean several products; if the family is "
                "already known, use stock_snapshot or stock_detail."
            ),
            ResolverDisambiguateCandidatesArgs,
            disambiguate,
        )
        self._register(
            "resolver_disambiguate_candidates",
            "Deprecated alias for stock_disambiguate. Hidden from normal MCP discovery.",
            ResolverDisambiguateCandidatesArgs,
            disambiguate,
            visible=False,
        )

    def _register_session(self) -> None:
        async def get_state(validated: SessionToolArgs, session_id: str | None, thought: str) -> ToolResult:
            state, cache_status = await self.session_store.get_state(session_id or validated.sessionId)
            trace = ToolTrace(
                thought=thought,
                tool="session_state",
                args={"sessionId": session_id or validated.sessionId},
                status="ok",
                cache_status=cache_status,
                source_data="session -> state",
                result_count=1,
            )
            summary = summarize_session_state(state, f"session_state {session_id or validated.sessionId}", mode="compact")
            rendered_summary = render_session_context(
                state,
                f"session_state {session_id or validated.sessionId}",
                mode="compact",
            )
            # Motivation vs Logic: the API response can keep the full structured
            # session state for callers, while the LLM only needs a compact
            # digest here so we do not re-expand the entire memory graph into
            # the next chat-completion prompt.
            summary_payload = {
                "session_id": state.session_id,
                "session_name": state.session_name,
                "session_name_source": state.session_name_source,
                "name_assigned": state.name_assigned,
                "summary": rendered_summary,
            }
            data = {
                "session_id": state.session_id,
                "session_name": state.session_name,
                "session_name_source": state.session_name_source,
                "name_assigned": state.name_assigned,
                "recent_product_names": list(state.recent_product_names),
                "recent_resolved_identifiers": list(state.recent_resolved_identifiers),
                "last_candidate_list": [candidate.model_dump(mode="json") for candidate in state.last_candidate_list[:4]],
                "last_filters": state.last_filters,
                "preferences": state.preferences,
                "plan": summary.get("plan"),
                "memo": summary.get("memo"),
                "conversation": summary.get("conversation"),
                "summary": rendered_summary,
            }
            return ToolResult(tool="session_state", data=data, llm_content=summary_payload, trace=trace)

        async def clear_state(validated: SessionToolArgs, session_id: str | None, thought: str) -> ToolResult:
            state, cache_status = await self.session_store.clear_state(session_id or validated.sessionId)
            trace = ToolTrace(
                thought=thought,
                tool="session_clear_state",
                args={"sessionId": session_id or validated.sessionId},
                status="ok",
                cache_status=cache_status,
                source_data="session -> state",
                result_count=1,
            )
            return ToolResult(tool="session_clear_state", data=state.model_dump(mode="json"), trace=trace)

        self._register(
            "session_state",
            (
                "Inspect MCP session working memory: recent products, identifiers, compact plan, and memo digest. "
                "Use only when the user explicitly asks about prior context/history; do not use for fresh stock availability."
            ),
            SessionToolArgs,
            get_state,
        )
        self._register(
            "session_get_state",
            "Deprecated alias for session_state. Hidden from normal MCP discovery.",
            SessionToolArgs,
            get_state,
            visible=False,
        )
        self._register(
            "session_clear_state",
            "Administrative/debug tool to clear MCP session working memory. Hidden from normal MCP tool discovery.",
            SessionToolArgs,
            clear_state,
            visible=False,
        )

    def _register_news(self) -> None:
        async def search(validated: NewsSearchArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.news_service.search(validated)
            trace = ToolTrace(
                thought=thought,
                tool="news_search",
                args=validated.model_dump(by_alias=True, exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="newsapi -> articles[*]",
                result_count=len(data.get("articles", [])),
                normalization_notes=notes,
            )
            # Motivation vs Logic: surface concise article summaries so the agent can cite trends directly.
            formatted = format_news_articles(
                data,
                validated.model_dump(by_alias=True, exclude_none=True),
                request_type="search",
            )
            return ToolResult(
                tool="news_search",
                data=data,
                llm_content=formatted,
                normalization_notes=notes,
                trace=trace,
            )

        async def headlines(validated: NewsHeadlinesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.news_service.headlines(validated)
            trace = ToolTrace(
                thought=thought,
                tool="news_headlines",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="newsapi -> headlines[*]",
                result_count=len(data.get("articles", [])),
                normalization_notes=notes,
            )
            formatted = format_news_articles(
                data,
                validated.model_dump(exclude_none=True),
                request_type="headlines",
            )
            return ToolResult(
                tool="news_headlines",
                data=data,
                llm_content=formatted,
                normalization_notes=notes,
                trace=trace,
            )

        async def sources(validated: NewsSourcesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.news_service.sources(validated)
            trace = ToolTrace(
                thought=thought,
                tool="news_sources",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="newsapi -> sources[*]",
                result_count=len(data.get("sources", [])),
                normalization_notes=notes,
            )
            formatted = format_news_sources(data, validated.model_dump(exclude_none=True))
            return ToolResult(
                tool="news_sources",
                data=data,
                llm_content=formatted,
                normalization_notes=notes,
                trace=trace,
            )

        self._register(
            "news_search",
            (
                "News API article search for external news questions. Supports keywords, source IDs, domains, ISO date "
                "bounds, language, sort, and pagination. Do not use for inventory or stock questions."
            ),
            NewsSearchArgs,
            search,
        )
        self._register(
            "news_headlines",
            (
                "Top headlines from News API by country, category, source, or keyword. Use for current news/headline "
                "questions, not Harmonise inventory."
            ),
            NewsHeadlinesArgs,
            headlines,
        )
        self._register(
            "news_sources",
            "List News API source IDs by category, language, or country so later news_search/headlines calls can use exact sources.",
            NewsSourcesArgs,
            sources,
        )

    def _register_weather(self) -> None:
        async def resolve(validated: WeatherResolveArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.resolve(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather_resolve",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> locations[*]",
                result_count=data.get("count"),
                normalization_notes=notes,
            )
            return ToolResult(tool="weather_resolve", data=data, normalization_notes=notes, trace=trace)

        async def current(validated: WeatherCurrentArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.current(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather_current",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> current",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="weather_current", data=data, normalization_notes=notes, trace=trace)

        async def forecast(validated: WeatherForecastArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.forecast(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather_forecast",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> forecast.list[*]",
                result_count=data.get("returned"),
                normalization_notes=notes,
            )
            return ToolResult(tool="weather_forecast", data=data, normalization_notes=notes, trace=trace)

        async def history(validated: WeatherHistoryArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.history(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather_history",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> history.points[*]",
                result_count=data.get("count"),
                normalization_notes=notes,
            )
            return ToolResult(tool="weather_history", data=data, normalization_notes=notes, trace=trace)

        self._register(
            "weather_resolve",
            "OpenWeather geocoding for weather questions: resolve a place name or lat/lon into candidate locations.",
            WeatherResolveArgs,
            resolve,
            visible=False,
        )
        self._register(
            "weather_current",
            "OpenWeather current conditions for a place name or coordinates. Use for weather, not inventory availability.",
            WeatherCurrentArgs,
            current,
        )
        self._register(
            "weather_forecast",
            "OpenWeather 5-day / 3-hour forecast for a place name or coordinates, with bounded forecast point count.",
            WeatherForecastArgs,
            forecast,
        )
        self._register(
            "weather_history",
            "OpenWeather historical conditions when the configured endpoint supports the requested date/window.",
            WeatherHistoryArgs,
            history,
        )

    def _register_currency(self) -> None:
        async def symbols(validated: CurrencySymbolsArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.symbols(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_symbols",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> symbols",
                result_count=len(data.get("symbols", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_symbols", data=data, normalization_notes=notes, trace=trace)

        async def latest(validated: CurrencyLatestArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.latest(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_latest",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> latest.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_latest", data=data, normalization_notes=notes, trace=trace)

        async def history(validated: CurrencyHistoryArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.history(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_history",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> historical.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_history", data=data, normalization_notes=notes, trace=trace)

        async def timeseries(validated: CurrencyTimeseriesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.timeseries(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_series",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> timeseries.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_series", data=data, normalization_notes=notes, trace=trace)

        async def convert(validated: CurrencyConvertArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.convert(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_convert",
                args=validated.model_dump(by_alias=True, exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> conversion",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_convert", data=data, normalization_notes=notes, trace=trace)

        async def fluctuation(validated: CurrencyFluctuationArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.fluctuation(validated)
            trace = ToolTrace(
                thought=thought,
                tool="fx_fluctuation",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> fluctuation.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="fx_fluctuation", data=data, normalization_notes=notes, trace=trace)

        self._register(
            "fx_symbols",
            "Exchange Rates API supported currency codes. Use before FX lookups when the user gives unclear currency names.",
            CurrencySymbolsArgs,
            symbols,
        )
        self._register(
            "currency_symbols",
            "Deprecated alias for fx_symbols. Hidden from normal MCP discovery.",
            CurrencySymbolsArgs,
            symbols,
            visible=False,
        )
        self._register(
            "fx_latest",
            "Latest FX rates for an optional base and comma-separated target symbols.",
            CurrencyLatestArgs,
            latest,
        )
        self._register(
            "currency_latest",
            "Deprecated alias for fx_latest. Hidden from normal MCP discovery.",
            CurrencyLatestArgs,
            latest,
            visible=False,
        )
        self._register(
            "fx_history",
            "Historical FX rates for one YYYY-MM-DD date, optional base, and optional comma-separated target symbols.",
            CurrencyHistoryArgs,
            history,
        )
        self._register(
            "currency_history",
            "Deprecated alias for fx_history. Hidden from normal MCP discovery.",
            CurrencyHistoryArgs,
            history,
            visible=False,
        )
        self._register(
            "fx_series",
            "Daily FX rate series between start_date and end_date for optional base/target symbols.",
            CurrencyTimeseriesArgs,
            timeseries,
        )
        self._register(
            "currency_timeseries",
            "Deprecated alias for fx_series. Hidden from normal MCP discovery.",
            CurrencyTimeseriesArgs,
            timeseries,
            visible=False,
        )
        self._register(
            "fx_convert",
            "Convert a positive amount from one currency to another, optionally as of a YYYY-MM-DD date.",
            CurrencyConvertArgs,
            convert,
        )
        self._register(
            "currency_convert",
            "Deprecated alias for fx_convert. Hidden from normal MCP discovery.",
            CurrencyConvertArgs,
            convert,
            visible=False,
        )
        self._register(
            "fx_fluctuation",
            "FX rate fluctuation over a YYYY-MM-DD date range, comparing start and end rates.",
            CurrencyFluctuationArgs,
            fluctuation,
        )
        self._register(
            "currency_fluctuation",
            "Deprecated alias for fx_fluctuation. Hidden from normal MCP discovery.",
            CurrencyFluctuationArgs,
            fluctuation,
            visible=False,
        )
