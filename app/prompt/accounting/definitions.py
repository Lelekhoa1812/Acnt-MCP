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
        "Accounting Example 1 (read/audit):\n"
        "User: I am onboarding the client 'Aurora Hire Co'. I only know the nickname 'Aurora'; "
        "I need the exact Open Collective client, a budget health check (balance + paid-to-date), "
        "recent expenses, recent bank transactions, and I want to triage one duplicate catering "
        "bill plus one grant invoice for approval.\n\n"
        "Assistant:\n"
        "1. Use accounting_account_search to resolve the client label and confirm the best id/number.\n"
        "2. Use accounting_budget_lookup for the resolved client's balance, paid-to-date, and credit balance.\n"
        "3. Use accounting_financial_snapshot for open liabilities, recent expenses, and recent bank transactions.\n"
        "4. Use accounting_expense_workflow for the approval, correction, creation, or cancellation step that follows, and keep the reasoning audit-friendly.\n\n"
        "Accounting Example 2 (record expenses from a narrative):\n"
        "User: Audit and record my Berlin conference trip to the 'BerlinConf' collective. Total spend AUD 1,250 across "
        "flights 800, hotels 300, meals 150. Reimburse to my personal card.\n\n"
        "Assistant:\n"
        "1. Use accounting_account_search to resolve 'BerlinConf' to its slug.\n"
        "2. Use accounting_financial_snapshot to verify there are no duplicate prior entries for this trip.\n"
        "3. For each line item (flights, hotels, meals) call accounting_expense_workflow with action=CREATE — one expense "
        "per line item, populating items[].amountV2 in the originating currency, incurredAt, payee=requesting user's slug, "
        "payoutMethod={type:'OTHER', name:'Personal Card Reimbursement'}, currency=AUD, type=RECEIPT.\n"
        "4. After each create, report the resulting expense id/legacyId so the user can confirm OpenCollective recorded it."
    ),
    "rules": [
        "Use Open Collective tools for client-scoped accounting reads and writes (expenses, bank transactions, payments).",
        "If the request uses a human label, vendor name, or project nickname instead of a known client id, search clients first and only then call the financial snapshot or expense workflow with the resolved client_id.",
        "If search returns a close match that is not exact, surface the match and ask the user to confirm it or create a new client/expense workflow instead of forcing a bad lookup.",
        "If nothing exists, explain that the user can create a new client or expense draft and ask for confirmation before attempting any create mutation.",
        "Ask for or infer the Open Collective client id/number when the request names a specific client.",
        "Use financial_snapshot for balance/health, recent ledger detail, and open-liability questions; use expense_workflow for create/edit/delete/process actions (archive, restore, mark_paid, invoice_expense).",
        "When the user supplies a narrative with concrete line items (amounts, dates, vendors, categories) to be RECORDED into a collective, do not stop at the snapshot. After confirming the slug, call accounting_expense_workflow with action=CREATE once per line item and surface each returned expense id. A snapshot that returns zero rows is the cue to create, not to declare the task done.",
        "If accounting_expense_workflow returns a 403 with 'Personal Access Token is missing required scope', stop calling write tools and report to the user that the OPENCOLLECTIVE_PAT_TOKEN must be regenerated with the 'expenses' scope (and 'transactions' for ledger writes) at https://opencollective.com/dashboard/<their-slug>/for-developers/personal-tokens.",
    ],
}
