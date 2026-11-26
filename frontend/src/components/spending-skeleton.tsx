export function SpendingSkeleton({
  group = "categories", detail = false,
}: { group?: "categories" | "merchants"; detail?: boolean }) {
  return <>
    <div className="spending-chart" aria-hidden="true">
      <section className="spending-report">
        <div className="chart-heading"><div className="spending-summary">
          <div className="spending-summary-meta"><p><i className="sk sk-date" /></p><p className="summary-account"><i className="sk sk-account" /></p></div>
          <strong className="m-amount"><i className="sk sk-amount" /></strong>
        </div></div>
        <div className="spending-chart-tools"><button disabled tabIndex={-1} className="text-button"><i className="sk sk-icon" /></button></div>
        <div className="chart sk sk-chart" />
        <div className="legend"><i className="sk sk-key" /><i className="sk sk-key" /></div>
      </section>
    </div>
    <section className={detail ? "detail-payments" : "spending-breakdown"} aria-hidden="true">
      {!detail && <>
        <div className="breakdown-switch" inert>
          <button aria-pressed={group === "categories"}>Categories</button>
          <button aria-pressed={group === "merchants"}>Merchants</button>
        </div>
        <div className="breakdown-bar-slot sk-share-slot">{group === "categories" && <i className="sk sk-share" />}</div>
      </>}
      {detail ? [2, 1].map((count, index) => (
        <section key={index}>
          <h3 className="date-heading"><i className="sk sk-date" /></h3>
          <SkeletonRows count={count} payments />
        </section>
      )) : <SkeletonRows count={4} group={group} />}
    </section>
  </>;
}

function SkeletonRows({ count, payments = false, group }: { count: number; payments?: boolean; group?: "categories" | "merchants" }) {
  return (
      <div className={payments ? "payments-card breakdown-list" : "breakdown-list"} data-group={group}>
        {Array.from({ length: count }, (_, row) => <div className="data-row" key={row}>
          {(payments || group === "categories") && <i className="spending-swatch sk" />}
          <span className="row-text"><strong><i className="sk sk-name" /></strong><small><i className="sk sk-count" /></small></span>
          <span className="row-amount"><i className="sk sk-value" /></span>
          <span className="row-chevron">›</span>
        </div>)}
      </div>
  );
}
