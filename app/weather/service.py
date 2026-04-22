from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings
from app.errors import UpstreamServiceError
from app.store import AppKeyValueStore
from app.weather.model import WeatherCurrentArgs, WeatherForecastArgs, WeatherHistoryArgs, WeatherResolveArgs


class WeatherService:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger
        self._client = httpx.AsyncClient(base_url="https://api.openweathermap.org", timeout=30)
        self._history = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.aclose()
        await self._history.aclose()

    async def resolve(self, args: WeatherResolveArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "weather.resolve",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._resolve_payload(args),
        )

    async def current(self, args: WeatherCurrentArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "weather.current",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._current_payload(args),
        )

    async def forecast(self, args: WeatherForecastArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "weather.forecast",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._forecast_payload(args),
        )

    async def history(self, args: WeatherHistoryArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "weather.history",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._history_payload(args),
        )

    async def _cached(self, namespace: str, payload: dict[str, object], loader) -> tuple[dict[str, object], str, list[str]]:
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"{namespace}:{json.dumps(payload, sort_keys=True, default=str)}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=loader,
        )
        return raw, cache_status, notes

    async def _resolve_payload(self, args: WeatherResolveArgs) -> tuple[dict[str, object], list[str]]:
        if args.query:
            payload = await self._geocode(args.query, args.limit)
            return {"locations": payload, "count": len(payload)}, []
        payload = await self._reverse(args.lat, args.lon, args.limit)
        return {"locations": payload, "count": len(payload)}, []

    async def _current_payload(self, args: WeatherCurrentArgs) -> tuple[dict[str, object], list[str]]:
        location = await self._resolve_one(args)
        raw = await self._get(
            "/data/2.5/weather",
            {
                "lat": location["lat"],
                "lon": location["lon"],
                **self._weather_params(args.units, args.lang),
            },
        )
        return {"location": location, "weather": raw}, []

    async def _forecast_payload(self, args: WeatherForecastArgs) -> tuple[dict[str, object], list[str]]:
        location = await self._resolve_one(args)
        raw = await self._get(
            "/data/2.5/forecast",
            {
                "lat": location["lat"],
                "lon": location["lon"],
                **self._weather_params(args.units, args.lang),
            },
        )
        trimmed = dict(raw)
        trimmed["list"] = raw.get("list", [])[: args.count]
        return {"location": location, "forecast": trimmed, "returned": len(trimmed["list"])}, []

    async def _history_payload(self, args: WeatherHistoryArgs) -> tuple[dict[str, object], list[str]]:
        location = await self._resolve_one(args)
        timestamps = self._history_points(args)
        notes: list[str] = []
        if len(timestamps) > 7:
            timestamps = timestamps[:7]
            notes.append("Weather history requests are capped to 7 timestamps per tool call.")
        points = []
        for timestamp in timestamps:
            points.append(
                {
                    "timestamp": timestamp,
                    "data": await self._history_call(
                        lat=location["lat"],
                        lon=location["lon"],
                        timestamp=timestamp,
                        only_current=args.onlyCurrent,
                        units=args.units,
                        lang=args.lang,
                    ),
                }
            )
        return {"location": location, "points": points, "count": len(points)}, notes

    async def _resolve_one(self, args: WeatherResolveArgs) -> dict[str, object]:
        if args.query:
            matches = await self._geocode(args.query, max(1, args.limit))
            if not matches:
                raise UpstreamServiceError(404, f"No OpenWeather geocoding match was found for '{args.query}'.")
            return matches[0]
        return {"lat": args.lat, "lon": args.lon}

    async def _geocode(self, query: str, limit: int) -> list[dict[str, object]]:
        return await self._get("/geo/1.0/direct", {"q": query, "limit": limit})

    async def _reverse(self, lat: float | None, lon: float | None, limit: int) -> list[dict[str, object]]:
        return await self._get("/geo/1.0/reverse", {"lat": lat, "lon": lon, "limit": limit})

    async def _get(self, path: str, params: dict[str, object]) -> object:
        if not self.settings.open_weather_api_key:
            raise UpstreamServiceError(503, "OPEN_WEATHER_API is not configured in the environment.")
        response = await self._client.get(path, params={"appid": self.settings.open_weather_api_key, **params})
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text)
        return response.json()

    async def _history_call(
        self,
        lat: float,
        lon: float,
        timestamp: int,
        only_current: bool,
        units: str | None,
        lang: str | None,
    ) -> dict[str, object]:
        params = {
            "lat": lat,
            "lon": lon,
            "dt": timestamp,
            "appid": self.settings.open_weather_api_key,
        }
        if only_current:
            params["only_current"] = "true"
        if units:
            params["units"] = units
        if lang:
            params["lang"] = lang

        urls = [
            "https://history.openweathermap.org/data/3.0/history/timemachine",
            "https://api.openweathermap.org/data/2.5/onecall/timemachine",
        ]
        last_error: UpstreamServiceError | None = None
        for url in urls:
            response = await self._history.get(url, params=params)
            if response.status_code < 400:
                return response.json()
            last_error = UpstreamServiceError(response.status_code, response.text)
        assert last_error is not None
        raise last_error

    def _history_points(self, args: WeatherHistoryArgs) -> list[int]:
        if args.dt is not None:
            return [args.dt]
        if args.date:
            return [self._to_unix(args.date)]
        if args.start and args.end:
            current = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
            stop = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
            points: list[int] = []
            while current <= stop:
                points.append(int(current.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()))
                current += timedelta(days=1)
            return points
        if args.start:
            return [self._to_unix(args.start)]
        if args.end:
            return [self._to_unix(args.end)]
        return []

    def _to_unix(self, value: str) -> int:
        return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc, hour=12, minute=0, second=0, microsecond=0).timestamp())

    def _weather_params(self, units: str | None, lang: str | None) -> dict[str, object]:
        params: dict[str, object] = {}
        if units:
            params["units"] = units
        if lang:
            params["lang"] = lang
        return params
