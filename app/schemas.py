from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ProductComponentAllocationDto(BaseModel):
    componentId: str
    quantity: int


class ProductVariantDetailsDto(BaseModel):
    departmentId: int | None = None
    subDepartmentId: int | None = None
    isActive: bool | None = None
    generalRate: float | None = None
    expoRate: float | None = None
    assignedCategoryId: str | None = None
    dimensional: bool | None = None
    canBeSoldInPortions: bool | None = None
    startDate: str | None = None
    endDate: str | None = None
    salesNote: str | None = None
    length: float | None = None
    width: float | None = None
    height: float | None = None
    vicStock: int | None = None
    vicHirable: int | None = None
    nswStock: int | None = None
    nswHirable: int | None = None
    qldStock: int | None = None
    qldHirable: int | None = None
    totalStock: int | None = None
    lastUpdatedDate: str | None = None
    imageFileName: str | None = None
    cost: float | None = None
    components: list[ProductComponentAllocationDto] = Field(default_factory=list)


class ProductVariationOptionDto(BaseModel):
    id: str
    name: str | None = None
    sortOrder: int | None = None


class ProductVariationDto(BaseModel):
    id: str | None = None
    name: str | None = None
    sortOrder: int | None = None
    options: list[ProductVariationOptionDto] = Field(default_factory=list)


class ProductVariantDto(BaseModel):
    id: str
    name: str | None = None
    sku: str | None = None
    totalHirable: int | None = None
    optionIds: list[str] = Field(default_factory=list)
    details: ProductVariantDetailsDto | None = None


class ProductListItemDto(BaseModel):
    id: str
    name: str | None = None
    departmentId: int
    subDepartmentId: int | None = None
    categoryId: str
    isActive: bool
    variations: list[ProductVariationDto] = Field(default_factory=list)
    variants: list[ProductVariantDto] = Field(default_factory=list)


class ProductListItemDtoPagedResponse(BaseModel):
    items: list[ProductListItemDto] = Field(default_factory=list)
    page: int = 1
    pageSize: int = 20
    totalCount: int = 0
    totalPages: int = 0


class StockApiSubDepartmentDto(BaseModel):
    id: int
    name: str
    isActive: bool = True
    sortOrder: int = 0


class StockApiDepartmentDto(BaseModel):
    id: int
    name: str
    isActive: bool = True
    sortOrder: int = 0
    subDepartments: list[StockApiSubDepartmentDto] = Field(default_factory=list)


class StockCategoryDto(BaseModel):
    id: str
    name: str
    departmentId: int
    parentStockCategoryId: str | None = None
    categoryType: str = "unknown"
    sortOrder: int = 0


class StockCategoryDtoPagedResponse(BaseModel):
    items: list[StockCategoryDto] = Field(default_factory=list)
    page: int = 1
    pageSize: int = 20
    totalCount: int = 0
    totalPages: int = 0


class PricingSnapshot(BaseModel):
    generalRate: float | None = None
    expoRate: float | None = None
    cost: float | None = None


class DimensionsSnapshot(BaseModel):
    dimensional: bool | None = None
    canBeSoldInPortions: bool | None = None
    length: float | None = None
    width: float | None = None
    height: float | None = None


class StockSnapshot(BaseModel):
    totalHirable: int | None = None
    vicStock: int | None = None
    vicHirable: int | None = None
    nswStock: int | None = None
    nswHirable: int | None = None
    qldStock: int | None = None
    qldHirable: int | None = None
    totalStock: int | None = None


class LifecycleSnapshot(BaseModel):
    isActive: bool | None = None
    startDate: str | None = None
    endDate: str | None = None
    lastUpdatedDate: str | None = None


class MediaSnapshot(BaseModel):
    imageFileName: str | None = None
    imageUrl: str | None = None


class ProvenanceSnapshot(BaseModel):
    tool: str
    matched_on: list[str] = Field(default_factory=list)
    confidence: float | None = None
    source_path: str | None = None


class NormalizedEvidence(BaseModel):
    entity_level: Literal["variant", "product"] = "variant"
    product_id: str | None = None
    product_name: str | None = None
    variant_id: str | None = None
    variant_name: str | None = None
    sku: str | None = None
    variation_options: list[str] = Field(default_factory=list)
    salesNote: str | None = None
    departmentId: int | None = None
    subDepartmentId: int | None = None
    categoryId: str | None = None
    isActive: bool | None = None
    pricing: PricingSnapshot = Field(default_factory=PricingSnapshot)
    dimensions: DimensionsSnapshot = Field(default_factory=DimensionsSnapshot)
    stock: StockSnapshot = Field(default_factory=StockSnapshot)
    lifecycle: LifecycleSnapshot = Field(default_factory=LifecycleSnapshot)
    media: MediaSnapshot = Field(default_factory=MediaSnapshot)
    components: list[ProductComponentAllocationDto] = Field(default_factory=list)
    provenance: ProvenanceSnapshot
    evidence_paths: dict[str, str] = Field(default_factory=dict)


class InventorySnapshotRow(BaseModel):
    product: str | None = None
    variant: str | None = None
    sku: str | None = None
    attributeEvidence: list[str] = Field(default_factory=list)
    size: str | None = None
    stock: str | None = None
    knownSpecs: list[str] = Field(default_factory=list)


class InventorySnapshotCoverage(BaseModel):
    requestedPage: int = 1
    requestedPageSize: int = 20
    matchedProducts: int = 0
    matchedPages: int = 0
    enrichedProducts: int = 0
    enrichedVariants: int = 0
    isPartial: bool = False
    limitations: list[str] = Field(default_factory=list)


class InventorySnapshotResponse(BaseModel):
    rows: list[InventorySnapshotRow] = Field(default_factory=list)
    evidence: list[NormalizedEvidence] = Field(default_factory=list)
    coverage: InventorySnapshotCoverage = Field(default_factory=InventorySnapshotCoverage)


class CandidateOption(BaseModel):
    candidate_id: str
    label: str
    confidence: float
    matched_on: list[str] = Field(default_factory=list)
    product_id: str | None = None
    variant_id: str | None = None
    sku: str | None = None
    evidence_summary: str | None = None


class ClarificationPayload(BaseModel):
    status: Literal["needs_clarification"] = "needs_clarification"
    question: str
    options: list[CandidateOption]
    total_matches: int | None = None
    selection_mode: Literal["select_option", "refine_query"] | None = None
    hints: list[str] = Field(default_factory=list)


class PlanValidation(BaseModel):
    expected_rows: int | None = None
    actual_rows: int | None = None
    cache_status: str | None = None
    findings: list[str] = Field(default_factory=list)
    ambiguity: list[str] = Field(default_factory=list)
    missing_statistics: list[str] = Field(default_factory=list)
    confidence: float | None = None


class PlanStep(BaseModel):
    id: int
    name: str
    tool: str
    status: Literal["planned", "pending", "in-progress", "done"] = "planned"
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)
    parallel_group: int | None = None
    hypotheses: list[str] = Field(default_factory=list)
    validation: PlanValidation | None = None


class MemoEntry(BaseModel):
    step_id: int | None = None
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    aggregates: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class MemoCache(BaseModel):
    entries: list[MemoEntry] = Field(default_factory=list)
    aggregates: dict[str, Any] = Field(default_factory=dict)


class PlanMetadata(BaseModel):
    sorted_priorities: list[int] = Field(default_factory=list)
    confidence_scores: dict[str, float] = Field(default_factory=dict)
    validation_findings: list[str] = Field(default_factory=list)


class PlanStatus(BaseModel):
    goal: str
    intent_classes: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    memo: MemoCache = Field(default_factory=MemoCache)
    status: Literal["in-progress", "complete", "blocked", "needs_clarification", "error"] = "in-progress"


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class SessionState(BaseModel):
    session_id: str
    session_name: str | None = None
    session_name_source: Literal["fallback", "llm"] | None = None
    recent_product_names: list[str] = Field(default_factory=list)
    recent_resolved_identifiers: list[str] = Field(default_factory=list)
    last_candidate_list: list[CandidateOption] = Field(default_factory=list)
    last_filters: dict[str, Any] = Field(default_factory=dict)
    preferences: dict[str, Any] = Field(default_factory=dict)
    plan_todo: list[PlanStep] = Field(default_factory=list)
    memo_cache: MemoCache = Field(default_factory=MemoCache)
    plan_metadata: PlanMetadata = Field(default_factory=PlanMetadata)
    current_plan: PlanStatus | None = None
    name_assigned: bool = False
    conversation_history: list[ConversationTurn] = Field(default_factory=list, exclude=True)


class ThoughtBlock(BaseModel):
    goal: str
    entity_guess: Literal["product", "variant", "category", "department", "weather", "news", "currency", "unknown"]
    strategy: str
    tool: str
    args_draft: dict[str, Any] = Field(default_factory=dict)
    risk: str = "none"

    def to_xml(self) -> str:
        return (
            "<thought>\n"
            f"goal: {self.goal}\n"
            f"entity_guess: {self.entity_guess}\n"
            f"strategy: {self.strategy}\n"
            f"tool: {self.tool}\n"
            f"args_draft: {self.args_draft}\n"
            f"risk: {self.risk}\n"
            "</thought>"
        )


class ToolTrace(BaseModel):
    thought: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str
    cache_status: str | None = None
    source_data: str | None = None
    result_count: int | None = None
    normalization_notes: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    tool: str
    status: str = "ok"
    data: Any
    llm_content: Any | None = Field(default=None, exclude=True)
    normalization_notes: list[str] = Field(default_factory=list)
    trace: ToolTrace | None = None
    plan_status: PlanStatus | None = None
    memo_update: MemoEntry | None = None
    validation: PlanValidation | None = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class AgentDebugIntent(BaseModel):
    current_goal: str
    primary_entity_guess: str = "unknown"
    requested_attributes: list[str] = Field(default_factory=list)
    inferred_filters: dict[str, Any] = Field(default_factory=dict)
    scope_status: str = "stock_supported"


class AgentDebugPlanStep(BaseModel):
    id: int
    name: str
    tool: str
    status: str
    depends_on: list[int] = Field(default_factory=list)
    parallel_group: int | None = None


class AgentDebugPlan(BaseModel):
    goal: str
    status: str
    ready_steps: list[int] = Field(default_factory=list)
    blocked_steps: list[int] = Field(default_factory=list)
    dag: list[AgentDebugPlanStep] = Field(default_factory=list)
    next_hop_rules: list[str] = Field(default_factory=list)


class AgentDebugTraceSummary(BaseModel):
    tool: str
    status: str
    result_count: int | None = None
    cache_status: str | None = None


class AgentDebugParallelBatch(BaseModel):
    batch_id: int
    execution_mode: Literal["parallel", "sequential"] = "parallel"
    tools: list[str] = Field(default_factory=list)
    step_ids: list[int] = Field(default_factory=list)


class AgentDebugRetrieval(BaseModel):
    thought_blocks: list[str] = Field(default_factory=list)
    trace_summary: list[AgentDebugTraceSummary] = Field(default_factory=list)
    parallel_batches: list[AgentDebugParallelBatch] = Field(default_factory=list)


class AgentDebugGrounding(BaseModel):
    resolved_identifiers: list[str] = Field(default_factory=list)
    evidence_count: int = 0
    unresolved_attributes: list[str] = Field(default_factory=list)
    user_impact_limitations: list[str] = Field(default_factory=list)


class AgentDebugPayload(BaseModel):
    intent: AgentDebugIntent
    plan: AgentDebugPlan
    retrieval: AgentDebugRetrieval
    grounding: AgentDebugGrounding


class CallToolRequest(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    sessionId: str | None = None


class AgentQueryRequest(BaseModel):
    message: str
    sessionId: str | None = None
    renderMockUi: bool = False
    includeThoughts: bool = True
    preferences: dict[str, Any] = Field(default_factory=dict)


class AgentQueryResponse(BaseModel):
    status: Literal["answered", "needs_clarification", "out_of_scope", "limited", "error"]
    answer: str
    thoughts: list[str] = Field(default_factory=list)
    debug: AgentDebugPayload | None = None
    tool_trace: list[ToolTrace] = Field(default_factory=list)
    clarification: ClarificationPayload | None = None
    resolved_items: list[NormalizedEvidence] = Field(default_factory=list)
    session_state: SessionState
    plan_status: PlanStatus | None = None
    mock_ui: str | None = None
    mock_ui_path: str | None = None
    limitations: list[str] = Field(default_factory=list)


class StockGetDepartmentsArgs(BaseModel):
    includeInactive: bool = False
    includeSubDepartments: bool = False


class StockGetCategoriesArgs(BaseModel):
    page: int = Field(1, ge=1)
    pageSize: int = Field(20, ge=1, le=100)


class StockSearchCatalogueArgs(BaseModel):
    page: int = Field(1, ge=1)
    pageSize: int = Field(20, ge=1, le=100)
    search: str | None = None
    departmentId: int | None = None
    categoryId: str | None = None


class StockGetProductArgs(BaseModel):
    id: str | None = None
    sku: str | None = None
    page: int = Field(1, ge=1)
    pageSize: int = Field(20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_identifier(self) -> "StockGetProductArgs":
        if not self.id and not self.sku:
            raise ValueError("Either 'id' or 'sku' must be provided.")
        return self


class StockExtractVariantEvidenceArgs(BaseModel):
    id: str | None = None
    sku: str | None = None
    variantId: str | None = None

    @model_validator(mode="after")
    def validate_identifier(self) -> "StockExtractVariantEvidenceArgs":
        if not self.id and not self.sku and not self.variantId:
            raise ValueError("At least one of 'id', 'sku', or 'variantId' must be provided.")
        if self.variantId and not self.id and not self.sku:
            raise ValueError("`variantId` requires accompanying `sku` or `id` for product resolution.")
        return self


class StockCompareVariantsArgs(BaseModel):
    # Motivation vs Logic: catalogue products can have >5 colour/SKU variants; a low cap
    # caused validation errors when comparing full variant sets. The service semaphore
    # (HTH_STOCK_PARALLEL_REQUESTS_LIMIT) still bounds concurrent Harmonise fan-out.
    identifiers: list[str] = Field(min_length=2, max_length=20)


class StockInventorySnapshotArgs(BaseModel):
    page: int = Field(1, ge=1)
    pageSize: int = Field(20, ge=1, le=100)
    search: str | None = None
    departmentId: int | None = None
    categoryId: str | None = None


class ResolverDisambiguateCandidatesArgs(BaseModel):
    query: str
    limit: int = Field(10, ge=2, le=10)
    departmentId: int | None = None
    categoryId: str | None = None


class SessionToolArgs(BaseModel):
    sessionId: str | None = None
