# Motivation vs Logic: isolate the weather few-shot so it can be swapped
# independently when the plugin evolves.
WEATHER_EXAMPLES = """
WEATHER Example:
User: What's the weather forecast for Melbourne this weekend?
Assistant: use weather.forecast, resolving the location dynamically if needed, then summarize the returned forecast windows.
""".strip()
