from __future__ import annotations

# Motivation vs Logic: keep the accounting prompt anchored on a realistic
# multi-step Open Collective onboarding and reconciliation story so the planner
# learns to resolve aliases, inspect balances, browse ledger rows, and route
# create/edit/delete/process work through the accounting workflow instead of
# stopping at the first match.
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
User: I am onboarding the Aurora Open Source Collective. I only know the nickname "Aurora OSS", I need the exact account, a budget health check, the latest public expenses, recent transactions, and I want to triage one duplicate catering bill plus one grant invoice for approval.

Assistant:
1. Use `accounting_account_search` to resolve the human label and confirm the best slug.
2. Use `accounting_budget_lookup` for the resolved account's balance and budget posture.
3. Use `accounting_financial_snapshot` for open liabilities, recent expenses, and recent transactions.
4. Use `accounting_expense_workflow` for the approval, correction, creation, or cancellation step that follows, and keep the reasoning audit-friendly.
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
