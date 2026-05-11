from __future__ import annotations

ACCOUNTING_PROMPT = {
    "domain": "accounting",
    "intent_classes": ["accounting"],
    "tools": [
        "accounting_account_search",
        "accounting_financial_snapshot",
        "accounting_expense_workflow",
    ],
    "examples": (
        "Accounting Example:\n"
        "User: I am onboarding the client 'Aurora Hire Co'. I only know the nickname 'Aurora'; "
        "I need the exact Open Collective client, a budget health check (balance + paid-to-date), "
        "recent expenses, recent bank transactions, and I want to triage one duplicate catering "
        "bill plus one grant invoice for approval.\n\n"
        "Assistant:\n"
        "1. Use accounting_account_search to resolve the client label and confirm the best id/number.\n"
        "2. Use accounting_budget_lookup for the resolved client's balance, paid-to-date, and credit balance.\n"
        "3. Use accounting_financial_snapshot for open liabilities, recent expenses, and recent bank transactions.\n"
        "4. Use accounting_expense_workflow for the approval, correction, creation, or cancellation step that follows, and keep the reasoning audit-friendly."
    ),
    "rules": [
        "Use Open Collective tools for client-scoped accounting reads and writes (expenses, bank transactions, payments).",
        "If the request uses a human label, vendor name, or project nickname instead of a known client id, search clients first and only then call the financial snapshot or expense workflow with the resolved client_id.",
        "If search returns a close match that is not exact, surface the match and ask the user to confirm it or create a new client/expense workflow instead of forcing a bad lookup.",
        "If nothing exists, explain that the user can create a new client or expense draft and ask for confirmation before attempting any create mutation.",
        "Ask for or infer the Open Collective client id/number when the request names a specific client.",
        "Use financial_snapshot for balance/health, recent ledger detail, and open-liability questions; use expense_workflow for create/edit/delete/process actions (archive, restore, mark_paid, invoice_expense).",
    ],
}
