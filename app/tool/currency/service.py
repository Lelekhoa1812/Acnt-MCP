from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose

import httpx

from app.config import Settings, UpstreamServiceError
from app.tool.currency.model import (
    CurrencyConvertArgs,
    CurrencyFluctuationArgs,
    CurrencyHistoryArgs,
    CurrencyLatestArgs,
    CurrencySymbolsArgs,
    CurrencyTimeseriesArgs,
)
from app.store import AppKeyValueStore


@dataclass
class CurrencyProviderError(Exception):
    provider: str
    status_code: int
    message: str
    code: str | None = None
    payload: dict[str, object] | None = None

    def __str__(self) -> str:
        return self.message


class CurrencyService:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger
        self._primary = httpx.AsyncClient(base_url="https://api.exchangeratesapi.io/v1", timeout=30)
        self._fallback = httpx.AsyncClient(base_url="https://api.frankfurter.dev/v1", timeout=30)

    async def close(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()

    async def symbols(self, args: CurrencySymbolsArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("currency.symbols", args.model_dump(mode="json"), self._symbols_payload)

    async def latest(self, args: CurrencyLatestArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "currency.latest",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._snapshot_payload(date_value=None, base=args.base, symbols=args.symbols),
        )

    async def history(self, args: CurrencyHistoryArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "currency.history",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._snapshot_payload(date_value=args.date, base=args.base, symbols=args.symbols),
        )

    async def timeseries(self, args: CurrencyTimeseriesArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "currency.timeseries",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._timeseries_payload(args),
        )

    async def convert(self, args: CurrencyConvertArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "currency.convert",
            args.model_dump(mode="json", by_alias=True, exclude_none=True),
            lambda: self._convert_payload(args),
        )

    async def fluctuation(self, args: CurrencyFluctuationArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached(
            "currency.fluctuation",
            args.model_dump(mode="json", exclude_none=True),
            lambda: self._fluctuation_payload(args),
        )

    async def _cached(
        self,
        namespace: str,
        payload: dict[str, object],
        loader,
    ) -> tuple[dict[str, object], str, list[str]]:
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"{namespace}:{json.dumps(payload, sort_keys=True, default=str)}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=loader,
        )
        return raw, cache_status, notes

    async def _symbols_payload(self) -> tuple[dict[str, object], list[str]]:
        notes: list[str] = []
        try:
            payload = await self._request_primary_json("/symbols", {})
            return {
                "success": True,
                "symbols": self._normalize_symbols_payload(payload.get("symbols", {})),
                "provider": "exchangeratesapi",
            }, notes
        except CurrencyProviderError as exc:
            notes.extend(self._notes_for_primary_issue(exc))
            try:
                fallback = await self._request_fallback_json("/currencies", {})
            except CurrencyProviderError as fallback_exc:
                raise self._compose_upstream_error(exc, fallback_exc) from fallback_exc
            notes.append("Fell back to Frankfurter currencies because the primary exchange-rate provider could not serve symbols.")
            return {
                "success": True,
                "symbols": self._normalize_symbols_payload(fallback),
                "provider": "frankfurter",
            }, notes

    async def _snapshot_payload(
        self,
        *,
        date_value: str | None,
        base: str | None,
        symbols: str | None,
    ) -> tuple[dict[str, object], list[str]]:
        notes: list[str] = []
        path = self._snapshot_path(date_value)
        params = self._snapshot_params(base=base, symbols=symbols)
        try:
            payload = await self._request_primary_json(path, params)
            return self._normalize_snapshot_payload(payload, requested_date=date_value, historical=date_value is not None), notes
        except CurrencyProviderError as exc:
            # Root Cause vs Logic: apilayer can reject arbitrary `base` values on
            # lower tiers, so we first try to rebuild the requested base from the
            # provider's default-base rates before falling back to a second vendor.
            if exc.code == "base_currency_access_restricted" and base:
                try:
                    payload = await self._cross_rate_snapshot_from_primary(date_value=date_value, base=base, symbols=symbols)
                    notes.append(
                        "Root Cause vs Logic: the primary provider tier rejected the requested base currency, so the service recomputed the requested base from the provider's default-base rates instead of failing the tool call."
                    )
                    return payload, notes
                except CurrencyProviderError as cross_error:
                    self.logger.warning("currency_cross_rate_fallback_failed base=%s reason=%s", base, cross_error)
            notes.extend(self._notes_for_primary_issue(exc))
            try:
                fallback = await self._request_fallback_json(path, params)
            except CurrencyProviderError as fallback_exc:
                raise self._compose_upstream_error(exc, fallback_exc) from fallback_exc
            notes.append("Fell back to Frankfurter because the primary exchange-rate provider could not serve this snapshot.")
            return self._normalize_snapshot_payload(fallback, requested_date=date_value, historical=date_value is not None, provider="frankfurter"), notes

    async def _timeseries_payload(self, args: CurrencyTimeseriesArgs) -> tuple[dict[str, object], list[str]]:
        notes: list[str] = []
        primary_params = {
            "start_date": args.start_date,
            "end_date": args.end_date,
            **self._snapshot_params(base=args.base, symbols=args.symbols),
        }
        try:
            payload = await self._request_primary_json("/timeseries", primary_params)
            return self._normalize_timeseries_payload(payload, args.start_date, args.end_date), notes
        except CurrencyProviderError as exc:
            if exc.code == "base_currency_access_restricted" and args.base:
                try:
                    payload = await self._cross_rate_timeseries_from_primary(args)
                    notes.append(
                        "Root Cause vs Logic: the primary provider tier rejected the requested base currency for the time series, so the service rebuilt the requested series from default-base daily rates."
                    )
                    return payload, notes
                except CurrencyProviderError as cross_error:
                    self.logger.warning("currency_timeseries_cross_rate_failed base=%s reason=%s", args.base, cross_error)
            notes.extend(self._notes_for_primary_issue(exc))
            try:
                fallback = await self._request_fallback_json(
                    f"/{args.start_date}..{args.end_date}",
                    self._snapshot_params(base=args.base, symbols=args.symbols),
                )
            except CurrencyProviderError as fallback_exc:
                raise self._compose_upstream_error(exc, fallback_exc) from fallback_exc
            notes.append("Fell back to Frankfurter because the primary exchange-rate provider could not serve this time series.")
            return self._normalize_timeseries_payload(
                fallback,
                args.start_date,
                args.end_date,
                provider="frankfurter",
            ), notes

    async def _convert_payload(self, args: CurrencyConvertArgs) -> tuple[dict[str, object], list[str]]:
        if args.from_code == args.to:
            payload = {
                "success": True,
                "query": {"from": args.from_code, "to": args.to, "amount": args.amount},
                "info": {"rate": 1.0},
                "historical": args.date is not None,
                "date": args.date or self._today_iso(),
                "result": args.amount,
                "provider": "synthetic",
            }
            return payload, ["Source currency and target currency are identical, so the conversion rate is 1.0."]

        snapshot, notes = await self._snapshot_payload(date_value=args.date, base=args.from_code, symbols=args.to)
        rate = self._lookup_rate(snapshot.get("rates"), args.to)
        result = args.amount * rate
        payload = {
            "success": True,
            "query": {"from": args.from_code, "to": args.to, "amount": args.amount},
            "info": {"rate": rate},
            "historical": args.date is not None,
            "date": snapshot.get("date"),
            "result": result,
            "provider": snapshot.get("provider"),
        }
        return payload, notes

    async def _fluctuation_payload(self, args: CurrencyFluctuationArgs) -> tuple[dict[str, object], list[str]]:
        series, notes = await self._timeseries_payload(
            CurrencyTimeseriesArgs(
                start_date=args.start_date,
                end_date=args.end_date,
                base=args.base,
                symbols=args.symbols,
            )
        )
        raw_rates = series.get("rates")
        if not isinstance(raw_rates, dict) or not raw_rates:
            raise UpstreamServiceError(404, "No fluctuation data was returned for the requested date range.")

        ordered_dates = sorted(raw_rates.keys())
        first_date = ordered_dates[0]
        last_date = ordered_dates[-1]
        first_rates = raw_rates[first_date]
        last_rates = raw_rates[last_date]
        if not isinstance(first_rates, dict) or not isinstance(last_rates, dict):
            raise UpstreamServiceError(502, "Currency fluctuation data was returned in an unexpected format.")

        symbols = sorted(set(first_rates).intersection(last_rates))
        if not symbols:
            raise UpstreamServiceError(404, "No overlapping rates were available to calculate fluctuation data.")

        rates: dict[str, object] = {}
        for symbol in symbols:
            start_rate = float(first_rates[symbol])
            end_rate = float(last_rates[symbol])
            change = end_rate - start_rate
            change_pct = None if isclose(start_rate, 0.0) else (change / start_rate) * 100
            rates[symbol] = {
                "start_rate": start_rate,
                "end_rate": end_rate,
                "change": change,
                "change_pct": change_pct,
            }

        if first_date != args.start_date or last_date != args.end_date:
            notes.append(
                f"Fluctuation used available market dates {first_date} to {last_date} because one or both requested dates did not have direct market data."
            )

        payload = {
            "success": True,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "actual_start_date": first_date,
            "actual_end_date": last_date,
            "base": series.get("base"),
            "rates": rates,
            "provider": series.get("provider"),
        }
        return payload, notes

    async def _cross_rate_snapshot_from_primary(
        self,
        *,
        date_value: str | None,
        base: str,
        symbols: str | None,
    ) -> dict[str, object]:
        request_symbols = self._symbols_for_cross_rate(base, symbols)
        payload = await self._request_primary_json(
            self._snapshot_path(date_value),
            self._snapshot_params(base=None, symbols=request_symbols),
        )
        return self._rebase_snapshot_payload(
            payload,
            requested_base=base,
            requested_symbols=symbols,
            requested_date=date_value,
            historical=date_value is not None,
        )

    async def _cross_rate_timeseries_from_primary(self, args: CurrencyTimeseriesArgs) -> dict[str, object]:
        request_symbols = self._symbols_for_cross_rate(args.base, args.symbols)
        payload = await self._request_primary_json(
            "/timeseries",
            {
                "start_date": args.start_date,
                "end_date": args.end_date,
                **self._snapshot_params(base=None, symbols=request_symbols),
            },
        )
        return self._rebase_timeseries_payload(payload, requested_base=args.base, requested_symbols=args.symbols)

    async def _request_primary_json(self, path: str, params: dict[str, object]) -> dict[str, object]:
        if not self.settings.exchange_rate_api_key:
            raise CurrencyProviderError(
                provider="exchangeratesapi",
                status_code=503,
                message="EXCHANGE_RATE_API is not configured in the environment.",
                code="missing_api_key",
            )
        response = await self._primary.get(path, params={"access_key": self.settings.exchange_rate_api_key, **params})
        payload = self._parse_json(response)
        if response.status_code >= 400:
            raise self._provider_error_from_response("exchangeratesapi", response.status_code, payload, response.text)
        if isinstance(payload, dict) and payload.get("success") is False:
            error = payload.get("error")
            if isinstance(error, dict):
                raise CurrencyProviderError(
                    provider="exchangeratesapi",
                    status_code=response.status_code or 400,
                    message=str(error.get("message") or "Currency provider request failed."),
                    code=str(error.get("code")) if error.get("code") is not None else None,
                    payload=payload,
                )
            raise CurrencyProviderError(
                provider="exchangeratesapi",
                status_code=response.status_code or 400,
                message=json.dumps(payload),
                payload=payload,
            )
        if not isinstance(payload, dict):
            raise CurrencyProviderError(
                provider="exchangeratesapi",
                status_code=502,
                message="Currency provider returned a non-object payload.",
            )
        return payload

    async def _request_fallback_json(self, path: str, params: dict[str, object]) -> dict[str, object]:
        response = await self._fallback.get(path, params=params)
        payload = self._parse_json(response)
        if response.status_code >= 400:
            raise self._provider_error_from_response("frankfurter", response.status_code, payload, response.text)
        if not isinstance(payload, dict):
            raise CurrencyProviderError(
                provider="frankfurter",
                status_code=502,
                message="Fallback currency provider returned a non-object payload.",
            )
        return payload

    def _normalize_snapshot_payload(
        self,
        payload: dict[str, object],
        *,
        requested_date: str | None,
        historical: bool,
        provider: str = "exchangeratesapi",
    ) -> dict[str, object]:
        return {
            "success": True,
            "historical": historical,
            "date": str(payload.get("date") or requested_date or self._today_iso()),
            "base": str(payload.get("base") or "EUR").upper(),
            "rates": self._normalize_rate_map(payload.get("rates")),
            "provider": provider,
        }

    def _normalize_timeseries_payload(
        self,
        payload: dict[str, object],
        start_date: str,
        end_date: str,
        *,
        provider: str = "exchangeratesapi",
    ) -> dict[str, object]:
        raw_rates = payload.get("rates")
        if not isinstance(raw_rates, dict):
            raise UpstreamServiceError(502, "Currency time-series data was returned in an unexpected format.")
        normalized_rates: dict[str, object] = {}
        for date_key, values in raw_rates.items():
            normalized_rates[str(date_key)] = self._normalize_rate_map(values)
        return {
            "success": True,
            "timeseries": True,
            "start_date": start_date,
            "end_date": end_date,
            "base": str(payload.get("base") or "EUR").upper(),
            "rates": normalized_rates,
            "provider": provider,
        }

    def _rebase_snapshot_payload(
        self,
        payload: dict[str, object],
        *,
        requested_base: str,
        requested_symbols: str | None,
        requested_date: str | None,
        historical: bool,
    ) -> dict[str, object]:
        rates = self._normalize_rate_map(payload.get("rates"))
        base_rate = rates.get(requested_base)
        if base_rate is None:
            raise CurrencyProviderError(
                provider="exchangeratesapi",
                status_code=502,
                message=f"The primary provider did not return the requested base currency '{requested_base}' for rebasing.",
            )
        rebased = self._rebase_rates(rates, requested_base, base_rate, requested_symbols)
        return {
            "success": True,
            "historical": historical,
            "date": str(payload.get("date") or requested_date or self._today_iso()),
            "base": requested_base,
            "rates": rebased,
            "provider": "exchangeratesapi_cross_rate",
        }

    def _rebase_timeseries_payload(
        self,
        payload: dict[str, object],
        *,
        requested_base: str | None,
        requested_symbols: str | None,
    ) -> dict[str, object]:
        if not requested_base:
            return self._normalize_timeseries_payload(
                payload,
                str(payload.get("start_date") or ""),
                str(payload.get("end_date") or ""),
            )
        raw_rates = payload.get("rates")
        if not isinstance(raw_rates, dict):
            raise CurrencyProviderError(
                provider="exchangeratesapi",
                status_code=502,
                message="The primary provider returned malformed time-series data during rebasing.",
            )
        rebased_rates: dict[str, object] = {}
        for date_key, values in raw_rates.items():
            normalized = self._normalize_rate_map(values)
            base_rate = normalized.get(requested_base)
            if base_rate is None:
                raise CurrencyProviderError(
                    provider="exchangeratesapi",
                    status_code=502,
                    message=f"The primary provider did not return '{requested_base}' for rebasing on {date_key}.",
                )
            rebased_rates[str(date_key)] = self._rebase_rates(normalized, requested_base, base_rate, requested_symbols)
        return {
            "success": True,
            "timeseries": True,
            "start_date": str(payload.get("start_date") or ""),
            "end_date": str(payload.get("end_date") or ""),
            "base": requested_base,
            "rates": rebased_rates,
            "provider": "exchangeratesapi_cross_rate",
        }

    def _rebase_rates(
        self,
        rates: dict[str, float],
        requested_base: str,
        base_rate: float,
        requested_symbols: str | None,
    ) -> dict[str, float]:
        rebased: dict[str, float] = {}
        allowed_symbols = set(self._split_symbols(requested_symbols)) if requested_symbols else None
        for symbol, value in rates.items():
            if symbol == requested_base:
                continue
            if allowed_symbols is not None and symbol not in allowed_symbols:
                continue
            rebased[symbol] = value / base_rate
        return rebased

    def _normalize_rate_map(self, payload: object) -> dict[str, float]:
        if not isinstance(payload, dict):
            raise UpstreamServiceError(502, "Currency rate data was returned in an unexpected format.")
        normalized: dict[str, float] = {}
        for key, value in payload.items():
            normalized[str(key).upper()] = float(value)
        return normalized

    def _normalize_symbols_payload(self, payload: object) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise UpstreamServiceError(502, "Currency symbol data was returned in an unexpected format.")
        return {str(key).upper(): str(value) for key, value in payload.items()}

    def _lookup_rate(self, payload: object, symbol: str) -> float:
        if not isinstance(payload, dict):
            raise UpstreamServiceError(502, "Currency rate data was returned in an unexpected format.")
        try:
            return float(payload[symbol])
        except KeyError as exc:
            raise UpstreamServiceError(404, f"No exchange rate was returned for target currency '{symbol}'.") from exc

    def _snapshot_path(self, date_value: str | None) -> str:
        return "/latest" if not date_value else f"/{date_value}"

    def _snapshot_params(self, *, base: str | None, symbols: str | None) -> dict[str, object]:
        params: dict[str, object] = {}
        if base:
            params["base"] = base
        if symbols:
            params["symbols"] = symbols
        return params

    def _symbols_for_cross_rate(self, base: str | None, symbols: str | None) -> str | None:
        if not base:
            return symbols
        requested = [base, *self._split_symbols(symbols)]
        return ",".join(dict.fromkeys(requested))

    def _split_symbols(self, symbols: str | None) -> list[str]:
        if not symbols:
            return []
        return [value.strip().upper() for value in symbols.split(",") if value.strip()]

    def _notes_for_primary_issue(self, exc: CurrencyProviderError) -> list[str]:
        if exc.code == "missing_api_key":
            return ["EXCHANGE_RATE_API is not configured, so currency data is using the fallback provider."]
        if exc.code == "base_currency_access_restricted":
            return ["The primary exchange-rate provider plan does not allow arbitrary base currencies for this request."]
        return [f"Primary exchange-rate provider issue: {exc.message}"]

    def _compose_upstream_error(
        self,
        primary_error: CurrencyProviderError,
        fallback_error: CurrencyProviderError,
    ) -> UpstreamServiceError:
        detail = (
            f"Currency providers failed. Primary ({primary_error.provider}): {primary_error.message}. "
            f"Fallback ({fallback_error.provider}): {fallback_error.message}."
        )
        return UpstreamServiceError(max(primary_error.status_code, fallback_error.status_code), detail)

    def _provider_error_from_response(
        self,
        provider: str,
        status_code: int,
        payload: object,
        fallback_text: str,
    ) -> CurrencyProviderError:
        if isinstance(payload, dict):
            if provider == "frankfurter":
                message = str(payload.get("message") or fallback_text)
                return CurrencyProviderError(provider=provider, status_code=status_code, message=message, payload=payload)
            error = payload.get("error")
            if isinstance(error, dict):
                return CurrencyProviderError(
                    provider=provider,
                    status_code=status_code,
                    message=str(error.get("message") or fallback_text),
                    code=str(error.get("code")) if error.get("code") is not None else None,
                    payload=payload,
                )
        return CurrencyProviderError(provider=provider, status_code=status_code, message=fallback_text, payload=payload if isinstance(payload, dict) else None)

    def _parse_json(self, response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:
            return {"message": response.text}

    def _today_iso(self) -> str:
        return datetime.now(UTC).date().isoformat()
