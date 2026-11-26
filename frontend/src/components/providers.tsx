"use client";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  DehydratedState,
  HydrationBoundary,
  QueryClient,
  QueryClientProvider,
  useQueryClient,
} from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Login } from "./login";
const LiveContext = createContext({
  connected: false,
  changedAt: null as string | null,
  updates: false,
  freeze: (value: boolean) => {
    void value;
  },
  refresh: () => {},
});
export const useLive = () => useContext(LiveContext);
function Live({ children }: { children: React.ReactNode }) {
  const client = useQueryClient();
  const [connected, setConnected] = useState(false),
    [updates, setUpdates] = useState(false),
    [changedAt, setChangedAt] = useState<string | null>(null);
  const lastSeen = useRef(
    client.getQueryData<{ revision: number }>(["/v1/dashboard/revision"])
      ?.revision ?? null,
  );
  const frozen = useRef(false),
    queued = useRef(false),
    timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refresh = useCallback(() => {
    if (document.hidden) return;
    if (frozen.current) {
      queued.current = true;
      setUpdates(true);
      return;
    }
    if (timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      if (frozen.current) {
        queued.current = true;
        setUpdates(true);
        return;
      }
      queued.current = false;
      setUpdates(false);
      void client.invalidateQueries({
        refetchType: "active",
      });
    }, 350);
  }, [client]);
  const freeze = useCallback(
    (value: boolean) => {
      frozen.current = value;
      if (!value) {
        setUpdates(false);
        if (queued.current) refresh();
      }
    },
    [refresh],
  );
  useEffect(() => {
    let source: EventSource | null = null,
      online = false,
      stopped = false,
      previouslyConnected = false,
      backoff = 1000;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    const connect = () => {
      if (reconnect) clearTimeout(reconnect);
      source?.close();
      source = null;
      online = false;
      setConnected(false);
      if (document.hidden || stopped) return;
      source = new EventSource("/v1/dashboard/events");
      source.onopen = () => {
        online = true;
        setConnected(true);
        backoff = 1000;
        if (previouslyConnected) refresh();
        previouslyConnected = true;
      };
      source.onerror = () => {
        online = false;
        setConnected(false);
        source?.close();
        if (!stopped && !document.hidden) {
          reconnect = setTimeout(connect, backoff);
          backoff = Math.min(30000, backoff * 2);
        }
      };
      source.addEventListener("revision", (event) => {
        const data = JSON.parse(event.data);
        setChangedAt(data.changed_at);
        if (data.revision !== lastSeen.current) refresh();
        lastSeen.current = data.revision;
      });
      source.addEventListener("expired", () => {
        source?.close();
        window.dispatchEvent(new Event("session-expired"));
      });
    };
    // Explicit backoff also recovers from HTTP errors that close EventSource.
    // Fallback only reads our API, never the bank.
    const fallback = setInterval(() => {
      if (!document.hidden && !online) {
        refresh();
        void api<{ changed_at: string | null }>("/v1/dashboard/revision")
          .then((data) => {
            if (!stopped) setChangedAt(data.changed_at);
          })
          .catch(() => {
            /* Keep the last observed revision during an outage. */
          });
      }
    }, 60000);
    document.addEventListener("visibilitychange", connect);
    connect();
    return () => {
      stopped = true;
      source?.close();
      clearInterval(fallback);
      if (reconnect) clearTimeout(reconnect);
      if (timer.current) clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", connect);
    };
  }, [refresh]);
  return (
    <LiveContext.Provider
      value={{ connected, changedAt, updates, freeze, refresh }}
    >
      {children}
    </LiveContext.Provider>
  );
}
export function Providers({
  state,
  children,
}: {
  state: DehydratedState;
  children: React.ReactNode;
}) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30000,
            refetchOnWindowFocus: false,
            retry: (count, error) =>
              !(error instanceof ApiError && error.status < 500) && count < 1,
            queryFn: ({ queryKey, signal }) =>
              api(String(queryKey[0]), { signal }),
          },
          mutations: { retry: false },
        },
      }),
  );
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    const clear = () => {
      void client.cancelQueries();
      client.clear();
      setExpired(true);
    };
    const pageHide = () => {
      client.clear();
    };
    const pageShow = (event: PageTransitionEvent) => {
      if (event.persisted) window.location.reload();
    };
    window.addEventListener("session-expired", clear);
    window.addEventListener("pagehide", pageHide);
    window.addEventListener("pageshow", pageShow);
    return () => {
      window.removeEventListener("session-expired", clear);
      window.removeEventListener("pagehide", pageHide);
      window.removeEventListener("pageshow", pageShow);
    };
  }, [client]);
  if (expired) return <Login />;
  return (
    <QueryClientProvider client={client}>
      <HydrationBoundary state={state}>
        <Live>{children}</Live>
      </HydrationBoundary>
    </QueryClientProvider>
  );
}
