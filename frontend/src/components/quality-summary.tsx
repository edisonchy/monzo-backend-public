"use client";
import { useQuery } from "@tanstack/react-query";
import type { Quality } from "@/lib/types";
import { date } from "./ui";

function useQuality() {
  return useQuery<Quality>({ queryKey: ["/v1/dashboard/quality"] });
}
export function Coverage() {
  const query = useQuality();
  return (
    <details className="settings-disclosure" open={query.isError || undefined}>
      <summary>Data coverage</summary>
      <div className="disclosure-body">
        <p className="m-help">
          Dates with complete payment history. Gaps may contain missing
          payments.
        </p>
        {query.isError ? (
          <p role="alert">
            Coverage could not be refreshed.{" "}
            <button
              className="text-button"
              onClick={() => void query.refetch()}
            >
              Retry
            </button>
          </p>
        ) : (
          (query.data?.coverage.map((a) => (
            <div key={a.account_id}>
              <h3>{a.kind === "uk_retail" ? "Current account" : "Flex"}</h3>
              {a.intervals.length ? (
                <ul>
                  {a.intervals.map(([start, end]) => (
                    <li key={start}>
                      {date(start)} – {date(end)}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>No confirmed history yet.</p>
              )}
              <p className="m-help">Last checked {date(a.observed_at)}</p>
            </div>
          )) ?? <p>Loading coverage…</p>)
        )}
      </div>
    </details>
  );
}
