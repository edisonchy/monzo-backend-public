"use client";
import { useEffect, useRef } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import type { Scope } from "@/lib/types";
import { TransactionFeed } from "./transaction-feed";
import { transactionsPath, reviewMerchantsPath } from "@/lib/scope";
import { api } from "@/lib/api";
import { Empty, Icon, label, money } from "./ui";
const statuses = [
  ["all", "All statuses"],
  ["assigned", "Categorised"],
  ["needs_review", "Needs review"],
  ["payments", "Payments"],
  ["pending", "Awaiting categorisation"],
  ["excluded", "Excluded"],
];
const sources = [
  ["all", "All sources"],
  ["ai", "AI suggestion"],
  ["manual", "You"],
  ["rule", "Saved rule"],
  ["monzo", "Monzo"],
  ["review", "Sent for review"],
];
type Merchant = {
  key: string;
  name: string;
  payment_id: string;
  payment_count: number;
  category: string | null;
  debits_minor: number | null;
  currency: string | null;
};
type Page = { items: Merchant[]; next_cursor: string | null };
export function Review({
  scope,
  navigate,
  open,
}: {
  scope: Scope;
  navigate: (patch: Partial<Scope>) => void;
  open: (id: string) => void;
}) {
  const path = reviewMerchantsPath(scope);
  const client = useQueryClient();
  const sentinel = useRef<HTMLDivElement>(null);
  const query = useInfiniteQuery({
    enabled: scope.review_tab !== "payments",
    queryKey: ["review-feed", path],
    queryFn: ({ pageParam, signal }) =>
      api<Page>(
        path + (pageParam ? "&cursor=" + encodeURIComponent(pageParam) : ""),
        { signal },
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor || undefined,
    initialData: () => {
      const first = client.getQueryData<Page>([path]);
      return first ? { pages: [first], pageParams: [null] } : undefined;
    },
  });
  const { fetchNextPage, hasNextPage, isFetching, isError } = query;
  useEffect(() => {
    if (!sentinel.current || !hasNextPage || isFetching || isError) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) void fetchNextPage();
      },
      { rootMargin: "200px" },
    );
    observer.observe(sentinel.current);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetching, isError]);
  const items = [
    ...new Map(
      query.data?.pages.flatMap((p) => p.items).map((m) => [m.key, m]) || [],
    ).values(),
  ];
  const groups: Record<string, Merchant[]> = {};
  for (const item of items) {
    const first =
      scope.review_sort && scope.review_sort !== "name"
        ? "Priority"
        : item.name[0]?.toUpperCase() || "#";
    (groups[/[A-Z]/.test(first) ? first : "#"] ??= []).push(item);
  }
  return (
    <>
      <section className="review-tools" aria-label="Review filters">
        <div className="m-segment" aria-label="Review status">
          {(
            [
              ["ai", "AI suggestions"],
              ["needs_review", "Needs review"],
              ["payments", "Payments"],
            ] as const
          ).map(([value, title]) => (
            <button
              key={value}
              aria-pressed={(scope.review_tab || "ai") === value}
              onClick={() => navigate({ review_tab: value })}
            >
              {title}
            </button>
          ))}
        </div>
        {scope.review_tab === "payments" ? (
          <>
            {(
              [
                ["status", "Category status", statuses],
                ["source", "Categorised by", sources],
              ] as const
            ).map(([key, title, options]) => (
              <label key={key}>
                {title}
                <select
                  className="m-input"
                  value={scope[key]}
                  onChange={(e) =>
                    navigate({ [key]: e.target.value, offset: 0 })
                  }
                >
                  {options.map(([value, name]) => (
                    <option key={value} value={value}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </>
        ) : (
          <>
            <form
              className="transaction-search"
              key={scope.q}
              onSubmit={(e) => {
                e.preventDefault();
                navigate({
                  q: String(new FormData(e.currentTarget).get("q") || ""),
                });
              }}
            >
              <Icon name="search" />
              <input
                type="search"
                name="q"
                aria-label="Search merchants"
                placeholder="Search merchants…"
                maxLength={200}
                defaultValue={scope.q}
              />
              <button className="text-button">Search</button>
            </form>
            <label>
              Sort by
              <select
                className="m-input"
                aria-label="Review priority"
                value={scope.review_sort || "name"}
                onChange={(e) =>
                  navigate({
                    review_sort: e.target.value as Scope["review_sort"],
                  })
                }
              >
                <option value="name">Merchant name</option>
                <option value="frequency">Most payments</option>
                <option value="value">
                  Highest spending ({scope.currency})
                </option>
              </select>
            </label>
          </>
        )}
      </section>
      {scope.review_tab === "payments" ? (
        <TransactionFeed
          key={transactionsPath(scope)}
          path={transactionsPath(scope)}
          open={open}
        />
      ) : (
        <>
          {Object.entries(groups).map(([letter, merchants]) => (
            <section key={letter}>
              <h3 className="date-heading">{letter}</h3>
              <div
                className="payments-card breakdown-list"
                data-group="merchants"
              >
                {merchants.map((m) => (
                  <button
                    className="data-row review-merchant"
                    key={m.key}
                    onClick={() => open(m.payment_id)}
                  >
                    <span className="row-text">
                      <strong>{m.name}</strong>
                      <span className="payment-meta">
                        {m.payment_count} payment
                        {m.payment_count === 1 ? "" : "s"}
                        {m.category ? ` · ${label(m.category)}` : ""}
                        {m.debits_minor !== null && m.currency
                          ? ` · ${money(m.debits_minor, m.currency)} outgoing`
                          : ""}
                      </span>
                    </span>
                    <Icon name="chevron_right" />
                  </button>
                ))}
              </div>
            </section>
          ))}
          {!items.length && !isFetching && !isError && (
            <Empty title={scope.q ? "No matching merchants" : "All caught up"}>
              {null}
            </Empty>
          )}
          <div className="feed-loader" ref={sentinel}>
            {isFetching ? (
              <span
                role="status"
                aria-label="Loading merchants"
                className="loading-dots"
              >
                <i />
                <i />
                <i />
              </span>
            ) : isError ? (
              <>
                <p role="alert">Couldn’t load merchants.</p>
                <button
                  className="m-button secondary"
                  onClick={() =>
                    query.isFetchNextPageError
                      ? void fetchNextPage()
                      : void query.refetch()
                  }
                >
                  Retry
                </button>
              </>
            ) : hasNextPage ? (
              <button
                className="text-button"
                onClick={() => void fetchNextPage()}
              >
                Load more
              </button>
            ) : null}
          </div>
        </>
      )}
    </>
  );
}
