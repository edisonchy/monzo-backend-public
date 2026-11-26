import type { Bootstrap, Scope, View } from "./types.ts";
export function parseScope(
  params: Record<string, string | string[] | undefined>,
  bootstrap: Bootstrap,
  view?: View,
): Scope {
  const value = (key: string) =>
    typeof params[key] === "string" ? (params[key] as string) : "";
  const account = value("account_id");
  const many = (key: string) => [
    ...new Set(
      (Array.isArray(params[key])
        ? params[key]
        : value(key)
          ? [value(key)]
          : []) as string[],
    ),
  ];
  return {
    review_sort:
      value("review_sort") === "frequency"
        ? "frequency"
        : value("review_sort") === "value"
          ? "value"
          : "name",
    review_tab: value("review_tab") === "payments" ? "payments" : value("review_tab") === "needs_review" ? "needs_review" : "ai",
    categories: many("categories")
      .filter((c) => [...bootstrap.categories, "uncategorised"].includes(c))
      .slice(0, 50),
    merchants: many("merchants")
      .map((m) => m.slice(0, 500))
      .slice(0, 100),
    accounts: many("accounts").filter((id) =>
      bootstrap.accounts.some((a) => a.id === id),
    ),
    min_amount: /^\d{1,12}$/.test(value("min_amount"))
      ? value("min_amount")
      : undefined,
    max_amount: /^\d{1,12}$/.test(value("max_amount"))
      ? value("max_amount")
      : undefined,
    account_id:
      account === "combined" || bootstrap.accounts.some((a) => a.id === account)
        ? account
        : bootstrap.default_account_id || "combined",
    period:
      /^(20\d{2}|2100)-(0[1-9]|1[0-2])$/.test(value("period")) &&
      (view !== "spending" || value("period") <= bootstrap.today.slice(0, 7))
        ? value("period")
        : bootstrap.today.slice(0, 7),
    ...(value("date_from") && value("date_to")
      ? { date_from: value("date_from"), date_to: value("date_to") }
      : {}),
    mode: value("mode") === "year" ? "year" : "month",
    currency: /^[A-Z]{3}$/.test(value("currency")) ? value("currency") : "GBP",
    category: [...bootstrap.categories, "uncategorised"].includes(
      value("category"),
    )
      ? value("category")
      : "",
    ...(typeof params.merchant === "string"
      ? { merchant: params.merchant.slice(0, 500) || "Unnamed payment" }
      : {}),
    q: value("q").slice(0, 200),
    status: ["assigned", "pending", "needs_review", "excluded"].includes(
      value("status"),
    )
      ? value("status")
      : "all",
    source: ["ai", "manual", "rule", "monzo", "review"].includes(
      value("source"),
    )
      ? value("source")
      : "all",
    settlement: ["pending", "settled", "declined"].includes(value("settlement"))
      ? value("settlement")
      : "all",
    offset: /^\d{1,6}$/.test(value("offset"))
      ? Math.min(100000, Number(value("offset")))
      : 0,
  };
}
export function scopeParams(
  scope: Partial<Scope>,
  extras: Record<string, string> = {},
): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...scope, ...extras }))
    if (Array.isArray(value)) value.forEach((v) => params.append(key, v));
    else if (value !== undefined && value !== "")
      params.set(key, String(value));
  return params.toString();
}
function spendingScope(scope: Scope): Partial<Scope> {
  return {
    account_id: scope.account_id,
    period: scope.period,
    mode: scope.mode,
    currency: scope.currency,
    category: scope.category,
    merchant: scope.merchant,
  };
}
export function spendingReportPath(scope: Scope): string {
  return "/v1/dashboard/report?" + scopeParams(spendingScope(scope));
}
export function spendingPaymentsPath(scope: Scope): string {
  return (
    "/v1/dashboard/transactions?" +
    scopeParams(spendingScope(scope), {
      settlement: "settled",
      outgoing_only: "true",
      spending_only: "true",
      cursor_mode: "true",
      offset: "0",
    })
  );
}
export function transactionsPath(scope: Scope): string {
  return (
    "/v1/dashboard/transactions?" +
    scopeParams(
      {
        account_id: scope.account_id,
        period: scope.period,
        mode: scope.mode,
        currency: scope.currency,
        category: scope.category,
        merchant: scope.merchant,
        q: scope.q,
        status: scope.status,
        source: scope.source,
        settlement: scope.settlement,
        categories: scope.categories,
        merchants: scope.merchants,
        accounts: scope.accounts,
        min_amount: scope.min_amount,
        max_amount: scope.max_amount,
        date_from: scope.date_from,
        date_to: scope.date_to,
        offset: scope.offset,
      },
      { all_time: "true", cursor_mode: "true" },
    )
  );
}
export function reviewMerchantsPath(scope: Scope): string {
  return (
    "/v1/dashboard/review-merchants?" +
    new URLSearchParams({
      tab: scope.review_tab === "needs_review" ? "needs_review" : "ai",
      q: scope.q,
      ...(scope.review_sort && scope.review_sort !== "name"
        ? { sort: scope.review_sort, currency: scope.currency }
        : {}),
    })
  );
}
export function budgetsPath(scope: Scope): string {
  return (
    "/v1/dashboard/budgets?" +
    new URLSearchParams({
      period: scope.period,
      account_id: scope.account_id,
      currency: "GBP",
    })
  );
}
export function viewPrefetchPaths(view: View, scope: Scope): string[] {
  switch (view) {
    case "spending":
      return [
        spendingReportPath(scope),
        ...(scope.category || scope.merchant !== undefined
          ? [spendingPaymentsPath(scope)]
          : []),
      ];
    case "chat":
      return [];
    case "transactions":
      return [transactionsPath(scope)];
    case "review":
      return [scope.review_tab === "payments" ? transactionsPath(scope) : reviewMerchantsPath(scope)];
    case "budgets":
      return [budgetsPath(scope)];
    case "balances":
      return ["/v1/dashboard/balances"];
    case "settings":
      return ["/v1/status", "/v1/dashboard/rules"];
  }
}
