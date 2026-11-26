import { infiniteQueryOptions, type QueryClient } from "@tanstack/react-query";
import type { Payments } from "./types.ts";

export function transactionFeedOptions(
  client: QueryClient,
  path: string,
  load: (path: string, signal: AbortSignal) => Promise<Payments>,
) {
  return infiniteQueryOptions({
    queryKey: ["transaction-feed", path],
    queryFn: ({ pageParam, signal }) =>
      load(
        path + (pageParam ? "&cursor=" + encodeURIComponent(pageParam) : ""),
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor || undefined,
    initialData: () => {
      const first = client.getQueryData<Payments>([path]);
      return first ? { pages: [first], pageParams: [null] } : undefined;
    },
    staleTime: Infinity,
    // Live updates invalidate inactive filters too. Refetch them on return.
    refetchOnMount: true,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}
