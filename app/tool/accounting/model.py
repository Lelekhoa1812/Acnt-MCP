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


class OpenCollectiveAccountSearchArgs(BaseModel):
    search_term: str = Field(description="Open Collective account search term.")
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


class OpenCollectiveFinancialSnapshotArgs(_OpenCollectiveBaseArgs):
    expense_limit: int = Field(20, ge=1, le=100, description="Maximum expense rows to include.")
    expense_offset: int = Field(0, ge=0, le=10_000, description="Offset into the expense rows.")
    transaction_limit: int = Field(20, ge=1, le=100, description="Maximum transaction rows to include.")
    transaction_offset: int = Field(0, ge=0, le=10_000, description="Offset into the transaction rows.")
    expense_search_term: str | None = Field(None, description="Optional search filter for expenses.")
    transaction_search_term: str | None = Field(None, description="Optional search filter for transactions.")
    include_open_liabilities: bool = Field(True, description="Include derived open-liability summaries.")

    @field_validator("expense_search_term", "transaction_search_term")
    @classmethod
    def _strip_optional_search(cls, value: str | None) -> str | None:
        return _strip_text(value)


class ExpenseWorkflowAction(str, Enum):
    CREATE = "CREATE"
    EDIT = "EDIT"
    DELETE = "DELETE"
    PROCESS = "PROCESS"


class OpenCollectiveExpenseWorkflowArgs(BaseModel):
    action: ExpenseWorkflowAction = Field(description="Expense workflow step to execute.")
    account: AccountReferenceInput | None = Field(None, description="Account receiving the expense for create actions.")
    expense: dict[str, Any] = Field(..., description="Expense payload for create/edit/delete/process actions.")
    privateComment: str | None = Field(None, description="Private comment for create actions.")
    processAction: ExpenseProcessAction | None = Field(None, description="Expense process action for workflow processing.")
    message: str | None = Field(None, description="Optional workflow message.")
    paymentParams: ProcessExpensePaymentParams | None = Field(None, description="Optional payment metadata.")

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
