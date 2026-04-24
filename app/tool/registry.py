from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

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
from app.tool.stock.media import build_harmonise_image_url
from app.tool.stock.service import InventoryService
from app.tool.news import NewsHeadlinesArgs, NewsSearchArgs, NewsService, NewsSourcesArgs
from app.tool.news.formatter import format_news_articles, format_news_sources
from app.resolver import ResolverService
from app.schemas import (
    InventorySnapshotResponse,
    NormalizedEvidence,
    ProductListItemDto,
    ProductListItemDtoPagedResponse,
    ResolverDisambiguateCandidatesArgs,
    SessionToolArgs,
    StockCompareVariantsArgs,
    StockExtractVariantEvidenceArgs,
    StockGetCategoriesArgs,
    StockGetDepartmentsArgs,
    StockGetProductArgs,
    StockInventorySnapshotArgs,
    StockSearchCatalogueArgs,
    ToolDefinition,
    ToolResult,
    ToolTrace,
)
from app.prompt.context import render_session_context, summarize_session_state
from app.session.store import SessionStore
from app.tool.weather import WeatherCurrentArgs, WeatherForecastArgs, WeatherHistoryArgs, WeatherResolveArgs, WeatherService


@dataclass
class ToolSpec:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[BaseModel, str | None, str], Awaitable[ToolResult]]


class ToolRegistry:
    def __init__(
        self,
        inventory_service: InventoryService,
        resolver_service: ResolverService,
        session_store: SessionStore,
        news_service: NewsService,
        weather_service: WeatherService,
        currency_service: CurrencyService,
        logger: logging.Logger,
    ) -> None:
        self.inventory_service = inventory_service
        self.resolver_service = resolver_service
        self.session_store = session_store
        self.news_service = news_service
        self.weather_service = weather_service
        self.currency_service = currency_service
        self.logger = logger
        self._tools: dict[str, ToolSpec] = {}
        self._register_stock()
        self._register_resolver()
        self._register_session()
        self._register_news()
        self._register_weather()
        self._register_currency()

    def list_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=spec.name,
                description=spec.description,
                input_schema=spec.model.model_json_schema(),
            )
            for spec in self._tools.values()
        ]

    def tool_payloads(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.model.model_json_schema(),
                },
            }
            for spec in self._tools.values()
        ]

    async def call_tool(
        self,
        tool_name: str,
        raw_args: dict[str, Any],
        session_id: str | None = None,
        thought: str = "",
    ) -> ToolResult:
        self.logger.debug("tool_call tool=%s args=%s session_id=%s", tool_name, raw_args, session_id)
        spec = self._tools.get(tool_name)
        if spec is None:
            raise UnsupportedToolError(f"Unsupported tool '{tool_name}'.")
        try:
            validated = spec.model.model_validate(raw_args)
        except ValidationError as exc:
            raise ParameterMappingError(self._format_validation_error(tool_name, exc)) from exc
        return await spec.handler(validated, session_id, thought)

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

    def _register(self, name: str, description: str, model: type[BaseModel], handler) -> None:
        self._tools[name] = ToolSpec(name=name, description=description, model=model, handler=handler)

    def _register_stock(self) -> None:
        async def get_departments(validated: StockGetDepartmentsArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.get_departments(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock.get_departments",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> departments[*]",
                result_count=len(data),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock.get_departments",
                data=[item.model_dump(mode="json") for item in data],
                normalization_notes=notes,
                trace=trace,
            )

        async def get_categories(validated: StockGetCategoriesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.get_categories(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock.get_categories",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> categories.items[*]",
                result_count=len(data.items),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock.get_categories",
                data=data.model_dump(mode="json"),
                normalization_notes=notes,
                trace=trace,
            )

        async def search_catalogue(validated: StockSearchCatalogueArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.search_catalogue(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock.search_catalogue",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> products.items[*]",
                result_count=len(data.items),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock.search_catalogue",
                data=data.model_dump(mode="json"),
                llm_content=self._catalogue_model_view(data),
                normalization_notes=notes,
                trace=trace,
            )

        async def get_product(validated: StockGetProductArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.get_product(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock.get_product",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> products.items[*]",
                result_count=len(data.items),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock.get_product",
                data=data.model_dump(mode="json"),
                llm_content=self._product_model_view(data),
                normalization_notes=notes,
                trace=trace,
            )

        async def extract_variant(validated: StockExtractVariantEvidenceArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.extract_variant_evidence(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock.extract_variant_evidence",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data=f"harmonise -> {data.provenance.source_path}",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock.extract_variant_evidence",
                data=data.model_dump(mode="json"),
                llm_content=self._evidence_model_view(data),
                normalization_notes=notes,
                trace=trace,
            )

        async def compare_variants(validated: StockCompareVariantsArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.compare_variants(validated.identifiers)
            trace = ToolTrace(
                thought=thought,
                tool="stock.compare_variants",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> normalized variant evidence[*]",
                result_count=len(data),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock.compare_variants",
                data=[item.model_dump(mode="json") for item in data],
                llm_content=[self._evidence_model_view(item) for item in data],
                normalization_notes=notes,
                trace=trace,
            )

        async def inventory_snapshot(validated: StockInventorySnapshotArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.inventory_service.inventory_snapshot(validated)
            trace = ToolTrace(
                thought=thought,
                tool="stock.inventory_snapshot",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="harmonise -> inventory_snapshot.rows[*]",
                result_count=len(data.rows),
                normalization_notes=notes + data.coverage.limitations,
            )
            return ToolResult(
                tool="stock.inventory_snapshot",
                data=data.model_dump(mode="json"),
                llm_content=self._inventory_snapshot_model_view(data),
                normalization_notes=notes + data.coverage.limitations,
                trace=trace,
            )

        if self.inventory_service.settings.local_harmonise:
            # Motivation vs Logic: the cloud Harmonise contract currently exposes
            # product endpoints only, so metadata tools are local-dev only.
            self._register(
                "stock.get_departments",
                "Retrieve department metadata and optional sub-departments for inventory narrowing.",
                StockGetDepartmentsArgs,
                get_departments,
            )
            self._register(
                "stock.get_categories",
                "Retrieve paged category metadata from Harmonise.",
                StockGetCategoriesArgs,
                get_categories,
            )
        self._register(
            "stock.search_catalogue",
            "Search the Harmonise product catalogue with search text and supported filters.",
            StockSearchCatalogueArgs,
            search_catalogue,
        )
        self._register(
            "stock.get_product",
            "Retrieve exact Harmonise product records by id or sku.",
            StockGetProductArgs,
            get_product,
        )
        self._register(
            "stock.extract_variant_evidence",
            "Normalize one specific Harmonise variant into answer-ready evidence. Use for variant-targeted requests; product-family requests should use stock.get_product or stock.inventory_snapshot.",
            StockExtractVariantEvidenceArgs,
            extract_variant,
        )
        self._register(
            "stock.get_variant_evidence",
            "Alias for stock.extract_variant_evidence for proposal compatibility. Requires variant-specific lookup context (sku or product id alongside variantId).",
            StockExtractVariantEvidenceArgs,
            extract_variant,
        )
        self._register(
            "stock.compare_variants",
            "Compare multiple resolved Harmonise variants side by side.",
            StockCompareVariantsArgs,
            compare_variants,
        )
        self._register(
            "stock.inventory_snapshot",
            "Retrieve a compact, answer-ready inventory evidence snapshot for a catalogue page, including variant-level specs and stock summaries.",
            StockInventorySnapshotArgs,
            inventory_snapshot,
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
        return {
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
        return {
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
                pageSize=50,
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
                    tool="resolver.disambiguate_candidates",
                    args=validated.model_dump(exclude_none=True),
                    status="ok",
                    cache_status="resolver",
                    source_data="harmonise -> ranked_candidates[*]",
                    result_count=len(ranked),
                    normalization_notes=notes,
                )
                return ToolResult(
                    tool="resolver.disambiguate_candidates",
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
                tool="resolver.disambiguate_candidates",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status="resolver",
                source_data="harmonise -> ranked_candidates[*]",
                result_count=len(clarification.options),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="resolver.disambiguate_candidates",
                data=clarification.model_dump(mode="json"),
                normalization_notes=notes,
                trace=trace,
            )

        self._register(
            "resolver.disambiguate_candidates",
            "Use only when catalogue search could mean multiple distinct products. Ranks candidates and returns a user-facing disambiguation prompt. Do not use when you already have a confirmed product and only need variant-level details—use `stock.get_product` or `stock.inventory_snapshot` instead.",
            ResolverDisambiguateCandidatesArgs,
            disambiguate,
        )

    def _register_session(self) -> None:
        async def get_state(validated: SessionToolArgs, session_id: str | None, thought: str) -> ToolResult:
            state, cache_status = await self.session_store.get_state(session_id or validated.sessionId)
            trace = ToolTrace(
                thought=thought,
                tool="session.get_state",
                args={"sessionId": session_id or validated.sessionId},
                status="ok",
                cache_status=cache_status,
                source_data="session -> state",
                result_count=1,
            )
            summary = summarize_session_state(state, f"session.get_state {session_id or validated.sessionId}", mode="compact")
            rendered_summary = render_session_context(
                state,
                f"session.get_state {session_id or validated.sessionId}",
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
            return ToolResult(tool="session.get_state", data=data, llm_content=summary_payload, trace=trace)

        async def clear_state(validated: SessionToolArgs, session_id: str | None, thought: str) -> ToolResult:
            state, cache_status = await self.session_store.clear_state(session_id or validated.sessionId)
            trace = ToolTrace(
                thought=thought,
                tool="session.clear_state",
                args={"sessionId": session_id or validated.sessionId},
                status="ok",
                cache_status=cache_status,
                source_data="session -> state",
                result_count=1,
            )
            return ToolResult(tool="session.clear_state", data=state.model_dump(mode="json"), trace=trace)

        self._register(
            "session.get_state",
            "Return the current working-memory state for the active session.",
            SessionToolArgs,
            get_state,
        )
        self._register(
            "session.clear_state",
            "Reset the current working-memory state for the active session.",
            SessionToolArgs,
            clear_state,
        )

    def _register_news(self) -> None:
        async def search(validated: NewsSearchArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.news_service.search(validated)
            trace = ToolTrace(
                thought=thought,
                tool="news.search",
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
                tool="news.search",
                data=data,
                llm_content=formatted,
                normalization_notes=notes,
                trace=trace,
            )

        async def headlines(validated: NewsHeadlinesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.news_service.headlines(validated)
            trace = ToolTrace(
                thought=thought,
                tool="news.headlines",
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
                tool="news.headlines",
                data=data,
                llm_content=formatted,
                normalization_notes=notes,
                trace=trace,
            )

        async def sources(validated: NewsSourcesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.news_service.sources(validated)
            trace = ToolTrace(
                thought=thought,
                tool="news.sources",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="newsapi -> sources[*]",
                result_count=len(data.get("sources", [])),
                normalization_notes=notes,
            )
            formatted = format_news_sources(data, validated.model_dump(exclude_none=True))
            return ToolResult(
                tool="news.sources",
                data=data,
                llm_content=formatted,
                normalization_notes=notes,
                trace=trace,
            )

        self._register(
            "news.search",
            "Search News API's article index with keywords, sources, domains, dates, and sorting controls.",
            NewsSearchArgs,
            search,
        )
        self._register(
            "news.headlines",
            "Retrieve live top headlines from News API by country, category, source, or keyword.",
            NewsHeadlinesArgs,
            headlines,
        )
        self._register(
            "news.sources",
            "List News API sources filtered by category, language, or country.",
            NewsSourcesArgs,
            sources,
        )

    def _register_weather(self) -> None:
        async def resolve(validated: WeatherResolveArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.resolve(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather.resolve",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> locations[*]",
                result_count=data.get("count"),
                normalization_notes=notes,
            )
            return ToolResult(tool="weather.resolve", data=data, normalization_notes=notes, trace=trace)

        async def current(validated: WeatherCurrentArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.current(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather.current",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> current",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="weather.current", data=data, normalization_notes=notes, trace=trace)

        async def forecast(validated: WeatherForecastArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.forecast(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather.forecast",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> forecast.list[*]",
                result_count=data.get("returned"),
                normalization_notes=notes,
            )
            return ToolResult(tool="weather.forecast", data=data, normalization_notes=notes, trace=trace)

        async def history(validated: WeatherHistoryArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.weather_service.history(validated)
            trace = ToolTrace(
                thought=thought,
                tool="weather.history",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="openweather -> history.points[*]",
                result_count=data.get("count"),
                normalization_notes=notes,
            )
            return ToolResult(tool="weather.history", data=data, normalization_notes=notes, trace=trace)

        self._register(
            "weather.resolve",
            "Resolve a location through OpenWeather geocoding by place query or lat/lon.",
            WeatherResolveArgs,
            resolve,
        )
        self._register(
            "weather.current",
            "Retrieve OpenWeather current conditions for a dynamic location.",
            WeatherCurrentArgs,
            current,
        )
        self._register(
            "weather.forecast",
            "Retrieve OpenWeather 5-day / 3-hour forecast data for a dynamic location.",
            WeatherForecastArgs,
            forecast,
        )
        self._register(
            "weather.history",
            "Retrieve OpenWeather historical weather snapshots for one or more dates when the vendor plan allows it.",
            WeatherHistoryArgs,
            history,
        )

    def _register_currency(self) -> None:
        async def symbols(validated: CurrencySymbolsArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.symbols(validated)
            trace = ToolTrace(
                thought=thought,
                tool="currency.symbols",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> symbols",
                result_count=len(data.get("symbols", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="currency.symbols", data=data, normalization_notes=notes, trace=trace)

        async def latest(validated: CurrencyLatestArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.latest(validated)
            trace = ToolTrace(
                thought=thought,
                tool="currency.latest",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> latest.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="currency.latest", data=data, normalization_notes=notes, trace=trace)

        async def history(validated: CurrencyHistoryArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.history(validated)
            trace = ToolTrace(
                thought=thought,
                tool="currency.history",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> historical.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="currency.history", data=data, normalization_notes=notes, trace=trace)

        async def timeseries(validated: CurrencyTimeseriesArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.timeseries(validated)
            trace = ToolTrace(
                thought=thought,
                tool="currency.timeseries",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> timeseries.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="currency.timeseries", data=data, normalization_notes=notes, trace=trace)

        async def convert(validated: CurrencyConvertArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.convert(validated)
            trace = ToolTrace(
                thought=thought,
                tool="currency.convert",
                args=validated.model_dump(by_alias=True, exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> conversion",
                result_count=1,
                normalization_notes=notes,
            )
            return ToolResult(tool="currency.convert", data=data, normalization_notes=notes, trace=trace)

        async def fluctuation(validated: CurrencyFluctuationArgs, _: str | None, thought: str) -> ToolResult:
            data, cache_status, notes = await self.currency_service.fluctuation(validated)
            trace = ToolTrace(
                thought=thought,
                tool="currency.fluctuation",
                args=validated.model_dump(exclude_none=True),
                status="ok",
                cache_status=cache_status,
                source_data="exchangeratesapi -> fluctuation.rates",
                result_count=len(data.get("rates", {})),
                normalization_notes=notes,
            )
            return ToolResult(tool="currency.fluctuation", data=data, normalization_notes=notes, trace=trace)

        self._register(
            "currency.symbols",
            "List supported currency symbols from Exchange Rates API.",
            CurrencySymbolsArgs,
            symbols,
        )
        self._register(
            "currency.latest",
            "Retrieve the latest exchange rates for a base currency and optional symbol targets.",
            CurrencyLatestArgs,
            latest,
        )
        self._register(
            "currency.history",
            "Retrieve historical exchange rates for a specific date.",
            CurrencyHistoryArgs,
            history,
        )
        self._register(
            "currency.timeseries",
            "Retrieve daily historical exchange-rate series between two dates.",
            CurrencyTimeseriesArgs,
            timeseries,
        )
        self._register(
            "currency.convert",
            "Convert an amount between currencies, optionally on a historical date.",
            CurrencyConvertArgs,
            convert,
        )
        self._register(
            "currency.fluctuation",
            "Retrieve currency fluctuation data between two dates.",
            CurrencyFluctuationArgs,
            fluctuation,
        )
