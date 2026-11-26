"use client";
import { useQuery } from "@tanstack/react-query";
import type { BalanceData, BalanceReport } from "@/lib/types";
import { date, money } from "./ui";

function Figures({ data }: { data: BalanceData }) {
  if ("amount_minor" in data)
    return (
      <p className="balance-amount">
        {money(data.amount_minor, data.currency)}
      </p>
    );
  return data.pots.length ? (
    <dl className="balance-pots">
      {data.pots.map((pot) => (
        <div key={pot.id}>
          <dt>{pot.name}</dt>
          <dd>{money(pot.amount_minor, pot.currency)}</dd>
        </div>
      ))}
    </dl>
  ) : (
    <p>No active pots.</p>
  );
}

export function BalanceDetails({ report }: { report: BalanceReport }) {
  return (
    <>
      <p className="m-help">
        Current account and pots. Flex balance is unavailable.
      </p>
      {!report.items.length && <p>No Current account is available.</p>}
      {report.items.map((item) => (
        <section
          className="m-card breakdown"
          key={item.account_id + item.source}
        >
          <h2>
            {item.source === "current"
              ? "Current available balance"
              : "Active pots"}
          </h2>
          {item.state === "unsupported" ? (
            <p>This balance source is not available from Monzo.</p>
          ) : item.data ? (
            <Figures data={item.data} />
          ) : (
            <p>Monzo hasn’t sent a balance yet.</p>
          )}
          {item.state === "stale" && (
            <p className="m-notice">
              Last known figures.{" "}
              {item.fetch_outcome === "report_unavailable"
                ? "We could not check when these were updated"
                : item.fetch_outcome === "error"
                  ? "The latest bank update failed"
                  : "The last update was over 45 minutes ago"}
              . Check service health in Settings.
            </p>
          )}
          {item.retrieved_at && (
            <p className="m-help">Updated {date(item.retrieved_at)}.</p>
          )}
          {item.source === "pots" && (
            <p className="m-help">
              Pots are updated separately from your Current balance.
            </p>
          )}
          <details className="balance-history">
            <summary>Balance history</summary>
            <p className="m-help">
              The last saved balance each day for the past 31 days, in UK time.
              Only days with a saved balance appear.
            </p>
            {item.history.length ? (
              <ol>
                {item.history.map((snapshot) => (
                  <li key={snapshot.day}>
                    <h3>{snapshot.day}</h3>
                    <Figures data={snapshot.data} />
                    <p className="m-help">
                      Updated {date(snapshot.retrieved_at)}
                    </p>
                  </li>
                ))}
              </ol>
            ) : (
              <p>No balance history yet.</p>
            )}
          </details>
        </section>
      ))}
    </>
  );
}

export function Balances() {
  const result = useQuery<BalanceReport>({
    queryKey: ["/v1/dashboard/balances"],
    refetchInterval: 30000,
  });
  // Do not keep showing a cached "fresh" label when the reporting API is unavailable.
  const report =
    result.data && result.error
      ? {
          ...result.data,
          items: result.data.items.map((item) => ({
            ...item,
            state: item.state === "fresh" ? ("stale" as const) : item.state,
            fetch_outcome: "report_unavailable",
          })),
        }
      : result.data;
  return (
    <>
      {result.error && (
        <p role="alert" className="m-error">
          Unable to refresh balances. Any figures below are from the last
          successful load.
        </p>
      )}
      {report ? (
        <BalanceDetails report={report} />
      ) : (
        !result.error && <p role="status">Loading balances…</p>
      )}
    </>
  );
}
