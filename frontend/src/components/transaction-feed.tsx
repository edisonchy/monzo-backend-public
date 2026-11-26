"use client";
import { useEffect, useRef } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import type { Payments } from "@/lib/types";
import { api } from "@/lib/api";
import { transactionFeedOptions } from "@/lib/transaction-feed";
import { PaymentList } from "./payments";
export function TransactionFeed({
  path,
  open,
  showEnd = true,
}: {
  path: string;
  open: (id: string) => void;
  showEnd?: boolean;
}) {
  const client = useQueryClient();
  const sentinel = useRef<HTMLDivElement>(null);
  const query = useInfiniteQuery(
    transactionFeedOptions(client, path, (url, signal) =>
      api<Payments>(url, { signal }),
    ),
  );
  const { fetchNextPage, hasNextPage, isFetching, isError } = query;
  useEffect(() => {
    const node = sentinel.current;
    if (!node || !hasNextPage || isFetching || isError) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) void fetchNextPage();
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetching, isError]);
  const items = [
    ...new Map(
      query.data?.pages.flatMap((p) => p.items).map((tx) => [tx.id, tx]) || [],
    ).values(),
  ];
  return (
    <>
      {query.data && (
        <PaymentList
          data={{
            items,
            total: query.data.pages[0].total,
            offset: 0,
            limit: 25,
          }}
          open={open}
        />
      )}
      <div className="feed-loader" ref={sentinel}>
        {query.isLoading || query.isFetchingNextPage ? (
          <span
            role="status"
            aria-label="Loading transactions"
            className="loading-dots"
          >
            <i />
            <i />
            <i />
          </span>
        ) : query.isError ? (
          <>
            <p role="alert">Couldn’t load transactions.</p>
            <button
              className="m-button secondary"
              onClick={() =>
                query.isFetchNextPageError
                  ? void query.fetchNextPage()
                  : void query.refetch()
              }
            >
              Retry
            </button>
          </>
        ) : query.hasNextPage ? (
          <button
            className="text-button"
            onClick={() => void query.fetchNextPage()}
          >
            Load more
          </button>
        ) : showEnd && items.length > 0 ? (
          <p className="muted">You’re all caught up</p>
        ) : null}
      </div>
    </>
  );
}
