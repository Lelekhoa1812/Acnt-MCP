from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, root_validator


class _OpenCollectiveBaseArgs(BaseModel):
    slug: str = Field(description="Open Collective account slug.")
    limit: int = Field(20, ge=1, le=100, description="Maximum rows to return.")
    offset: int = Field(0, ge=0, le=10_000, description="Offset into the row set.")

    @field_validator("slug")
    @classmethod
    def _strip_slug(cls, value: str) -> str:
        cleaned = _strip_text(value)
        if not cleaned:
            raise ValueError("slug must not be empty.")
        return cleaned


def _strip_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class OpenCollectiveExpenseListArgs(_OpenCollectiveBaseArgs):
    search_term: str | None = Field(None, description="Optional expense search term.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        return _strip_text(value)


class OpenCollectiveTransactionAllArgs(_OpenCollectiveBaseArgs):
    search_term: str | None = Field(None, description="Optional transaction search term.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        return _strip_text(value)


class OpenCollectiveBudgetLookupArgs(_OpenCollectiveBaseArgs):
    @model_validator(mode="after")
    def _validate_slug(self) -> "OpenCollectiveBudgetLookupArgs":
        return self


class OpenCollectiveCollectiveSearchArgs(BaseModel):
    search_term: str = Field(description="Open Collective collective search term.")
    limit: int = Field(10, ge=1, le=25, description="Maximum search matches to return.")
    offset: int = Field(0, ge=0, le=10_000, description="Offset into the search result set.")
    include_archived: bool = Field(False, description="Include archived accounts in the search.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str) -> str:
        cleaned = _strip_text(value)
        if not cleaned:
            raise ValueError("search_term must not be empty.")
        return cleaned


# Keep backward-compatible alias so existing test fixtures and callers don't break immediately.
OpenCollectiveAccountSearchArgs = OpenCollectiveCollectiveSearchArgs


class OpenCollectiveCollectiveListArgs(BaseModel):
    limit: int = Field(20, ge=1, le=100, description="Maximum collectives to return.")
    offset: int = Field(0, ge=0, le=10_000, description="Offset into the result set.")
    search_term: str | None = Field(None, description="Optional keyword filter for collectives.")
    include_archived: bool = Field(False, description="Include archived collectives.")
    type: list[str] | None = Field(
        None,
        description="Account types to include (e.g. COLLECTIVE, FUND, ORGANIZATION). Omit for all types.",
    )
    roles: list[str] | None = Field(
        None,
        description="Filter by member role (e.g. ADMIN, MEMBER, ACCOUNTANT). Defaults to [ADMIN, MEMBER, ACCOUNTANT].",
    )

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        return _strip_text(value)


class CollectiveCreateInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., description="Display name of the collective.")
    slug: str | None = Field(None, description="Desired URL slug (auto-generated if omitted).")
    description: str | None = Field(None, description="Short description.")
    tags: list[str] | None = Field(None, description="Categorisation tags.")
    website: str | None = Field(None, description="Website URL.")
    githubHandle: str | None = Field(None, description="GitHub organisation handle.")
    twitterHandle: str | None = Field(None, description="Twitter handle.")


class OpenCollectiveCollectiveCreateArgs(BaseModel):
    collective: CollectiveCreateInput = Field(..., description="Collective payload to create.")
    host: AccountReferenceInput = Field(..., description="Host collective that will approve this application. Use accounting_host_list to discover available host slugs.")
    message: str | None = Field(None, description="Application message sent to the host admins.")


class CollectiveUpdateInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str | None = Field(None, description="Public account ID (e.g. acc_xxx). Preferred identifier — pass this if available.")
    slug: str | None = Field(None, description="Current URL slug of the collective (used to resolve the account id when 'id' is not provided).")
    new_slug: str | None = Field(None, description="New URL slug to assign to the collective (e.g. 'harmonise-stock').")
    currency: str | None = Field(None, description="New ISO 4217 currency code (e.g. 'AUD', 'EUR'). Changes the collective's native currency.")
    name: str | None = Field(None, description="New display name.")
    description: str | None = Field(None, description="New short description.")
    tags: list[str] | None = Field(None, description="New categorisation tags.")
    website: str | None = Field(None, description="New website URL.")

    @model_validator(mode="after")
    def _require_identifier(self) -> "CollectiveUpdateInput":
        if not self.id and not self.slug:
            raise ValueError("provide at least one of 'id' or 'slug' to identify the collective.")
        return self

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        return cleaned or None


class OpenCollectiveCollectiveUpdateArgs(BaseModel):
    collective: CollectiveUpdateInput = Field(
        ...,
        description=(
            "Collective update payload. Must include 'id' or 'slug' to identify the collective, "
            "plus any fields to change (e.g. currency='AUD'). "
            "Use accounting_collective_search first to confirm the slug/id."
        ),
    )


class OpenCollectiveHostListArgs(BaseModel):
    limit: int = Field(10, ge=1, le=50, description="Maximum hosts to return.")
    offset: int = Field(0, ge=0, le=10_000, description="Offset into the result set.")
    search_term: str | None = Field(None, description="Optional keyword filter for host name or description.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        return _strip_text(value)

class OpenCollectivePayeeListArgs(BaseModel):
    limit: int = Field(20, ge=1, le=100, description="Maximum payees to return.")
    offset: int = Field(0, ge=0, le=10_000, description="Offset into the result set.")
    search_term: str | None = Field(None, description="Optional search filter (name, slug).")
    types: list[str] | None = Field(
        None,
        description="Account types to include. Defaults to [INDIVIDUAL, ORGANIZATION, VENDOR].",
    )

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        return _strip_text(value)


class OpenCollectivePayeeViewArgs(BaseModel):
    slug: str | None = Field(None, description="Open Collective slug of the payee account.")
    id: str | None = Field(None, description="Public Open Collective account id (e.g. acc_xxx).")

    @model_validator(mode="after")
    def _require_identifier(cls, values: "OpenCollectivePayeeViewArgs") -> "OpenCollectivePayeeViewArgs":
        if not values.slug and not values.id:
            raise ValueError("provide at least one of 'slug' or 'id'.")
        return values


class OrganizationCreateInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., description="Display name of the organisation.")
    legalName: str | None = Field(None, description="Legal registered name.")
    slug: str | None = Field(None, description="Desired URL slug (auto-generated if omitted).")
    description: str | None = Field(None, description="Short description.")
    website: str | None = Field(None, description="Website URL.")
    twitterHandle: str | None = Field(None, description="Twitter handle.")
    githubHandle: str | None = Field(None, description="GitHub handle.")


class OpenCollectivePayeeCreateArgs(BaseModel):
    organization: OrganizationCreateInput = Field(..., description="Organisation details to create as a payee.")

class OpenCollectiveFinancialSnapshotArgs(_OpenCollectiveBaseArgs):
    expense_limit: int = Field(20, ge=1, le=100, description="Maximum expense rows to include.")
    expense_offset: int = Field(0, ge=0, le=10_000, description="Offset into the expense rows.")
    transaction_limit: int = Field(20, ge=1, le=100, description="Maximum transaction rows to include.")
    transaction_offset: int = Field(0, ge=0, le=10_000, description="Offset into the transaction rows.")
    expense_search_term: str | None = Field(None, description="Optional search filter for expenses.")
    transaction_search_term: str | None = Field(None, description="Optional search filter for transactions.")
    include_open_liabilities: bool = Field(True, description="Include derived open-liability summaries.")
    display_currency: str | None = Field(
        None,
        description=(
            "Optional ISO 4217 target currency for display (e.g. 'AUD', 'GBP'). "
            "When set, all stat amounts are also reported in this currency at today's FX rate "
            "under 'display_stats' and 'display_summary'. Native amounts are always included."
        ),
    )

    @field_validator("expense_search_term", "transaction_search_term")
    @classmethod
    def _strip_optional_search(cls, value: str | None) -> str | None:
        return _strip_text(value)

    @field_validator("display_currency")
    @classmethod
    def _normalize_display_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        return cleaned or None


class ExpenseWorkflowAction(str, Enum):
    CREATE = "CREATE"
    EDIT = "EDIT"
    DELETE = "DELETE"
    PROCESS = "PROCESS"


class AccountReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(None, description="Public Open Collective account id (e.g. acc_xxx).")
    slug: str | None = Field(None, description="Public Open Collective slug.")

    @model_validator(mode="after")
    def _require_identifier(cls, values: "AccountReferenceInput") -> "AccountReferenceInput":
        if not values.id and not values.slug:
            raise ValueError("provide at least one of 'id' or 'slug'.")
        return values


class ExpenseReferenceInput(BaseModel):
    id: str | None = Field(None, description="Public expense id (e.g. ex_xxx).")
    legacyId: int | None = Field(None, description="Numeric legacy expense id.")

    @model_validator(mode="after")
    def _require_identifier(cls, values: "ExpenseReferenceInput") -> "ExpenseReferenceInput":
        if not values.id and not values.legacyId:
            raise ValueError("provide 'id' or 'legacyId' to reference an expense.")
        return values


class AmountInput(BaseModel):
    value: float | None = Field(None, description="Amount in float.")
    currency: str | None = Field(None, description="Currency code.")
    valueInCents: int | None = Field(None, description="Amount in cents.")
    exchangeRate: dict[str, object] | None = Field(
        None,
        description="Optional exchange rate metadata.",
    )


class ExpenseItemCreateInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str = Field(..., description="Line-item description.")
    amountV2: AmountInput | None = Field(None, description="Amount for the line item.")
    incurredAt: str | None = Field(None, description="ISO timestamp when the expense occurred.")
    url: str | None = Field(None, description="Optional URL for receipts or metadata. Also accepts 'attachment' as an alias.")

    @model_validator(mode="before")
    @classmethod
    def _normalize_attachment(cls, values: Any) -> Any:
        if isinstance(values, dict) and "attachment" in values:
            values = dict(values)
            attachment = values.pop("attachment")
            if attachment and not values.get("url"):
                values["url"] = attachment
        return values

    @field_validator("incurredAt", mode="before")
    @classmethod
    def _normalize_incurred_at(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        s = value.strip()
        if not s:
            return None
        # Date-only (YYYY-MM-DD) → midnight UTC, full ISO-8601 with millis.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return f"{s}T00:00:00.000Z"
        return s


class PayoutMethodType(str, Enum):
    OTHER = "OTHER"
    PAYPAL = "PAYPAL"
    BANK_ACCOUNT = "BANK_ACCOUNT"
    ACCOUNT_BALANCE = "ACCOUNT_BALANCE"
    CREDIT_CARD = "CREDIT_CARD"
    STRIPE = "STRIPE"


# Aliases an AI agent typically emits. Note: CREDIT_CARD and STRIPE exist in
# Open Collective's enum but are NOT valid payout types for a normal
# reimbursement — CREDIT_CARD is an *incoming* payment method, and STRIPE is
# gated to SETTLEMENT/PLATFORM_BILLING expenses. "Paid by card" means the
# real-world purchase was on the user's card; the reimbursement payout is
# out-of-band, which OC models as type=OTHER with data.content describing it.
_PAYOUT_METHOD_ALIASES: dict[str, PayoutMethodType] = {
    "card": PayoutMethodType.OTHER,
    "credit": PayoutMethodType.OTHER,
    "creditcard": PayoutMethodType.OTHER,
    "debit": PayoutMethodType.OTHER,
    "debitcard": PayoutMethodType.OTHER,
    "bank": PayoutMethodType.BANK_ACCOUNT,
    "bankaccount": PayoutMethodType.BANK_ACCOUNT,
    "banktransfer": PayoutMethodType.BANK_ACCOUNT,
    "wire": PayoutMethodType.BANK_ACCOUNT,
    "wiretransfer": PayoutMethodType.BANK_ACCOUNT,
    "ach": PayoutMethodType.BANK_ACCOUNT,
    "transfer": PayoutMethodType.BANK_ACCOUNT,
    "eft": PayoutMethodType.BANK_ACCOUNT,
    "paypal": PayoutMethodType.PAYPAL,
    "stripe": PayoutMethodType.STRIPE,
    "balance": PayoutMethodType.ACCOUNT_BALANCE,
    "accountbalance": PayoutMethodType.ACCOUNT_BALANCE,
    "cash": PayoutMethodType.OTHER,
    "manual": PayoutMethodType.OTHER,
    "outofband": PayoutMethodType.OTHER,
    "reimbursement": PayoutMethodType.OTHER,
    "reimburse": PayoutMethodType.OTHER,
    "other": PayoutMethodType.OTHER,
}


class PayoutMethodInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: PayoutMethodType = Field(
        ...,
        description=(
            "Payout method type. Must be one of OTHER, PAYPAL, BANK_ACCOUNT, "
            "ACCOUNT_BALANCE, CREDIT_CARD, STRIPE. Colloquial aliases like "
            "'card', 'bank', 'wire', 'cash' are normalised. For real-world "
            "card payments use CREDIT_CARD; for cash / manual / out-of-band "
            "use OTHER and record details in `name`."
        ),
    )
    publicId: str | None = Field(None, description="Existing payout method public id.")
    name: str | None = Field(None, description="Friendly payout method name.")
    data: dict[str, object] | None = Field(None, description="Payout-method specific data.")
    isSaved: bool | None = Field(None, description="Whether to save the payout method.")

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_payout_type(cls, value: Any) -> Any:
        if isinstance(value, PayoutMethodType):
            return value
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("payoutMethod.type must not be empty.")
        upper = cleaned.upper()
        if upper in PayoutMethodType.__members__:
            return PayoutMethodType[upper]
        alias_key = re.sub(r"[\s_\-]+", "", cleaned).lower()
        aliased = _PAYOUT_METHOD_ALIASES.get(alias_key)
        if aliased is not None:
            return aliased
        valid = ", ".join(m.value for m in PayoutMethodType)
        raise ValueError(
            f"payoutMethod.type '{value}' is not a valid Open Collective payout "
            f"type. Valid values: {valid}. Use OTHER for cash / card / manual "
            f"reimbursements."
        )

    @model_validator(mode="after")
    def _validate_payout_shape(self) -> "PayoutMethodInput":
        # If the user is referencing a saved payout method via publicId we
        # don't need inline data — OC will resolve the saved record.
        if self.publicId:
            return self

        # CREDIT_CARD: enum member but NOT a valid payout type for expenses
        # (it's an incoming payment method). OC silently rejects expenses
        # filed with this type; fail fast with the actionable remap.
        if self.type == PayoutMethodType.CREDIT_CARD:
            raise ValueError(
                "payoutMethod.type=CREDIT_CARD is not a valid Open Collective "
                "expense payout method (credit cards are an incoming payment "
                "method, not a reimbursement channel). If the real-world "
                "purchase was on a card, use type=OTHER and describe it in "
                "data.content (e.g. {'type': 'OTHER', 'data': {'content': "
                "'Paid by card — reimburse manually'}})."
            )

        # OC's createExpense rejects STRIPE unless the expense is a SETTLEMENT
        # or PLATFORM_BILLING (internal types). Surface that locally.
        if self.type == PayoutMethodType.STRIPE:
            raise ValueError(
                "payoutMethod.type=STRIPE is only allowed for SETTLEMENT / "
                "PLATFORM_BILLING expenses on Open Collective. Use type=OTHER "
                "for manual reimbursements, or type=PAYPAL / BANK_ACCOUNT "
                "when the host has those integrations connected."
            )

        # OTHER requires data.content to be a non-empty string, otherwise OC
        # silently fails the expense (see opencollective/opencollective#3537).
        # Auto-populate from `name` if the caller supplied a free-text note,
        # so AI callers that pass {'type': 'OTHER', 'name': 'Paid by card'}
        # don't get stuck. Only raise if we genuinely have nothing.
        if self.type == PayoutMethodType.OTHER:
            data = dict(self.data) if isinstance(self.data, dict) else {}
            content = data.get("content")
            if not isinstance(content, str) or not content.strip():
                fallback = (self.name or "").strip() or "Manual / out-of-band reimbursement"
                data["content"] = fallback
                self.data = data
        return self


class ExpenseType(str, Enum):
    INVOICE = "INVOICE"
    RECEIPT = "RECEIPT"
    GRANT = "GRANT"
    CHARGE = "CHARGE"
    SETTLEMENT = "SETTLEMENT"
    UNCLASSIFIED = "UNCLASSIFIED"
    FUNDING_REQUEST = "FUNDING_REQUEST"


class ExpenseCreateInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    description: str = Field(..., description="Expense description.")
    type: ExpenseType = Field(
        ...,
        description=(
            "Expense type. Use INVOICE when the payee is billing for services "
            "and no receipt image is available — items do NOT need `url`. "
            "Use RECEIPT only when reimbursing a purchase and EVERY item carries "
            "a public `url` linking to the receipt file (PDF/image). Open "
            "Collective rejects RECEIPT expenses whose items lack a `url`."
        ),
    )
    payee: AccountReferenceInput = Field(..., description="Account receiving the funds.")
    payoutMethod: PayoutMethodInput = Field(..., description="How the payee gets paid.")
    currency: str | None = Field(None, description="Payout currency (defaults to account currency).")
    longDescription: str | None = Field(None, description="Additional narrative text.")
    privateMessage: str | None = Field(None, description="Private note for the payee.")
    reference: str | None = Field(None, description="Accounting reference number.")
    tags: list[str] | None = Field(None, description="Optional expense tags.")
    accountingCategory: dict[str, object] | None = Field(None, description="Accounting category reference.")
    invoiceInfo: str | None = Field(None, description="Custom invoice information.")
    items: list[ExpenseItemCreateInput] | None = Field(None, description="Line items contributing to the total.")

    @model_validator(mode="after")
    def _validate_receipt_items(self) -> "ExpenseCreateInput":
        if self.type == ExpenseType.RECEIPT and self.items:
            missing = [i for i, item in enumerate(self.items) if not item.url]
            if missing:
                raise ValueError(
                    f"type=RECEIPT requires every item to include a `url` "
                    f"pointing to the receipt file. Items missing url "
                    f"(0-indexed): {missing}. Either provide a public URL for "
                    f"each item, or switch to type=INVOICE if no receipt image "
                    f"is available."
                )
        return self


class ExpenseUpdateInput(ExpenseCreateInput):
    id: str = Field(..., description="Expense public id to update.")


class ExpenseProcessAction(str, Enum):
    APPROVE = "APPROVE"
    UNAPPROVE = "UNAPPROVE"
    REQUEST_RE_APPROVAL = "REQUEST_RE_APPROVAL"
    REJECT = "REJECT"
    MARK_AS_UNPAID = "MARK_AS_UNPAID"
    SCHEDULE_FOR_PAYMENT = "SCHEDULE_FOR_PAYMENT"
    UNSCHEDULE_PAYMENT = "UNSCHEDULE_PAYMENT"
    PAY = "PAY"
    MARK_AS_PAID_WITH_STRIPE = "MARK_AS_PAID_WITH_STRIPE"
    MARK_AS_SPAM = "MARK_AS_SPAM"
    MARK_AS_INCOMPLETE = "MARK_AS_INCOMPLETE"
    HOLD = "HOLD"
    RELEASE = "RELEASE"
    DECLINE_INVITED_EXPENSE = "DECLINE_INVITED_EXPENSE"


class ProcessExpensePaymentParams(BaseModel):
    model_config = ConfigDict(extra="allow")
    clearedAt: str | None = Field(None, description="When funds cleared on host bank.")
    feesPayer: str | None = Field(None, description="Who pays the fees (COLLECTIVE, etc.).")
    forceManual: bool | None = Field(None, description="Force manual payment handling.")
    markAsUnPaidStatus: str | None = Field(None, description="Status used when marking unpaid.")
    paymentMethodService: str | None = Field(None, description="Payment method service name.")
    paymentProcessorFeeInHostCurrency: int | None = Field(None, description="Fee in host currency cents.")
    shouldRefundPaymentProcessorFee: bool | None = Field(None, description="Refund processor fee when marking unpaid.")
    totalAmountPaidInHostCurrency: int | None = Field(None, description="Total amount paid in host currency.")
    transfer: dict[str, object] | None = Field(None, description="Transfer details for manual payment.")


class OpenCollectiveExpenseCreateArgs(BaseModel):
    account: AccountReferenceInput = Field(..., description="Account receiving the expense.")
    expense: ExpenseCreateInput = Field(..., description="Expense to submit.")
    privateComment: str | None = Field(None, description="Private comment for the administrator.")


class OpenCollectiveExpenseUpdateArgs(BaseModel):
    expense: ExpenseUpdateInput = Field(..., description="Expense payload with updated values.")


class OpenCollectiveExpenseDeleteArgs(BaseModel):
    expense: ExpenseReferenceInput = Field(..., description="Expense to delete.")


class OpenCollectiveExpenseProcessArgs(BaseModel):
    expense: ExpenseReferenceInput = Field(..., description="Expense to process.")
    action: ExpenseProcessAction = Field(..., description="Action that should be invoked.")
    message: str | None = Field(None, description="Log message attached to the action.")
    paymentParams: ProcessExpensePaymentParams | None = Field(None, description="Optional payment metadata.")



_EXPENSE_FIELD_DESCRIPTION = (
    "Expense payload. Shape depends on `action`: "
    "CREATE -> {description, type (INVOICE|RECEIPT), payee:{id|slug}, payoutMethod:{type,...}, "
    "items?:[{description, amountV2:{valueInCents, currency}, incurredAt?}], currency?, tags?, ...}; "
    "EDIT -> ExpenseCreateInput fields plus {id}; "
    "DELETE / PROCESS -> {id} or {legacyId}."
)


class OpenCollectiveExpenseWorkflowArgs(BaseModel):
    # Motivation vs Logic: the workflow tool dispatches four distinct mutations (CREATE/EDIT/DELETE/PROCESS)
    # whose `expense` payloads have very different required fields. We expose all three concrete shapes in
    # the schema so MCP clients can see what to populate per action, while still accepting a raw dict for
    # forward-compatibility with future Open Collective fields.
    action: ExpenseWorkflowAction = Field(description="Expense workflow step to execute.")
    account: AccountReferenceInput | None = Field(None, description="Account receiving the expense for CREATE actions.")
    expense: ExpenseUpdateInput | ExpenseCreateInput | ExpenseReferenceInput | dict[str, Any] = Field(
        ..., description=_EXPENSE_FIELD_DESCRIPTION
    )
    privateComment: str | None = Field(None, description="Private comment for create actions.")
    processAction: ExpenseProcessAction | None = Field(None, description="Expense process action for workflow processing.")
    message: str | None = Field(None, description="Optional workflow message.")
    paymentParams: ProcessExpensePaymentParams | None = Field(None, description="Optional payment metadata.")

    @model_validator(mode="after")
    def _validate_workflow_shape(self) -> "OpenCollectiveExpenseWorkflowArgs":
        expense_payload = self._expense_as_dict()
        if not expense_payload:
            raise ValueError("provide 'expense' payload.")
        if self.action == ExpenseWorkflowAction.CREATE:
            if self.account is None:
                raise ValueError("provide 'account' for CREATE actions.")
            missing = [
                key for key in ("description", "type", "payee", "payoutMethod")
                if not expense_payload.get(key)
            ]
            if missing:
                raise ValueError(
                    f"CREATE expense payload missing required fields: {', '.join(missing)}. "
                    "Provide description, type (INVOICE|RECEIPT), payee:{slug|id}, payoutMethod:{type,...}."
                )
            # When `expense` was passed as a raw dict the nested ExpenseCreateInput
            # validator never runs, so mirror its RECEIPT/url check here so the
            # MCP fails fast instead of waiting for OC to reject the request.
            if expense_payload.get("type") == ExpenseType.RECEIPT.value:
                items = expense_payload.get("items") or []
                missing_urls = [
                    i for i, item in enumerate(items)
                    if not (isinstance(item, dict) and item.get("url"))
                ]
                if missing_urls:
                    raise ValueError(
                        f"type=RECEIPT requires every item to include a `url` "
                        f"pointing to the receipt file. Items missing url "
                        f"(0-indexed): {missing_urls}. Either provide a public "
                        f"URL for each item, or switch to type=INVOICE if no "
                        f"receipt image is available."
                    )
        elif self.action == ExpenseWorkflowAction.EDIT:
            if not expense_payload.get("id"):
                raise ValueError("EDIT expense payload requires 'id'.")
        elif self.action == ExpenseWorkflowAction.DELETE:
            if not expense_payload.get("id") and not expense_payload.get("legacyId"):
                raise ValueError("DELETE expense payload requires 'id' or 'legacyId'.")
        elif self.action == ExpenseWorkflowAction.PROCESS:
            if self.processAction is None:
                raise ValueError("provide 'processAction' for PROCESS actions.")
            if not expense_payload.get("id") and not expense_payload.get("legacyId"):
                raise ValueError("PROCESS expense payload requires 'id' or 'legacyId'.")
        return self

    def _expense_as_dict(self) -> dict[str, Any]:
        if isinstance(self.expense, BaseModel):
            return self.expense.model_dump(mode="json", exclude_none=True)
        payload = dict(self.expense or {})
        # When CREATE is invoked with a raw dict the union accepts it as-is,
        # bypassing ExpenseCreateInput's normalisation (payoutMethod aliasing,
        # incurredAt ISO coercion, RECEIPT/url cross-check). Route it through
        # ExpenseCreateInput now so the GraphQL payload carries canonical values.
        if self.action == ExpenseWorkflowAction.CREATE and payload:
            normalised = ExpenseCreateInput.model_validate(payload)
            return normalised.model_dump(mode="json", exclude_none=True)
        return payload

    def expense_payload(self) -> dict[str, Any]:
        """Return the expense payload as a plain dict suitable for GraphQL variables."""
        return self._expense_as_dict()
