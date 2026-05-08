from __future__ import annotations

ACCOUNTING_PROMPT = {
    "domain": "accounting",
    "intent_classes": ["accounting"],
    "tools": [
        "opencollective_expense_list",
        "opencollective_transaction_all",
        "opencollective_budget_lookup",
    ],
    "examples": """
Accounting Example:
User: Show the latest public expenses for the open-source collective webpack.
Assistant: Use `opencollective_expense_list` with the collective slug, then summarize the public expense descriptions, amounts, and statuses.
""".strip(),
    "rules": [
        "Use Open Collective tools for public, read-only financial transparency data.",
        "Ask for or infer the collective slug when the request names a specific collective.",
        "Use budget lookup for balance/health questions and transactions/expenses for ledger detail.",
    ],
}
