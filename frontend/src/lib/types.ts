export type View =
  "spending" | "transactions" | "chat" | "review" | "settings" | "balances" | "budgets";
export type Scope = {
  review_sort?: "name" | "frequency" | "value";
  review_tab?: "ai" | "needs_review" | "payments";
  account_id: string;
  period: string;
  categories?: string[];
  merchants?: string[];
  accounts?: string[];
  min_amount?: string;
  max_amount?: string;
  date_from?: string;
  date_to?: string;
  mode: "month" | "year";
  currency: string;
  category: string;
  merchant?: string;
  q: string;
  status: string;
  source: string;
  settlement: string;
  offset: number;
};
export type Bootstrap = {
  category_colours?: Record<string, string>;
  today: string;
  categories: string[];
  default_account_id: string | null;
  accounts: {
    id: string;
    kind: string;
    history_status: string;
    currencies: string[];
    recorded_years?: Record<string, number[]>;
  }[];
};
export type Status = {
  observed_at: string;
  stale_after_minutes: number;
  accounts: {
    id: string;
    kind: string;
    state: string;
    last_sync_at: string | null;
    checked_through: string | null;
    history_status: string;
    last_webhook_at: string | null;
  }[];
  queues: Record<"sync" | "category", QueueHealth>;
  connection: {
    state: string;
    last_sync_at: string | null;
    checked_through: string | null;
    oauth_configured: boolean;
    reconnect_state: string;
  };
  categorisation: { state: string; pending: number };
  notifications: {
    pending: number;
    state: string;
    max_attempts: number;
    oldest_pending_at: string | null;
    last_acknowledged_at: string | null;
  };
  webhooks?: { kind: string; last_received_at: string | null }[];
  ai_enabled: boolean;
  ai_auto_accept: boolean;
};
export type Payment = {
  id: string;
  account_id: string;
  merchant: string;
  merchant_id: string | null;
  category_identity_id?: string | null;
  directional_group?: boolean;
  description: string;
  amount_minor: number;
  currency: string;
  created_at: string;
  settled_at: string | null;
  declined: boolean;
  bank_category: string;
  category: string | null;
  category_source: string | null;
  purpose_confirmation?: {
    purpose: string;
    label: string;
    source: string;
    scope: string;
  } | null;
  category_status: string;
  version: number;
  card_verification: boolean;
  combined_exclusion: { reason: string } | null;
  reporting_evidence_available: boolean;
};
export type Rule = {
  id: string;
  merchant: string;
  category: string;
  effective_from: string | null;
};
export type Detail = Payment & {
  rule: Pick<Rule, "category"> | null;
  research: {
    finding: { summary?: string; source_urls?: string[] };
    created_at: string;
    model: string;
  } | null;
};
export type Payments = {
  next_cursor?: string | null;
  items: Payment[];
  total: number;
  limit: number;
  offset: number;
};
export type Totals = {
  debits_minor: number;
  credits_minor: number;
  count: number;
  series: number[];
  bucket_categories?: string[][];
  excluded_count: number;
  excluded_debits_minor: number;
  evidence_missing_count: number;
  unmatched_flex_count: number;
};
export type Report = {
  currency: string;
  current: Totals;
  previous: Totals;
  categories: Group[];
  merchants: Group[];
  review_count: number;
  visible_buckets: number;
  history_status: string;
  combined: boolean;
  interpretation: string;
};
export type Group = {
  key: string;
  count: number;
  amount_minor: number;
  needs_review_count: number;
};
export type Preview = {
  change_count: number;
  protected_count: number;
  unchanged_count: number;
  card_check_count: number;
  declined_count: number;
  verified_replaced_count: number;
  preview_token: string;
  existing_rule: { category: string } | null;
};
export type Draft = {
  group_merchant?: boolean;
  category: string;
  scope: "this" | "unverified" | "all";
  future_rule: boolean;
  expected_version: number;
};

export type QueueHealth = {
  pending: number;
  running: number;
  failed: number;
  overdue: number;
  delayed: boolean;
  oldest_pending_at: string | null;
};
export type Quality = {
  total: number;
  verified: number;
  ai_suggested: number;
  needs_review: number;
  pending: number;
  missing_payment_name: number;
  reference_only_name: number;
  unnamed: number;
  unnamed_with_bank_or_rule_evidence: number;
  unnamed_manual_review: number;
  coverage: {
    account_id: string;
    kind: string;
    intervals: [string, string][];
    observed_at: string | null;
  }[];
};

export type BalanceData =
  | { amount_minor: number; currency: string; basis: "available" }
  | {
      pots: {
        id: string;
        name: string;
        amount_minor: number;
        currency: string;
        basis: "pot_balance";
      }[];
    };
export type BalanceReport = {
  observed_at: string;
  stale_after_minutes: number;
  snapshot_timezone: string;
  excluded: string[];
  items: {
    account_id: string;
    source: "current" | "pots";
    state: "fresh" | "stale" | "unavailable" | "unsupported";
    fetch_outcome: string | null;
    retrieved_at: string | null;
    provider_observed_at: string | null;
    last_attempt_at: string | null;
    data: BalanceData | null;
    history: { day: string; retrieved_at: string; data: BalanceData }[];
  }[];
};

export type BudgetItem = {
  category: string;
  target_minor: number | null;
  version: number;
  spent_minor: number;
  remaining_minor: number | null;
  pending_minor: number;
  credits_minor: number;
  history: {
    version: number;
    amount_minor: number | null;
    created_at: string;
  }[];
};
export type BudgetReport = {
  period: string;
  account_id: string;
  currency: "GBP";
  revision: number;
  observed_at: string;
  editable: boolean;
  coverage: {
    state: "complete" | "incomplete" | "not_started";
    required_until: string | null;
    accounts: {
      account_id: string;
      covered_until: string | null;
      complete: boolean;
    }[];
  };
  evidence_missing_count: number;
  unmatched_flex_count: number;
  items: BudgetItem[];
};
