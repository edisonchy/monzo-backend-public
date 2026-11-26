import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  parseScope,
  viewPrefetchPaths,
  spendingReportPath,
  spendingPaymentsPath,
  transactionsPath,
  reviewMerchantsPath,
  scopeParams,
} from "../src/lib/scope.ts";
const bootstrap = {
  today: "2026-10-16",
  categories: ["groceries"],
  default_account_id: "combined",
  accounts: [
    {
      id: "acc_current",
      kind: "uk_retail",
      history_status: "imported",
      currencies: ["GBP"],
    },
  ],
};
test("invalid URL scopes cannot select hidden accounts or malformed report periods", () => {
  const scope = parseScope(
    {
      account_id: "acc_rewards",
      period: "2026-13",
      mode: "bad",
      offset: "-25",
      currency: "GBP&owner=true",
    },
    bootstrap,
  );
  assert.equal(scope.account_id, "combined");
  assert.equal(scope.period, "2026-10");
  assert.equal(scope.offset, 0);
  assert.equal(scope.currency, "GBP");
});
test("view query paths include full scope and preserve merchant text safely", () => {
  const scope = parseScope(
    { merchant: "A&B ? /", offset: "25", category: "groceries" },
    bootstrap,
  );
  const url = new URL(spendingPaymentsPath(scope), "https://example.test");
  assert.equal(url.searchParams.get("merchant"), "A&B ? /");
  assert.equal(url.searchParams.get("offset"), "0");
  assert.equal(url.searchParams.get("cursor_mode"), "true");
  assert.equal(url.searchParams.has("all_time"), false);
  assert.equal(url.searchParams.get("period"), "2026-10");
  assert.equal(url.searchParams.get("category"), "groceries");
  assert.equal(url.searchParams.get("spending_only"), "true");
  assert.equal(url.searchParams.get("settlement"), "settled");
  assert.equal(
    new URLSearchParams(scopeParams(scope)).get("account_id"),
    "combined",
  );
  assert.equal(
    new URL(
      reviewMerchantsPath(scope),
      "https://example.test",
    ).searchParams.get("tab"),
    "ai",
  );
});

test("fresh transactions default to this month and custom dates only scope transactions", () => {
  const fresh = parseScope({}, bootstrap);
  assert.equal(fresh.period, "2026-10");
  assert.equal(fresh.mode, "month");
  assert.equal(fresh.account_id, "combined");
  const ranged = parseScope(
    { date_from: "2026-09-30", date_to: "2026-10-02" },
    bootstrap,
  );
  assert.equal(
    new URL(transactionsPath(ranged), "https://example.test").searchParams.get(
      "date_from",
    ),
    "2026-09-30",
  );
  assert.equal(
    new URL(
      spendingReportPath(ranged),
      "https://example.test",
    ).searchParams.has("date_from"),
    false,
  );
});

test("transactions use all-time cursors and preserve multi-select filters", () => {
  const scope = parseScope(
    {
      categories: ["groceries", "bad"],
      merchants: ["A&B", "Shop"],
      accounts: ["acc_current", "hidden"],
      min_amount: "499",
    },
    bootstrap,
  );
  const url = new URL(transactionsPath(scope), "https://example.test");
  assert.equal(url.searchParams.get("all_time"), "true");
  assert.equal(url.searchParams.get("cursor_mode"), "true");
  assert.deepEqual(url.searchParams.getAll("categories"), ["groceries"]);
  assert.deepEqual(url.searchParams.getAll("merchants"), ["A&B", "Shop"]);
  assert.deepEqual(url.searchParams.getAll("accounts"), ["acc_current"]);
  assert.equal(
    new URL(spendingReportPath(scope), "https://example.test").searchParams.has(
      "categories",
    ),
    false,
  );
});

test("Spending clamps future periods using the London bootstrap date", () => {
  for (const mode of ["month", "year"]) {
    assert.equal(
      parseScope({ period: "2027-01", mode }, bootstrap, "spending").period,
      "2026-10",
    );
    assert.equal(
      parseScope({ period: "2026-11", mode }, bootstrap, "spending").period,
      "2026-10",
    );
    assert.equal(
      parseScope({ period: "2026-10", mode }, bootstrap, "spending").period,
      "2026-10",
    );
    assert.equal(
      parseScope({ period: "2025-12", mode }, bootstrap, "spending").period,
      "2025-12",
    );
  }
  assert.equal(
    parseScope({ period: "2027-01" }, bootstrap, "review").period,
    "2027-01",
  );
});

test("review uses only its tab and search across all history", () => {
  const scope = parseScope(
    {
      review_tab: "needs_review",
      q: "Shop",
      account_id: "acc_current",
      period: "2025-01",
      category: "groceries",
    },
    bootstrap,
  );
  const url = new URL(reviewMerchantsPath(scope), "https://example.test");
  assert.equal(url.pathname, "/v1/dashboard/review-merchants");
  assert.equal(url.searchParams.get("tab"), "needs_review");
  assert.equal(url.searchParams.get("q"), "Shop");
  assert.equal(url.searchParams.has("period"), false);
  assert.equal(url.searchParams.has("account_id"), false);
  assert.equal(url.searchParams.has("category"), false);
});

test("review priorities preserve currency and do not inherit spending filters", () => {
  const scope = parseScope(
    {
      review_sort: "value",
      currency: "USD",
      categories: "groceries",
      date_from: "2020-01-01",
      date_to: "2020-02-01",
    },
    bootstrap,
    "review",
  );
  const url = new URL(reviewMerchantsPath(scope), "https://example.test");
  assert.equal(url.searchParams.get("sort"), "value");
  assert.equal(url.searchParams.get("currency"), "USD");
  assert.equal(url.searchParams.has("date_from"), false);
  assert.equal(url.searchParams.has("categories"), false);
  assert.equal(
    parseScope({ review_sort: "untrusted" }, bootstrap).review_sort,
    "name",
  );
});

test("Spending drilldowns discard hidden transaction filters without changing Transactions", () => {
  const scope = parseScope(
    {
      q: "Tesco",
      source: "ai",
      status: "needs_review",
      settlement: "pending",
      categories: ["groceries"],
      merchants: ["Tesco"],
      accounts: ["acc_current"],
      min_amount: "100",
      max_amount: "500",
      date_from: "2026-10-01",
      date_to: "2026-10-02",
      category: "groceries",
    },
    bootstrap,
    "spending",
  );
  for (const path of viewPrefetchPaths("spending", scope)) {
    const params = new URL(path, "https://example.test").searchParams;
    for (const key of [
      "q",
      "categories",
      "merchants",
      "accounts",
      "min_amount",
      "max_amount",
      "date_from",
      "date_to",
    ])
      assert.equal(params.has(key), false, key);
    assert.equal(params.has("source"), false);
    assert.equal(params.has("status"), false);
    assert.equal(params.get("category"), "groceries");
    assert.equal(params.get("period"), "2026-10");
  }
  const transactions = new URL(transactionsPath(scope), "https://example.test")
    .searchParams;
  assert.equal(transactions.get("q"), "Tesco");
  assert.equal(transactions.get("source"), "ai");
  assert.equal(transactions.get("status"), "needs_review");
  assert.equal(transactions.get("settlement"), "pending");
});

test("Spending prefetch includes unnamed merchant drilldowns and omits overview payments", () => {
  assert.deepEqual(viewPrefetchPaths("spending", parseScope({}, bootstrap)), [
    "/v1/dashboard/report?account_id=combined&period=2026-10&mode=month&currency=GBP",
  ]);
  const unnamed = parseScope({ merchant: "" }, bootstrap);
  for (const path of viewPrefetchPaths("spending", unnamed)) {
    assert.equal(
      new URL(path, "https://example.test").searchParams.get("merchant"),
      "Unnamed payment",
    );
  }
  assert.equal(
    new URLSearchParams(scopeParams(unnamed)).get("merchant"),
    "Unnamed payment",
  );
  assert.deepEqual(viewPrefetchPaths("spending", unnamed), [
    spendingReportPath(unnamed),
    spendingPaymentsPath(unnamed),
  ]);
});

test("transaction requests allow supported filters without review state", () => {
  const scoped = parseScope(
    {
      review_tab: "needs_review",
      review_sort: "value",
      q: "A&B ? /",
      category: "groceries",
      merchant: "Shop",
      categories: ["groceries"],
      merchants: ["One", "Two"],
      accounts: ["acc_current"],
      date_from: "2026-01-01",
      date_to: "2026-10-31",
      min_amount: "100",
      max_amount: "500",
      status: "assigned",
      source: "manual",
      settlement: "settled",
      offset: "25",
    },
    bootstrap,
  );
  assert.deepEqual(
    [...new URL(transactionsPath(scoped), "https://example.test").searchParams],
    [
      ["account_id", "combined"],
      ["period", "2026-10"],
      ["mode", "month"],
      ["currency", "GBP"],
      ["category", "groceries"],
      ["merchant", "Shop"],
      ["q", "A&B ? /"],
      ["status", "assigned"],
      ["source", "manual"],
      ["settlement", "settled"],
      ["categories", "groceries"],
      ["merchants", "One"],
      ["merchants", "Two"],
      ["accounts", "acc_current"],
      ["min_amount", "100"],
      ["max_amount", "500"],
      ["date_from", "2026-01-01"],
      ["date_to", "2026-10-31"],
      ["offset", "25"],
      ["all_time", "true"],
      ["cursor_mode", "true"],
    ],
  );
});

test("bookmarked filters survive URL serialization and browser-history parsing", () => {
  const original = parseScope(
    {
      merchant: "A&B ? /",
      categories: ["groceries"],
      merchants: ["One", "Two"],
      accounts: ["acc_current"],
      min_amount: "100",
      max_amount: "500",
      date_from: "2026-01-01",
      date_to: "2026-10-31",
      q: "Shop",
      source: "manual",
      review_tab: "needs_review",
      review_sort: "value",
      offset: "25",
    },
    bootstrap,
  );
  const query = new URLSearchParams(scopeParams(original));
  const params = Object.fromEntries(
    [...new Set(query.keys())].map((key) => [
      key,
      query.getAll(key).length > 1 ? query.getAll(key) : query.get(key)!,
    ]),
  );
  assert.deepEqual(parseScope(params, bootstrap), original);
});
