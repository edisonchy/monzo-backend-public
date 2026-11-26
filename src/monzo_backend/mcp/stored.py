"""Stored balance and budget contracts; no bank calls or owner database sessions."""

from datetime import date, datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select

from monzo_backend import balances, budgets
from monzo_backend.models import DashboardRevision

from .contracts import ContextInput, Contract, Identifier
from .cursors import ToolFailure


class BalanceInput(ContextInput):
    account_id: Identifier | None = None
    currency: Literal["GBP"] = "GBP"


class BudgetInput(ContextInput):
    period: str = Field(pattern=r"^(20\d{2}|2100)-(0[1-9]|1[0-2])$")
    account_id: Identifier = "combined"
    currency: Literal["GBP"] = "GBP"


class CurrentBalance(Contract):
    amount_minor: int
    currency: Literal["GBP"]
    basis: Literal["available"]


class Pot(Contract):
    id: str
    name: str
    amount_minor: int
    currency: Literal["GBP"]
    basis: Literal["pot_balance"]


class Pots(Contract):
    pots: list[Pot]


class BalanceDay(Contract):
    day: date
    retrieved_at: datetime
    data: CurrentBalance | Pots


class BalanceItem(Contract):
    account_id: str
    source: Literal["current", "pots"]
    state: Literal["fresh", "stale", "unavailable", "unsupported"]
    last_attempt_at: datetime | None
    fetch_outcome: str | None
    retrieved_at: datetime | None
    provider_observed_at: datetime | None
    data: CurrentBalance | Pots | None
    history: list[BalanceDay]


class BalanceResult(Contract):
    observed_at: datetime
    revision: int
    currency: Literal["GBP"]
    account_id: str | None
    stale_after_minutes: int
    snapshot_timezone: Literal["Europe/London"]
    items: list[BalanceItem]
    excluded: list[str]


class TargetVersion(Contract):
    version: int
    amount_minor: int | None
    created_at: datetime


class BudgetItem(Contract):
    category: str
    target_minor: int | None
    version: int
    spent_minor: int
    remaining_minor: int | None
    pending_minor: int
    credits_minor: int
    history: list[TargetVersion]


class AccountCoverage(Contract):
    account_id: str
    covered_until: datetime | None
    complete: bool


class BudgetCoverage(Contract):
    state: Literal["not_started", "complete", "incomplete"]
    required_until: datetime | None
    accounts: list[AccountCoverage]


class BudgetResult(Contract):
    period: str
    start: datetime
    end: datetime
    account_id: str
    account_ids: list[str]
    currency: Literal["GBP"]
    timezone: Literal["Europe/London"]
    rollover: Literal[False]
    revision: int
    observed_at: datetime
    editable: bool
    coverage: BudgetCoverage
    evidence_missing_count: int
    unmatched_flex_count: int
    items: list[BudgetItem]


class StoredReports:
    def __init__(self, s):
        self.s = s

    def check_revision(self, expected):
        revision = (
            self.s.scalar(select(DashboardRevision.revision).where(DashboardRevision.id == 1)) or 0
        )
        if expected is not None and expected != revision:
            raise ToolFailure(
                "data_changed", "Data changed. Read the latest report before comparing."
            )

    def balances(self, inputs):
        self.check_revision(inputs.expected_revision)
        try:
            return balances.read_report(self.s, account_id=inputs.account_id)
        except HTTPException:
            raise ToolFailure(
                "validation_error",
                "Choose an enabled Current account. Flex balances are unverified and excluded.",
            ) from None

    def budgets(self, inputs):
        self.check_revision(inputs.expected_revision)
        try:
            return budgets.read_report(self.s, inputs.period, inputs.account_id)
        except HTTPException:
            raise ToolFailure(
                "validation_error",
                "Choose a supported owner account and a month between 2000 and 2100.",
            ) from None
