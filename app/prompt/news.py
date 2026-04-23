# Motivation vs Logic: keep news examples modular so we can swap sources independent of other domains.
NEWS_EXAMPLES = """
NEWS Example 1:
User: Survey the most recent AI chip supply-chain coverage from trusted US and EU outlets in the last 48 hours.
Assistant: Use `news.search` with a keyword-rich q (e.g., "AI chip", "fabrication", or "semiconductor"), `sortBy=publishedAt`, and a `from`/`to` window covering the past two days. After the tool returns, inspect the `matchConfidence`, `matchingKeywords`, and `matchingArticles` before deciding if the results satisfy the query; only answer when `matchConfidence` is strong (≥0.4) and the matching articles clearly cite the requested keywords. Mention the `topSources`, `topKeywords`, and `publishedRange` fields so the user knows which outlets dominated the coverage, then summarize the freshest headlines from the top 3 articles while calling out any `totalResults` or vendor limits the API reported.

NEWS Example 2:
User: What are today's Australian engineering or infrastructure headlines?
Assistant: Use `news.headlines` with `country=au`, `category=business`, and a small `pageSize`. Treat the returned `topSources` and `topKeywords` as the signal for what is currently trending, highlight the `publishedRange` to prove freshness, and keep the answer focused on the specific category (don’t drift into unrelated industries) along with any API notes (e.g., source coverage or rate-limit warnings).

NEWS Example 3:
User: Which outlets cover freight logistics in New Zealand?
Assistant: Call `news.sources` with `category=business` and `country=nz` first to surface outlet names, languages, and any country-specific spreads; cite the `byCountry`, `byLanguage`, and `byCategory` breakdown. If the user still wants articles, follow with `news.headlines` or `news.search` narrowed to the approved sources, then mention how the summary fields describe coverage versus gaps.
Only answer if the resulting `matchConfidence` indicates a clear keyword match and at least one `matchingArticle` aligns with the user’s freight-logistics focus; otherwise explain the vendor coverage is too sparse and ask if the user wants to broaden the topic or try different keywords.
""".strip()
