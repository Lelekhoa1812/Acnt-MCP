from __future__ import annotations

ACCOUNTING_PROMPT = {
    "domain": "accounting",
    "intent_classes": ["accounting"],
    "tools": [
        "accounting_account_search",
        "accounting_financial_snapshot",
        "accounting_expense_workflow",
    ],
    "examples": """
Accounting Example:
User: Show the latest public expenses for the open-source collective webpack.
Assistant: Use `accounting_financial_snapshot` with the collective slug, then summarize the budget, recent expenses, and open liabilities.
""".strip(),
    "rules": [
        "Use Open Collective tools for public, read-only financial transparency data.",
        "If the request uses a human label, budget code, or project name instead of a known slug, search accounts first and only then call the financial snapshot or expense workflow with the resolved slug.",
        "If search returns a close match that is not exact, surface the match and ask the user to confirm it or create a new account/slug/expense workflow instead of forcing a bad lookup.",
        "If nothing exists, explain that the user can create a new account/slug/expense draft and ask for confirmation before attempting any create mutation.",
        "Ask for or infer the collective slug when the request names a specific collective.",
        "Use financial_snapshot for balance/health, recent ledger detail, and open-liability questions; use expense_workflow for create/edit/delete/process actions.",
    ],
}
