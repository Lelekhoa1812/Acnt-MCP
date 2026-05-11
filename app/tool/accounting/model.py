from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _strip_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class _OpenCollectiveBaseArgs(BaseModel):
    client_id: str = Field(
        description=(
            "Invoice Ninja client identifier. Accepts the client's UUID hashed id (e.g. 'V2v...'), "
            "the human number (e.g. '0001'), or a name fragment that will be resolved via the clients filter."
        ),
        alias="slug",
        validation_alias="slug",
        serialization_alias="slug",
    )
    limit: int = Field(20, ge=1, le=100, description="Maximum rows to return (mapped to per_page).")
    offset: int = Field(0, ge=0, le=10_000, description="Row offset (translated to page = offset/limit + 1).")

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("client_id")
    @classmethod
    def _strip_client_id(cls, value: str) -> str:
        cleaned = _strip_text(value)
        if not cleaned:
            raise ValueError("client_id (slug) must not be empty.")
        return cleaned


class OpenCollectiveExpenseListArgs(_OpenCollectiveBaseArgs):
    search_term: str | None = Field(None, description="Optional expense search term (matches description, number, vendor).")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        return _strip_text(value)


class OpenCollectiveTransactionAllArgs(_OpenCollectiveBaseArgs):
    search_term: str | None = Field(None, description="Optional bank-transaction search term.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str | None) -> str | None:
        return _strip_text(value)


class OpenCollectiveBudgetLookupArgs(_OpenCollectiveBaseArgs):
    @model_validator(mode="after")
    def _validate(self) -> "OpenCollectiveBudgetLookupArgs":
        return self


class OpenCollectiveAccountSearchArgs(BaseModel):
    search_term: str = Field(description="Invoice Ninja client search term (matches name, number, email, contacts).")
    limit: int = Field(10, ge=1, le=25, description="Maximum matches to return.")
    offset: int = Field(0, ge=0, le=10_000, description="Row offset (translated to page).")
    include_archived: bool = Field(False, description="Include archived/soft-deleted clients in the search.")

    @field_validator("search_term")
    @classmethod
    def _strip_search_term(cls, value: str) -> str:
        cleaned = _strip_text(value)
        if not cleaned:
            raise ValueError("search_term must not be empty.")
        return cleaned


class OpenCollectiveFinancialSnapshotArgs(_OpenCollectiveBaseArgs):
    expense_limit: int = Field(20, ge=1, le=100, description="Maximum expense rows to include.")
    expense_offset: int = Field(0, ge=0, le=10_000, description="Offset into expense rows.")
    transaction_limit: int = Field(20, ge=1, le=100, description="Maximum bank-transaction rows to include.")
    transaction_offset: int = Field(0, ge=0, le=10_000, description="Offset into bank-transaction rows.")
    expense_search_term: str | None = Field(None, description="Optional search filter for expenses.")
    transaction_search_term: str | None = Field(None, description="Optional search filter for bank transactions.")
    include_open_liabilities: bool = Field(True, description="Include derived open-liability summaries (unpaid invoices/expenses).")

    @field_validator("expense_search_term", "transaction_search_term")
    @classmethod
    def _strip_optional_search(cls, value: str | None) -> str | None:
        return _strip_text(value)


class ExpenseWorkflowAction(str, Enum):
    CREATE = "CREATE"
    EDIT = "EDIT"
    DELETE = "DELETE"
    PROCESS = "PROCESS"


class ExpenseProcessAction(str, Enum):
    """Subset of Invoice Ninja bulk actions used to drive expense lifecycle transitions."""

    ARCHIVE = "archive"
    RESTORE = "restore"
    DELETE = "delete"
    MARK_AS_PAID = "mark_paid"
    INVOICE = "invoice_expense"
    EMAIL = "email"
    # Compatibility aliases kept so existing prompts/tests do not break.
    APPROVE = "restore"
    UNAPPROVE = "archive"
    REJECT = "delete"
    PAY = "mark_paid"
    HOLD = "archive"
    RELEASE = "restore"


class AccountReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str | None = Field(None, description="Invoice Ninja client hashed id.")
    client_id: str | None = Field(
        None,
        description="Invoice Ninja client identifier (hashed id, number, or name fragment).",
        alias="slug",
        validation_alias="slug",
        serialization_alias="slug",
    )

    @model_validator(mode="after")
    def _require_identifier(self) -> "AccountReferenceInput":
        if not self.id and not self.client_id:
            raise ValueError("provide at least one of 'id' or 'client_id' (slug).")
        return self


class ExpenseReferenceInput(BaseModel):
    id: str | None = Field(None, description="Invoice Ninja expense hashed id.")
    legacyId: int | None = Field(None, description="Optional numeric expense number when hashed id is unknown.")

    @model_validator(mode="after")
    def _require_identifier(self) -> "ExpenseReferenceInput":
        if not self.id and not self.legacyId:
            raise ValueError("provide 'id' or 'legacyId' to reference an expense.")
        return self


class AmountInput(BaseModel):
    value: float | None = Field(None, description="Amount as a float.")
    currency: str | None = Field(None, description="Currency code (e.g. AUD, USD).")
    valueInCents: int | None = Field(None, description="Amount in minor units (cents).")
    exchangeRate: dict[str, object] | None = Field(None, description="Optional exchange-rate metadata.")


class ExpenseItemCreateInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    description: str = Field(..., description="Line-item description.")
    amountV2: AmountInput | None = Field(None, description="Line-item amount.")
    incurredAt: str | None = Field(None, description="ISO timestamp when the expense item occurred.")
    url: str | None = Field(None, description="Optional URL for receipts or supporting metadata.")


class PayoutMethodInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = Field(..., description="Payment-type name (CHEQUE, BANK_TRANSFER, CREDIT_CARD, OTHER, ...).")
    publicId: str | None = Field(None, description="Optional Invoice Ninja payment_type_id when known.")
    name: str | None = Field(None, description="Friendly payment-method label.")
    data: dict[str, object] | None = Field(None, description="Payment-method specific data (account hints, etc.).")
    isSaved: bool | None = Field(None, description="Persist the payment method on the client for reuse.")


class ExpenseCreateInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    description: str = Field(..., description="Expense description / public notes.")
    type: str = Field(..., description="Free-form expense category label (mapped to category_id when resolved).")
    payee: AccountReferenceInput = Field(..., description="Vendor/client the expense belongs to.")
    payoutMethod: PayoutMethodInput = Field(..., description="How the expense is/was paid.")
    currency: str | None = Field(None, description="Currency code (defaults to the company currency).")
    longDescription: str | None = Field(None, description="Internal/private notes for staff.")
    privateMessage: str | None = Field(None, description="Private note for the vendor.")
    reference: str | None = Field(None, description="Accounting reference / transaction reference number.")
    tags: list[str] | None = Field(None, description="Optional tags (custom_value1-4 fallback).")
    accountingCategory: dict[str, object] | None = Field(None, description="Category reference payload.")
    invoiceInfo: str | None = Field(None, description="Custom invoice information / public_notes.")
    items: list[ExpenseItemCreateInput] | None = Field(None, description="Line items contributing to the total.")


class ExpenseUpdateInput(ExpenseCreateInput):
    id: str = Field(..., description="Invoice Ninja expense hashed id to update.")


class ProcessExpensePaymentParams(BaseModel):
    model_config = ConfigDict(extra="allow")
    clearedAt: str | None = Field(None, description="When funds cleared on the host bank (payment_date).")
    feesPayer: str | None = Field(None, description="Who pays the fees ('CLIENT'/'COMPANY').")
    forceManual: bool | None = Field(None, description="Force manual payment handling.")
    markAsUnPaidStatus: str | None = Field(None, description="Status used when marking unpaid.")
    paymentMethodService: str | None = Field(None, description="Payment service identifier (e.g. 'stripe', 'paypal').")
    paymentProcessorFeeInHostCurrency: int | None = Field(None, description="Processor fee in minor units.")
    shouldRefundPaymentProcessorFee: bool | None = Field(None, description="Refund the processor fee on unpaid.")
    totalAmountPaidInHostCurrency: int | None = Field(None, description="Total paid in minor units (host currency).")
    transfer: dict[str, object] | None = Field(None, description="Transfer details for manual payments.")


class OpenCollectiveExpenseWorkflowArgs(BaseModel):
    action: ExpenseWorkflowAction = Field(description="Expense workflow step to execute.")
    account: AccountReferenceInput | None = Field(None, description="Vendor/client for CREATE actions.")
    expense: dict[str, Any] = Field(..., description="Expense payload for create/edit/delete/process actions.")
    privateComment: str | None = Field(None, description="Optional private comment recorded against the expense.")
    processAction: ExpenseProcessAction | None = Field(None, description="Invoice Ninja bulk action for PROCESS workflows.")
    message: str | None = Field(None, description="Optional message attached to the workflow action.")
    paymentParams: ProcessExpensePaymentParams | None = Field(None, description="Optional payment metadata for mark-as-paid.")

    @model_validator(mode="after")
    def _validate_workflow_shape(self) -> "OpenCollectiveExpenseWorkflowArgs":
        if self.action == ExpenseWorkflowAction.CREATE:
            if self.account is None:
                raise ValueError("provide 'account' for CREATE actions.")
            if not self.expense:
                raise ValueError("provide 'expense' for CREATE actions.")
        elif self.action == ExpenseWorkflowAction.PROCESS:
            if self.processAction is None:
                raise ValueError("provide 'processAction' for PROCESS actions.")
            if not self.expense:
                raise ValueError("provide 'expense' for PROCESS actions.")
        elif self.action in {ExpenseWorkflowAction.EDIT, ExpenseWorkflowAction.DELETE}:
            if not self.expense:
                raise ValueError("provide 'expense' for EDIT and DELETE actions.")
        return self


class OpenCollectiveExpenseCreateArgs(BaseModel):
    account: AccountReferenceInput = Field(..., description="Client the expense is billed against.")
    expense: ExpenseCreateInput = Field(..., description="Expense payload to submit.")
    privateComment: str | None = Field(None, description="Optional private comment attached to the new expense.")


class OpenCollectiveExpenseUpdateArgs(BaseModel):
    expense: ExpenseUpdateInput = Field(..., description="Expense payload with the values to update.")


class OpenCollectiveExpenseDeleteArgs(BaseModel):
    expense: ExpenseReferenceInput = Field(..., description="Expense to delete (soft-delete in Invoice Ninja).")


class OpenCollectiveExpenseProcessArgs(BaseModel):
    expense: ExpenseReferenceInput = Field(..., description="Expense to process via a bulk action.")
    action: ExpenseProcessAction = Field(..., description="Invoice Ninja bulk action to invoke.")
    message: str | None = Field(None, description="Optional log message captured with the action.")
    paymentParams: ProcessExpensePaymentParams | None = Field(None, description="Optional payment metadata for mark-as-paid.")


# Backwards-compatible aliases — keep InvoiceNinja names pointing to OpenCollective for compatibility
InvoiceNinjaExpenseListArgs = OpenCollectiveExpenseListArgs
InvoiceNinjaTransactionAllArgs = OpenCollectiveTransactionAllArgs
InvoiceNinjaBudgetLookupArgs = OpenCollectiveBudgetLookupArgs
InvoiceNinjaAccountSearchArgs = OpenCollectiveAccountSearchArgs
InvoiceNinjaFinancialSnapshotArgs = OpenCollectiveFinancialSnapshotArgs
InvoiceNinjaExpenseWorkflowArgs = OpenCollectiveExpenseWorkflowArgs
InvoiceNinjaExpenseCreateArgs = OpenCollectiveExpenseCreateArgs
InvoiceNinjaExpenseUpdateArgs = OpenCollectiveExpenseUpdateArgs
InvoiceNinjaExpenseDeleteArgs = OpenCollectiveExpenseDeleteArgs
InvoiceNinjaExpenseProcessArgs = OpenCollectiveExpenseProcessArgs
