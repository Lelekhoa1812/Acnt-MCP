from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.currency import (
    CurrencyConvertArgs,
    CurrencyFluctuationArgs,
    CurrencyHistoryArgs,
    CurrencyLatestArgs,
    CurrencyService,
    CurrencySymbolsArgs,
    CurrencyTimeseriesArgs,
)
from app.errors import ParameterMappingError, UnsupportedToolError
from app.inventory.service import InventoryService
from app.news import NewsHeadlinesArgs, NewsSearchArgs, NewsService, NewsSourcesArgs
from app.resolver import ResolverService
from app.schemas import (
    ResolverDisambiguateCandidatesArgs,
    SessionToolArgs,
    StockCompareVariantsArgs,
    StockExtractVariantEvidenceArgs,
    StockGetCategoriesArgs,
    StockGetDepartmentsArgs,
    StockGetProductArgs,
    StockSearchCatalogueArgs,
    ToolDefinition,
    ToolResult,
    ToolTrace,
)
from app.session.store import SessionStore
from app.weather import WeatherCurrentArgs, WeatherForecastArgs, WeatherHistoryArgs, WeatherResolveArgs, WeatherService


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
                source_data="harmonise -> product-catalogue.items[*]",
                result_count=len(data.items),
                normalization_notes=notes,
            )
            return ToolResult(
                tool="stock.search_catalogue",
                data=data.model_dump(mode="json"),
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
                normalization_notes=notes,
                trace=trace,
            )

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
            "Normalize a Harmonise product variant into answer-ready evidence. Prefer sku or product id; variantId alone is not enough.",
            StockExtractVariantEvidenceArgs,
            extract_variant,
        )
        self._register(
            "stock.get_variant_evidence",
            "Alias for stock.extract_variant_evidence for proposal compatibility. Prefer sku or product id; variantId alone is not enough.",
            StockExtractVariantEvidenceArgs,
            extract_variant,
        )
        self._register(
            "stock.compare_variants",
            "Compare multiple resolved Harmonise variants side by side.",
            StockCompareVariantsArgs,
            compare_variants,
        )

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
            clarification = self.resolver_service.build_clarification(validated.query, ranked, limit=validated.limit)
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
            "Rank likely catalogue candidates and build a clarification payload when the request is ambiguous.",
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
            return ToolResult(tool="session.get_state", data=state.model_dump(mode="json"), trace=trace)

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
            return ToolResult(tool="news.search", data=data, normalization_notes=notes, trace=trace)

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
            return ToolResult(tool="news.headlines", data=data, normalization_notes=notes, trace=trace)

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
            return ToolResult(tool="news.sources", data=data, normalization_notes=notes, trace=trace)

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
