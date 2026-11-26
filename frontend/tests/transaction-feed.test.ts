import { strict as assert } from "node:assert";
import { test } from "node:test";
import { InfiniteQueryObserver, QueryClient } from "@tanstack/react-query";
import { transactionFeedOptions } from "../src/lib/transaction-feed.ts";
import type { Payments } from "../src/lib/types.ts";

const path = "/v1/dashboard/transactions?categories=groceries";
const page = (total: number, next_cursor: string | null = null): Payments => ({
  items: [],
  total,
  next_cursor,
  offset: 0,
  limit: 25,
});

test("returning to an invalidated filter refreshes its cached feed", async () => {
  const client = new QueryClient();
  const reads: string[] = [];
  client.setQueryData([path], page(1));
  const options = transactionFeedOptions(client, path, async (url) => {
    reads.push(url);
    return page(2);
  });
  const first = new InfiniteQueryObserver(client, options);
  const leave = first.subscribe(() => {});
  assert.equal(first.getCurrentResult().data?.pages[0].total, 1);
  leave();
  await client.invalidateQueries({ refetchType: "active" });
  assert.equal(reads.length, 0);
  // Navigation hydrates a fresh plain query, but the feed has its own cache key.
  client.setQueryData([path], page(2));
  const returned = new InfiniteQueryObserver(client, options);
  const unsubscribe = returned.subscribe(() => {});
  try {
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.deepEqual(reads, [path]);
    assert.equal(returned.getCurrentResult().data?.pages[0].total, 2);
    assert.equal(client.getQueryState(options.queryKey)?.isInvalidated, false);
  } finally {
    unsubscribe();
    client.clear();
  }
});

test("returning to a valid filter preserves loaded pages without refetching", async () => {
  const client = new QueryClient();
  const reads: string[] = [];
  client.setQueryData([path], page(40, "next&page"));
  const options = transactionFeedOptions(client, path, async (url) => {
    reads.push(url);
    return page(40);
  });
  const first = new InfiniteQueryObserver(client, options);
  const leave = first.subscribe(() => {});
  await first.fetchNextPage();
  assert.deepEqual(reads, [path + "&cursor=next%26page"]);
  leave();
  const returned = new InfiniteQueryObserver(client, options);
  const unsubscribe = returned.subscribe(() => {});
  try {
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(reads.length, 1);
    assert.equal(returned.getCurrentResult().data?.pages.length, 2);
  } finally {
    unsubscribe();
    client.clear();
  }
});
