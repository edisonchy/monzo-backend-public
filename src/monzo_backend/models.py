import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class CustomCategory(Base):
    __tablename__ = "custom_categories"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    identity: Mapped[str] = mapped_column(String(160), unique=True)
    colour: Mapped[str] = mapped_column(String(7))


class Connection(Base):
    __tablename__ = "bank_connections"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    user_id: Mapped[str] = mapped_column(String(128))
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(40), default="awaiting_approval")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchedulerHeartbeat(Base):
    __tablename__ = "monzo_scheduler_heartbeats"
    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NightlySyncReceipt(Base):
    __tablename__ = "monzo_nightly_sync_receipts"
    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    job_ids: Mapped[list[str]] = mapped_column(JSONB)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("bank_connections.id"))
    kind: Mapped[str] = mapped_column(String(80), default="unknown")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_status: Mapped[str] = mapped_column(String(40), default="not_started")


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    merchant_id: Mapped[str | None] = mapped_column(String(128))
    merchant_name: Mapped[str] = mapped_column(String(500), default="")
    counterparty_name: Mapped[str | None] = mapped_column(String(500))
    payment_name: Mapped[str | None] = mapped_column(String(2000))
    payment_name_source: Mapped[str | None] = mapped_column(String(40))
    identity_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identity_retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identity_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    payment_group_identity: Mapped[str | None] = mapped_column(
        ForeignKey("merchant_identities.merchant_id")
    )
    counterparty_key: Mapped[str | None] = mapped_column(String(64))
    purpose_confirmation: Mapped[dict | None] = mapped_column(JSONB)
    bank_category: Mapped[str] = mapped_column(String(100), default="general")
    reporting_facts: Mapped[dict | None] = mapped_column(JSONB)
    reporting_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transaction_type: Mapped[str] = mapped_column(String(40), default="unknown")
    pending_check_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    category: Mapped[str | None] = mapped_column(String(100))
    category_source: Mapped[str | None] = mapped_column(String(40))
    category_status: Mapped[str] = mapped_column(String(40), default="pending")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    @hybrid_property
    def category_identity(self):
        return self.payment_group_identity or self.merchant_id

    @category_identity.inplace.expression
    @classmethod
    def _category_identity_expression(cls):
        return func.coalesce(cls.payment_group_identity, cls.merchant_id)


class PaymentGroupMatcher(Base):
    __tablename__ = "payment_group_matchers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    identity_id: Mapped[str] = mapped_column(ForeignKey("merchant_identities.merchant_id"))
    evidence: Mapped[dict] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PaymentPurposeRule(Base):
    __tablename__ = "payment_purpose_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    counterparty_key: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    purpose: Mapped[str] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("account_id", "counterparty_key", "direction"),)


class Rule(Base):
    __tablename__ = "category_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    merchant_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("merchant_groups.id"), unique=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(100))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    queue: Mapped[str] = mapped_column(String(30), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    dedupe_key: Mapped[str] = mapped_column(String(240), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Control(Base):
    __tablename__ = "processing_control"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(30), default="running")
    active_job_id: Mapped[str | None] = mapped_column(String(36))
    incident_id: Mapped[str | None] = mapped_column(String(36))
    recovery_attempt: Mapped[int] = mapped_column(Integer, default=0)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Incident(Base):
    __tablename__ = "processing_incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_key: Mapped[str] = mapped_column(String(240), unique=True)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attempt(Base):
    __tablename__ = "classification_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String(36))
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"))
    input_version: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(200), default="")
    policy_version: Mapped[str] = mapped_column(String(40), default="v1")
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("transaction_id", "payload_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    transaction_id: Mapped[str] = mapped_column(String(128))
    account_id: Mapped[str | None] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MerchantDecision(Base):
    __tablename__ = "merchant_decisions"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MerchantResearch(Base):
    __tablename__ = "merchant_research"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_name: Mapped[str] = mapped_column(String(160))
    finding: Mapped[dict] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    credential_tag: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginWindow(Base):
    __tablename__ = "browser_login_window"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)


class LoginBucket(Base):
    __tablename__ = "browser_login_buckets"
    client_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer)


class DashboardRevision(Base):
    __tablename__ = "dashboard_revision"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    transaction_id: Mapped[str] = mapped_column(String(32))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MerchantGroup(Base):
    __tablename__ = "merchant_groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, default=1)
    merged_into: Mapped[str | None] = mapped_column(ForeignKey("merchant_groups.id"))


class MerchantIdentity(Base):
    __tablename__ = "merchant_identities"
    merchant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("merchant_groups.id"), index=True)


class MerchantAlias(Base):
    __tablename__ = "merchant_aliases"
    name: Mapped[str] = mapped_column(String(500), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("merchant_groups.id"), index=True)


class ReportingCoverage(Base):
    __tablename__ = "reporting_coverage"
    __table_args__ = (UniqueConstraint("account_id", "start_at", "end_at", "source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CategoryWriteControl(Base):
    __tablename__ = "category_write_control"
    __table_args__ = (CheckConstraint("id = 1", name="category_control_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class CategoryOperation(Base):
    __tablename__ = "category_operations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('apply', 'undo') AND origin = 'mcp'", name="category_operation_kind"
        ),
        CheckConstraint(
            "state IN ('prepared', 'committed', 'stale', 'expired')",
            name="category_operation_state",
        ),
        CheckConstraint("expires_at > created_at", name="category_operation_lifetime"),
        CheckConstraint(
            "(state = 'committed') = (receipt IS NOT NULL AND committed_at IS NOT NULL)",
            name="category_operation_receipt",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    issuer: Mapped[str] = mapped_column(String(512))
    owner_subject: Mapped[str] = mapped_column(String(512))
    authorized_client: Mapped[str] = mapped_column(String(512))
    origin: Mapped[str] = mapped_column(String(16), default="mcp")
    kind: Mapped[str] = mapped_column(String(16))
    command: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="prepared")
    error_code: Mapped[str | None] = mapped_column(String(40))
    source_batch_id: Mapped[str | None] = mapped_column(ForeignKey("audit_events.id"))
    undone_batch_id: Mapped[str | None] = mapped_column(ForeignKey("audit_events.id"), unique=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("audit_events.id"), index=True)
    receipt: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CategoryRateLimit(Base):
    __tablename__ = "category_rate_limits"
    owner_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), primary_key=True)
    attempts: Mapped[list] = mapped_column(JSONB)
    credits: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BalanceObservation(Base):
    __tablename__ = "balance_observations"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(20))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict | None] = mapped_column(JSONB)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB)


class BudgetVersion(Base):
    """Append-only targets. Empty category denotes the overlapping overall envelope."""

    __tablename__ = "budget_versions"
    __table_args__ = (
        CheckConstraint("currency = 'GBP'", name="budget_currency"),
        CheckConstraint("version > 0", name="budget_version_positive"),
        CheckConstraint(
            "amount_minor IS NULL OR amount_minor BETWEEN 0 AND 1000000000000", name="budget_amount"
        ),
        CheckConstraint("EXTRACT(DAY FROM month) = 1", name="budget_month_start"),
    )
    connection_id: Mapped[int] = mapped_column(ForeignKey("bank_connections.id"), primary_key=True)
    account_scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    category: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
