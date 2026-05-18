from __future__ import annotations

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
    model_config = ConfigDict(extra="allow")
    description: str = Field(..., description="Line-item description.")
    amountV2: AmountInput | None = Field(None, description="Amount for the line item.")
    incurredAt: str | None = Field(None, description="ISO timestamp when the expense occurred.")
    url: str | None = Field(None, description="Optional URL for receipts or metadata.")


class PayoutMethodInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = Field(..., description="Payout method type (PayPal, OTHER, etc.).")
    publicId: str | None = Field(None, description="Existing payout method public id.")
    name: str | None = Field(None, description="Friendly payout method name.")
    data: dict[str, object] | None = Field(None, description="Payout-method specific data.")
    isSaved: bool | None = Field(None, description="Whether to save the payout method.")


class ExpenseCreateInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    description: str = Field(..., description="Expense description.")
    type: str = Field(..., description="Expense type (INVOICE, RECEIPT, etc.).")
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
        return dict(self.expense or {})

    def expense_payload(self) -> dict[str, Any]:
        """Return the expense payload as a plain dict suitable for GraphQL variables."""
        return self._expense_as_dict()
