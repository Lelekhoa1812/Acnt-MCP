from app.tool.news.formatter import format_news_articles, format_news_sources


def test_format_news_articles_builds_keyword_and_source_summary() -> None:
    payload = {
        "status": "ok",
        "totalResults": 2,
        "articles": [
            {
                "source": {"id": "the-verge", "name": "The Verge"},
                "author": "Jane Doe",
                "title": "AI chip shortage eases as fabs expand",
                "description": "New fabs in the US and EU added capacity this week.",
                "url": "https://example.com/article-1",
                "publishedAt": "2026-04-22T10:00:00Z",
            },
            {
                "source": {"id": "techcrunch", "name": "TechCrunch"},
                "author": "Alex Roe",
                "title": "Semiconductor supply chains shift toward clean energy",
                "description": "Renewable-powered fabs accelerate rollout across Europe.",
                "url": "https://example.com/article-2",
                "publishedAt": "2026-04-22T12:30:00Z",
            },
        ],
    }
    args = {"q": "ai chip", "sortBy": "publishedAt"}
    summary = format_news_articles(payload, args, request_type="search")

    assert summary["requestType"] == "search"
    assert summary["articleCount"] == 2
    assert summary["status"] == "ok"
    assert summary["totalResults"] == 2
    top_source_values = [entry["value"] for entry in summary["topSources"]]
    assert "The Verge" in top_source_values
    assert "TechCrunch" in top_source_values
    assert any(keyword in summary["topKeywords"] for keyword in ("ai", "chip", "semiconductor"))
    assert summary["publishedRange"]["earliest"].startswith("2026-04-22T10:00:00")
    assert summary["publishedRange"]["latest"].startswith("2026-04-22T12:30:00")
    assert summary["requestArgs"] == args
    assert set(summary["requestTokens"]) == {"ai", "chip"}
    assert summary["matchConfidence"] == 1.0
    assert "ai" in summary["matchingKeywords"]
    assert summary["matchingArticles"][0]["matchScore"] == 1.0
    assert len(summary["articles"]) == 2
    assert summary["articles"][0]["url"] == "https://example.com/article-1"
    assert "renewable" in summary["articles"][1]["description"].lower()


def test_format_news_sources_groups_category_language_and_country() -> None:
    payload = {
        "sources": [
            {
                "id": "nzherald",
                "name": "NZ Herald",
                "category": "business",
                "language": "en",
                "country": "nz",
            },
            {
                "id": "stuff",
                "name": "Stuff",
                "category": "business",
                "language": "en",
                "country": "nz",
            },
            {
                "id": "tech",
                "name": "Tech News",
                "category": "technology",
                "language": "en",
                "country": "au",
            },
        ]
    }
    args = {"category": "business", "country": "nz"}
    summary = format_news_sources(payload, args)

    assert summary["requestType"] == "sources"
    assert summary["totalSources"] == 3
    assert summary["requestArgs"] == args
    category_values = {entry["value"]: entry["count"] for entry in summary["byCategory"]}
    assert category_values["business"] == 2
    language_values = {entry["value"]: entry["count"] for entry in summary["byLanguage"]}
    assert language_values["en"] == 3
    country_values = {entry["value"]: entry["count"] for entry in summary["byCountry"]}
    assert country_values["nz"] == 2
    assert any(src["id"] == "stuff" for src in summary["sources"])
