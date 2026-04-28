# Motivation vs Logic: keep currency guidance separate for easier vendor-toggle.
CURRENCY_EXAMPLES = """
CURRENCY Example:
User: Convert 250 AUD to USD using last year's rate.
Assistant: use fx_convert with the requested date when possible; if the vendor plan blocks the lookup, explain that limitation instead of guessing.
""".strip()
