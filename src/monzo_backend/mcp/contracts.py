"""Public tool contracts. Dates are London calendar dates; money is minor units."""

from datetime import date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from monzo_backend.domain import CATEGORIES
from monzo_backend.models import now

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Dimension = Literal["category", "merchant_group", "account"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextInput(Contract):
    expected_revision: int | None = Field(None, ge=0, strict=True)


class Scope(ContextInput):
    date_from: date
    date_to: date
    account_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    currency: str = Field("GBP", pattern=r"^[A-Z]{3}$")
    categories: list[str] = Field(default_factory=list, max_length=50)
    merchant_group_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    category_status: Literal["all", "assigned", "needs_review", "pending", "excluded"] = "all"

    @model_validator(mode="after")
    def check_scope(self):
        today = now().astimezone(ZoneInfo("Europe/London")).date()
        if self.date_from.year < 2000 or not self.date_from <= self.date_to <= today:
            raise ValueError("Choose dates between 2000 and today, with start no later than end")
        if (self.date_to - self.date_from).days > 3660:
            raise ValueError("Choose a range of at most ten years")
        if set(self.categories) - set((*CATEGORIES, "uncategorised")):
            raise ValueError("Unknown category")
        self.account_ids = sorted(set(self.account_ids))
        self.categories = sorted(set(self.categories))
        self.merchant_group_ids = sorted(set(self.merchant_group_ids))
        return self


class Page(Scope):
    limit: int = Field(25, ge=1, le=100, strict=True)
    cursor: str | None = Field(None, max_length=4000)


class BreakdownInput(Page):
    dimensions: list[Dimension] = Field(
        default_factory=lambda: ["category"], min_length=1, max_length=2
    )
    metric: Literal["spending", "money_in", "count"] = "spending"
    sort: Literal["largest", "smallest"] = "largest"

    @model_validator(mode="after")
    def unique_dimensions(self):
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("Choose distinct dimensions")
        return self


class SeriesInput(Scope):
    grain: Literal["day", "week", "month"] = "day"
    cumulative: bool = False
    profile: Literal["weekday", "day_of_month"] | None = None

    @model_validator(mode="after")
    def profile_options(self):
        if self.profile and (self.grain != "day" or self.cumulative):
            raise ValueError("Profiles require incremental daily values")
        return self


class CompareInput(Contract):
    selected: Scope
    previous: Scope
    alignment: Literal["full_period", "elapsed_period"] = "full_period"
    attribution: Dimension | None = None

    @model_validator(mode="after")
    def comparable(self):
        a = self.selected.model_dump(exclude={"date_from", "date_to", "expected_revision"})
        b = self.previous.model_dump(exclude={"date_from", "date_to", "expected_revision"})
        if a != b:
            raise ValueError("Comparison scopes must match except for dates")
        return self


class SearchInput(Page):
    reported_spending_only: bool = False
    mode: Literal["payments", "summary"] = "payments"
    query: str = Field("", max_length=200)
    min_amount_minor: int | None = Field(None, ge=0, le=10**11, strict=True)
    max_amount_minor: int | None = Field(None, ge=0, le=10**11, strict=True)
    direction: Literal["all", "out", "in"] = "all"
    settlement: Literal["all", "settled", "pending", "declined"] = "all"
    source: Literal["all", "ai", "manual", "rule", "monzo", "review"] = "all"
    sort: Literal["newest", "oldest"] = "newest"

    @model_validator(mode="after")
    def amount_range(self):
        if self.mode == "summary" and (self.cursor or not self.reported_spending_only):
            raise ValueError("Summary requires reported spending and no page cursor")
        if self.min_amount_minor is not None and self.max_amount_minor is not None:
            if self.min_amount_minor > self.max_amount_minor:
                raise ValueError("Minimum amount exceeds maximum")
        return self


class DetailInput(ContextInput):
    transaction_id: Identifier


class Coverage(Contract):
    account_id: str
    status: Literal["complete", "partial", "unknown"]
    intervals: list[tuple[str, str]]
    latest_observation: str | None = Field(description="When coverage evidence was recorded.")
    checked_through: str | None = Field(
        description="Use for payment data freshness. End of the latest verified bank search window, or null when unknown. Consult intervals for gaps."
    )


class Metadata(Contract):
    schema_version: Literal[1] = 1
    effective_filters: dict
    reporting_basis: str
    currency: str | None
    generated_at: str
    data_revision: int
    latest_sync: dict[str, str | None] = Field(
        description="When the sync worker last finished for each account. Use coverage.checked_through for bank data freshness."
    )
    coverage: list[Coverage]
    complete: bool
    limitations: list[str]


class Money(Contract):
    spending_minor: int
    money_in_minor: int
    count: int
    outgoing_count: int
    mean_outgoing_minor: str | None
    excluded_count: int
    excluded_outgoing_minor: int
    pending_outgoing_minor: int
    pending_count: int
    declined_count: int


class SummaryResult(Contract):
    metadata: Metadata
    totals: Money


class AccountResult(Contract):
    id: str
    kind: str
    currencies: list[str]


class ContextResult(Contract):
    metadata: Metadata
    accounts: list[AccountResult]
    categories: list[str]
    capabilities: list[str]
    definitions: dict[str, str]


class BreakdownRow(Contract):
    keys: list[str]
    labels: list[str]
    value: int
    count: int
    share_percent: str | None


class BreakdownResult(Contract):
    metadata: Metadata
    rows: list[BreakdownRow]
    denominator: int
    matching_groups: int
    next_cursor: str | None


class Bucket(Contract):
    date: str
    spending_minor: int
    money_in_minor: int
    count: int
    suggested_count: int
    uncategorised_count: int
    coverage_complete: bool


class ProfileBucket(Contract):
    key: int
    observed_days: int
    mean_spending_minor: str | None


class SeriesResult(Contract):
    metadata: Metadata
    buckets: list[Bucket]
    profile: list[ProfileBucket]


class Contributor(Contract):
    key: str
    label: str
    difference_minor: int


class CompareResult(Contract):
    metadata: Metadata
    selected: SummaryResult
    previous: SummaryResult
    difference_minor: int
    change_percent: str | None
    contributors: list[Contributor]


class PurposeConfirmation(Contract):
    purpose: str
    label: str
    source: Literal["owner_confirmed"]
    scope: Literal["payment", "recipient"]
    confirmed_at: str


class PaymentResult(Contract):
    pending_card: bool = False
    id: str
    account_id: str
    merchant: str
    merchant_group_id: str | None
    merchant_id: str | None = None
    category_identity_id: str | None = None
    directional_group: bool = False
    amount_minor: int
    currency: str
    created_at: str
    settled_at: str | None
    declined: bool
    category: str
    category_source: str | None
    purpose_confirmation: PurposeConfirmation | None = None
    category_status: str
    included_in_combined_spending: bool
    exclusion_reason: str | None
    matched_transaction_id: str | None
    reporting_evidence_available: bool
    description: str | None = None
    version: int
    category_edit_eligible: bool
    category_edit_reason: str | None


class SearchSpending(Contract):
    pending_card_minor: int | None = None
    pending_card_count: int | None = None
    verified_until: str | None = None
    recorded_total_minor: int
    largest_minor: int | None


class SearchResult(Contract):
    summary: SearchSpending | None = None
    metadata: Metadata
    rows: list[PaymentResult]
    matching_count: int
    next_cursor: str | None


class DetailResult(Contract):
    metadata: Metadata
    payment: PaymentResult


class RuleResult(Contract):
    id: str
    merchant_id: str | None
    group_id: str | None
    category: str
    effective_from: str | None


class QualityRow(Contract):
    group_key: str
    merchant: str
    count: int
    suggested_count: int
    uncategorised_count: int
    missing_evidence_count: int
    outgoing_minor: int
    suggested_categories: list[str]
    conflicting_suggestions: bool
    rules: list[RuleResult]


class QualityResult(Contract):
    metadata: Metadata
    rows: list[QualityRow]
    matching_groups: int
    next_cursor: str | None


class RecurringInput(Page):
    direction: Literal["out", "in", "all"] = "out"

    @model_validator(mode="after")
    def lookback(self):
        if not 90 <= (self.date_to - self.date_from).days + 1 <= 730:
            raise ValueError("Recurring analysis requires 90–730 days")
        return self


class UnusualInput(Page):
    baseline_from: date
    baseline_to: date
    kinds: list[
        Literal["large_payment", "spending_spike", "possible_duplicate", "new_merchant"]
    ] = Field(
        default_factory=lambda: [
            "large_payment",
            "spending_spike",
            "possible_duplicate",
            "new_merchant",
        ],
        min_length=1,
        max_length=4,
    )

    @model_validator(mode="after")
    def baseline(self):
        if (
            self.baseline_from.year < 2000
            or not self.baseline_from <= self.baseline_to < self.date_from
        ):
            raise ValueError("Baseline must precede the analysis range")
        if not 90 <= (self.baseline_to - self.baseline_from).days + 1 <= 730:
            raise ValueError("Baseline must cover 90–730 days")
        self.kinds = sorted(set(self.kinds))
        return self


class Finding(Contract):
    id: str
    direction: Literal["out", "in", "mixed"]
    kind: str
    merchant_group_key: str | None
    merchant: str | None
    evidence_ids: list[str]
    occurrence_count: int
    first_date: str
    last_date: str
    amount_minor: int
    minimum_minor: int | None = None
    maximum_minor: int | None = None
    cadence: str | None = None
    next_date_window: tuple[str, str] | None = None
    baseline_observations: int | None = None
    threshold_minor: str | None = None
    explanation: str


class PatternResult(Contract):
    metadata: Metadata
    rule_version: Literal["patterns-v1"] = "patterns-v1"
    findings: list[Finding]
    matching_count: int
    next_cursor: str | None


class CategoryOptionsInput(Contract):
    model_config = ConfigDict(extra="forbid", strict=True)


class CategoryChangeInput(CategoryOptionsInput):
    transaction_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    category: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)


class CategoryBatchInput(CategoryChangeInput):
    merchant_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Exact merchant_id or category_identity_id returned by get_transaction; payment-group identities apply only to their payment direction.",
    )
    merchant_group_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    scope: Literal["this", "unverified", "all"] = "unverified"
    future_rule: bool = False


class CategoryUndoInput(CategoryOptionsInput):
    batch_id: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class CategoryOperationInput(CategoryOptionsInput):
    operation_id: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class CategoryOption(Contract):
    id: str
    label: str


class CategoryOptionsResult(Contract):
    categories: list[CategoryOption] = Field(max_length=100)
    writes_enabled: bool
    can_write: bool
    scope: Literal["this"]
    future_rules: bool
    batch_scopes: list[Literal["unverified", "all"]] = Field(default_factory=list)
    batch_limit: int = 0
    group_scope: Literal["unverified"] = "unverified"


class CategoryPreviewResult(Contract):
    operation_id: str
    kind: Literal["apply"]
    state: Literal["prepared"]
    expires_at: str
    transaction_id: str
    previous: dict
    category: str
    expected_version: int
    verified_replaced: bool
    pending_settlement: bool
    changed: int
    future_rule: Literal[False]


class CategoryBatchPreviewResult(Contract):
    operation_id: str
    kind: Literal["apply"]
    state: Literal["prepared"]
    expires_at: str
    transaction_id: str
    merchant_id: str
    merchant_group_id: str | None
    scope: Literal["this", "unverified", "all"]
    category: str
    expected_version: int
    counts: dict
    account_ids: list[str] = Field(max_length=100)
    payments: list[dict] = Field(max_length=100)
    future_rule: bool
    rule_target: str | None
    rules_before: dict
    proposed_rule: dict | None
    later_arrivals_excluded: Literal[True]


class CategoryUndoPreviewResult(Contract):
    operation_id: str
    kind: Literal["undo"]
    state: Literal["prepared"]
    expires_at: str
    batch_id: str
    transaction_id: str
    previous: dict
    restore: dict
    resumes_classification: bool
    payments: list[dict] = Field(default_factory=list, max_length=100)
    rules_before: dict = Field(default_factory=dict)
    rules_restore: dict = Field(default_factory=dict)
    future_rule: bool = False


class CategoryOperationResult(Contract):
    operation_id: str
    kind: Literal["apply", "undo"]
    state: Literal["prepared", "committed", "stale", "expired"]
    error: str | None
    receipt: dict | None
    current_revision: int
