from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from app.text.utils import significant_tokens

_TEXT_ARG_KEYS = {
    "q",
    "search",
    "searchIn",
    "sources",
    "domains",
    "excludeDomains",
    "category",
    "country",
    "language",
}


def format_news_articles(
    payload: dict[str, Any],
    request_args: dict[str, Any],
    request_type: str,
) -> dict[str, Any]:
    """Motivation vs Logic: surface lightweight summaries so Claude can cite sources quickly."""
    request_tokens = _request_tokens(request_args)
    articles = _normalize_articles(payload.get("articles", []), request_tokens)
    summary = _summarize_articles(articles)
    return {
        "requestType": request_type,
        "requestArgs": request_args,
        "requestTokens": request_tokens,
        "status": payload.get("status"),
        "totalResults": payload.get("totalResults"),
        "articleCount": len(articles),
        "publishedRange": summary["publishedRange"],
        "topSources": summary["topSources"],
        "topKeywords": summary["topKeywords"],
        "matchConfidence": summary["matchConfidence"],
        "matchingKeywords": summary["matchingKeywords"],
        "matchingArticles": summary["matchingArticles"],
        "articles": summary["articles"],
    }


def format_news_sources(payload: dict[str, Any], request_args: dict[str, Any]) -> dict[str, Any]:
    """Motivation vs Logic: the agent-reported source roster needs category/language context."""
    sources = [source for source in payload.get("sources", []) if isinstance(source, dict)]
    normalized = [
        {
            "id": source.get("id"),
            "name": source.get("name"),
            "description": source.get("description"),
            "url": source.get("url"),
            "category": source.get("category"),
            "language": source.get("language"),
            "country": source.get("country"),
        }
        for source in sources
    ]
    return {
        "requestType": "sources",
        "requestArgs": request_args,
        "totalSources": len(normalized),
        "byCategory": _top_breakdown([item.get("category") for item in normalized], label="category"),
        "byLanguage": _top_breakdown([item.get("language") for item in normalized], label="language"),
        "byCountry": _top_breakdown([item.get("country") for item in normalized], label="country"),
        "sources": normalized,
    }


def _normalize_articles(raw_articles: list[Any], request_tokens: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    request_token_set = set(request_tokens)
    for index, article in enumerate(raw_articles):
        if not isinstance(article, dict):
            continue
        source = article.get("source") or {}
        title = article.get("title") or ""
        description = article.get("description") or ""
        keywords = significant_tokens(f"{title} {description}")
        published_at = article.get("publishedAt")
        parsed_datetime = _parse_datetime(published_at)
        matched_keywords = [token for token in keywords if token in request_token_set]
        match_score = _calculate_match_score(request_token_set, keywords)
        normalized.append(
            {
                "index": index + 1,
                "title": title.strip() or None,
                "sourceId": source.get("id"),
                "source": source.get("name"),
                "publishedAt": published_at,
                "publishedAtUtc": parsed_datetime.isoformat() if parsed_datetime else None,
                "description": description.strip() or None,
                "url": article.get("url"),
                "imageUrl": article.get("urlToImage"),
                "author": article.get("author"),
                "keywords": keywords,
                "matchingKeywords": matched_keywords,
                "matchScore": match_score,
            }
        )
    return normalized


def _summarize_articles(articles: list[dict[str, Any]]) -> dict[str, Any]:
    counter = Counter()
    published_dates: list[datetime] = []
    matching_keywords_counter: Counter[str] = Counter()
    matching_articles = sorted(
        (article for article in articles if article.get("matchScore")),
        key=lambda entry: entry.get("matchScore", 0),
        reverse=True,
    )
    match_confidence = matching_articles[0]["matchScore"] if matching_articles else 0.0
    for article in articles:
        counter.update(article.get("keywords") or [])
        published_at = article.get("publishedAt")
        parsed = _parse_datetime(published_at)
        if isinstance(parsed, datetime):
            published_dates.append(parsed)
        for keyword in article.get("matchingKeywords", []):
            matching_keywords_counter[keyword] += 1

    top_sources = _top_breakdown([article.get("source") for article in articles], label="source")
    return {
        "publishedRange": _published_range(published_dates),
        "topSources": top_sources,
        "topKeywords": [keyword for keyword, _ in counter.most_common(8)],
        "articles": articles[:6],
        "matchConfidence": match_confidence,
        "matchingKeywords": [keyword for keyword, _ in matching_keywords_counter.most_common(5)],
        "matchingArticles": matching_articles[:3],
    }


def _published_range(dates: list[datetime]) -> dict[str, str | None]:
    if not dates:
        return {"earliest": None, "latest": None}
    earliest = min(dates)
    latest = max(dates)
    return {"earliest": earliest.isoformat(), "latest": latest.isoformat()}


def _top_breakdown(iterable: Iterable[str | None], *, label: str = "label") -> list[dict[str, Any]]:
    counter = Counter(value for value in iterable if value)
    return [
        {"type": label, "value": value, "count": count}
        for value, count in counter.most_common(5)
    ]


def _request_tokens(request_args: dict[str, Any]) -> list[str]:
    tokens_source: list[str] = []
    for key, value in request_args.items():
        if key not in _TEXT_ARG_KEYS:
            continue
        if isinstance(value, str):
            tokens_source.append(value)
        elif isinstance(value, (list, tuple, set)):
            tokens_source.extend(str(item) for item in value if item)
    return significant_tokens(" ".join(tokens_source))


def _calculate_match_score(request_tokens: set[str], keywords: list[str]) -> float:
    if not request_tokens or not keywords:
        return 0.0
    article_tokens = set(keywords)
    if not article_tokens:
        return 0.0
    return len(article_tokens & request_tokens) / len(request_tokens)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
