import type { BudgetItem, BudgetReport } from "@/lib/types";
import { date, label, money } from "./ui";

export function BudgetDetails({
  report,
  categories,
  edit,
  disabled = false,
}: {
  report: BudgetReport;
  categories?: string[];
  edit?: (item: BudgetItem) => void;
  disabled?: boolean;
}) {
  const active = (item: BudgetItem) =>
    !item.category ||
    item.target_minor !== null ||
    item.spent_minor > 0 ||
    item.pending_minor > 0 ||
    item.credits_minor > 0 ||
    item.history.length > 0;
  const card = (item: BudgetItem) => (
    <section className="m-card budget-card" key={item.category}>
      <h2>{item.category ? label(item.category) : "Overall"}</h2>
      {item.remaining_minor !== null && (
        <p className="budget-remaining">
          <strong>{money(item.remaining_minor, "GBP")}</strong> remaining
        </p>
      )}
      <dl className="balance-pots">
        <div>
          <dt>Target</dt>
          <dd>
            {item.target_minor === null
              ? "No budget set"
              : money(item.target_minor, "GBP")}
          </dd>
        </div>
        <div>
          <dt>Settled spending</dt>
          <dd>{money(item.spent_minor, "GBP")}</dd>
        </div>

        <div>
          <dt>Pending payments</dt>
          <dd>{money(item.pending_minor, "GBP")}</dd>
        </div>
        <div>
          <dt>Refunds and other credits</dt>
          <dd>{money(item.credits_minor, "GBP")}</dd>
        </div>
      </dl>
      {report.editable &&
        edit &&
        (!item.category.startsWith("custom:") ||
          categories?.includes(item.category)) && (
          <button
            className="m-button secondary"
            disabled={disabled}
            onClick={() => edit(item)}
          >
            {item.target_minor === null ? "Set" : "Edit"}{" "}
            {item.category ? label(item.category).toLowerCase() : "overall"}{" "}
            target
          </button>
        )}
      {item.history.length > 0 && (
        <details className="balance-history">
          <summary>Target history</summary>
          <ol>
            {item.history.map((version) => (
              <li key={version.version}>
                Version {version.version}:{" "}
                {version.amount_minor === null
                  ? "Target removed"
                  : money(version.amount_minor, "GBP")}{" "}
                · {date(version.created_at)}
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
  return (
    <>
      {report.account_id !== "combined" && (
        <p className="m-notice">
          Single-account view: transfers remain included, as in Spending. Choose
          Combined Current + Flex to exclude evidenced own-pot transfers and
          matched Flex repayments.
        </p>
      )}
      {report.coverage.state !== "complete" && (
        <p className="m-notice">
          {report.coverage.state === "not_started"
            ? "This month has not started. Spending will appear as payments arrive."
            : "Payment history is incomplete. Your remaining budget may be lower than shown."}
        </p>
      )}
      {report.evidence_missing_count + report.unmatched_flex_count > 0 && (
        <details className="spending-history-note">
          <summary>
            Provisional spending.{" "}
            <span className="when-closed">View details</span>
            <span className="when-open">Hide details</span>
          </summary>
          <p>
            Totals include{" "}
            {[
              report.evidence_missing_count > 0 &&
                `${report.evidence_missing_count} payment${report.evidence_missing_count === 1 ? "" : "s"} without bank confirmation`,
              report.unmatched_flex_count > 0 &&
                `${report.unmatched_flex_count} unmatched Flex movement${report.unmatched_flex_count === 1 ? "" : "s"}`,
            ]
              .filter(Boolean)
              .join(" and ")}
            .
          </p>
        </details>
      )}
      {!report.editable && (
        <p className="m-notice">Past targets are read-only.</p>
      )}
      <div className="budget-grid">{report.items.filter(active).map(card)}</div>
      {report.items.some((item) => !active(item)) && (
        <details className="balance-history">
          <summary>Other category targets</summary>
          <div className="budget-grid">
            {report.items.filter((item) => !active(item)).map(card)}
          </div>
        </details>
      )}
      <details className="balance-history">
        <summary>How budgets work</summary>
        <p className="m-help">
          GBP · London calendar months · No rollover. Overall and category
          limits overlap; they are not added together. Targets apply only to the
          selected month and account scope.
        </p>
        <p className="m-help">
          Pending payments are separate. Refunds and other credits do not
          increase the allowance. Uncategorised spending counts toward the
          overall target. Settlement, bank corrections and category edits can
          change actual spending, including past months.
        </p>
      </details>
      <details className="balance-history">
        <summary>Data coverage</summary>
        <p className="m-help">
          Loaded {date(report.observed_at)} · revision {report.revision}.
          Coverage requires completed bank reads from the start of the month
          through{" "}
          {report.coverage.required_until
            ? date(report.coverage.required_until)
            : "the elapsed part of the month"}{" "}
          for every selected account.
        </p>
        <ul>
          {report.coverage.accounts.map((account) => (
            <li key={account.account_id}>
              {account.account_id}:{" "}
              {account.covered_until
                ? `continuous coverage through ${date(account.covered_until)}`
                : "no evidenced coverage from the month start"}
            </li>
          ))}
        </ul>
      </details>
    </>
  );
}
