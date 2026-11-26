import type { Payments } from "@/lib/types";
import { categoryClass, Badge, label, day, Empty, Icon, money } from "./ui";
export function PaymentList({
  data,
  open,
}: {
  data: Payments;
  open: (id: string) => void;
}) {
  if (!data.items.length)
    return (
      <Empty title="No matching payments">
        Try another period or clear your filters.
      </Empty>
    );
  const days: Record<string, Payments["items"]> = {};
  for (const tx of data.items) (days[day(tx.created_at)] ??= []).push(tx);
  return (
    <>
      {Object.entries(days).map(([date, items]) => (
        <section key={date}>
          <h3 className="date-heading">{date}</h3>
          <div className="payments-card breakdown-list">
            {items.map((tx) => (
              <button
                key={tx.id}
                className="data-row"
                onClick={() => open(tx.id)}
              >
                <span
                  role="img"
                  aria-label={label(
                    ["assigned", "needs_review"].includes(tx.category_status)
                      ? tx.category
                      : "uncategorised",
                  )}
                  className={
                    "spending-swatch " +
                    categoryClass(
                      ["assigned", "needs_review"].includes(tx.category_status)
                        ? tx.category
                        : "uncategorised",
                    )
                  }
                />
                <span className="row-text">
                  <strong>{tx.merchant}</strong>
                  <span className="payment-meta">
                    {tx.declined && <span>Declined</span>}
                    {!tx.declined && (
                      <>
                        {!tx.settled_at && (
                          <span
                            className="m-badge pending"
                            title="This payment has not settled yet"
                          >
                            Pending
                          </span>
                        )}
                        <Badge tx={tx} />
                      </>
                    )}
                  </span>
                </span>
                <span
                  className={
                    "row-amount" + (tx.amount_minor > 0 ? " positive" : "")
                  }
                >
                  {tx.amount_minor > 0 ? "+" : ""}
                  {money(tx.amount_minor, tx.currency)}
                </span>
                <Icon name="chevron_right" className="row-chevron" />
              </button>
            ))}
          </div>
        </section>
      ))}
    </>
  );
}
