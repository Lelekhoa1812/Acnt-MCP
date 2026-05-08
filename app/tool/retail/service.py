from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

import httpx

from app.config import Settings, UpstreamServiceError
from app.tool.retail.model import OpenLibraryBookSearchArgs, OpenLibraryIsbnLookupArgs, OpenLibrarySubjectListArgs
from app.store import AppKeyValueStore


class OpenLibraryService:
    def __init__(self, settings: Settings, key_value_store: AppKeyValueStore, logger: logging.Logger) -> None:
        self.settings = settings
        self.key_value_store = key_value_store
        self.logger = logger
        self._client = httpx.AsyncClient(base_url="https://openlibrary.org", timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def book_search(self, args: OpenLibraryBookSearchArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("openlibrary_book_search", args.model_dump(mode="json", exclude_none=True), lambda: self._book_search_payload(args))

    async def isbn_lookup(self, args: OpenLibraryIsbnLookupArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("openlibrary_isbn_lookup", args.model_dump(mode="json", exclude_none=True), lambda: self._isbn_lookup_payload(args))

    async def subject_list(self, args: OpenLibrarySubjectListArgs) -> tuple[dict[str, object], str, list[str]]:
        return await self._cached("openlibrary_subject_list", args.model_dump(mode="json", exclude_none=True), lambda: self._subject_list_payload(args))

    async def _cached(self, namespace: str, payload: dict[str, object], loader) -> tuple[dict[str, object], str, list[str]]:
        raw, cache_status, notes = await self.key_value_store.cached_call(
            namespace="tool",
            key=f"{namespace}:{json.dumps(payload, sort_keys=True, default=str)}",
            ttl_seconds=self.settings.cache_ttl_seconds,
            loader=loader,
        )
        return raw, cache_status, notes

    async def _book_search_payload(self, args: OpenLibraryBookSearchArgs) -> tuple[dict[str, object], list[str]]:
        params = {
            "q": args.query or args.title or args.author or args.subject,
            "title": args.title,
            "author": args.author,
            "subject": args.subject,
            "page": args.page,
            "limit": args.limit,
        }
        payload = await self._get("/search.json", params)
        return {"query": args.model_dump(mode="json", exclude_none=True), "search": payload}, []

    async def _isbn_lookup_payload(self, args: OpenLibraryIsbnLookupArgs) -> tuple[dict[str, object], list[str]]:
        payload = await self._get(f"/isbn/{args.isbn}.json", {})
        return {"isbn": args.isbn, "book": payload}, []

    async def _subject_list_payload(self, args: OpenLibrarySubjectListArgs) -> tuple[dict[str, object], list[str]]:
        payload = await self._get(
            f"/subjects/{args.subject}.json",
            {"limit": args.limit, "offset": (args.page - 1) * args.limit},
        )
        return {"subject": args.subject, "subjectList": payload}, []

    async def _get(self, path: str, params: dict[str, object]) -> dict[str, object]:
        response = await self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        if response.status_code >= 400:
            raise UpstreamServiceError(response.status_code, response.text, request=self._request_label(path, params))
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstreamServiceError(502, "Open Library returned an unexpected payload.", request=self._request_label(path, params))
        return payload

    @staticmethod
    def _request_label(path: str, params: dict[str, object]) -> str:
        query = urlencode({key: value for key, value in params.items() if value is not None}, doseq=True)
        return f"GET {path}?{query}" if query else f"GET {path}"
