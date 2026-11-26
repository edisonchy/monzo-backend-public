"use client";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Report, Scope } from "@/lib/types";
import { categoryClass, Icon, label, money } from "./ui";
import { movePlot, plotGeometry, rollAmount } from "./spending-motion";
export function Chart({
  report,
  scope,
  accountLabel,
  change,
}: {
  report: Report;
  scope: Scope;
  accountLabel: string;
  change: object | null;
}) {
  const [mode, setMode] = useState("line");
  const plot = useRef<SVGRectElement>(null);
  const [selection, setSelection] = useState<{
    key: string;
    index: number;
  } | null>(null);
  const partialHistory = report.history_status !== "imported";
  const scopeKey = JSON.stringify(scope);
  const svgRef = useRef<SVGSVGElement>(null);
  const reels = useRef<HTMLSpanElement>(null);
  const consumed = useRef<object | null>(null);
  const last = useRef<{ key: string; total: number; geometry: ReturnType<typeof plotGeometry> } | null>(null);
  useLayoutEffect(() => {
    const svg = svgRef.current!;
    const host = reels.current!;
    const key = scopeKey + mode;
    const total = report.current.debits_minor;
    const old = last.current;
    last.current = { key, total, geometry: plotGeometry(svg) };
    if (!change || consumed.current === change) return;
    consumed.current = change;
    if (old?.key !== key || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const stopPlot = movePlot(svg, old.geometry);
    const animations = old.total === total ? [] : rollAmount(host, old.total, total, scope.currency);
    let active = true;
    const reset = () => {
      host.replaceChildren();
      animations.forEach(animation => animation.cancel());
      stopPlot();
    };
    if (animations.length) Promise.all(animations.map(animation => animation.finished)).then(() => {
      if (active) reset();
    }).catch(() => {});
    return () => { active = false; reset(); };
  }, [report, change, scopeKey, scope.currency, mode]);
  const selected =
    selection?.key === scopeKey && selection.index < report.visible_buckets
      ? selection.index
      : null;
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (event.pointerType !== "mouse" && event.target !== plot.current)
        setSelection(null);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  const cumulative = (values: number[]) => {
    let n = 0;
    return values.map((value) => (n += value));
  };
  const running = cumulative(report.current.series);
  const current = mode === "line" ? running : report.current.series;
  const previous =
    mode === "line"
      ? cumulative(report.previous.series)
      : report.previous.series;
  const length = Math.max(current.length, previous.length),
    max = Math.max(1, ...current, ...previous);
  const currencyFormat = new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: scope.currency,
  });
  const scale =
    10 ** (currencyFormat.resolvedOptions().maximumFractionDigits ?? 2);
  const axisFormat = new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: scope.currency,
    notation: max / scale >= 1000 ? "compact" : "standard",
    minimumFractionDigits: 0,
    maximumFractionDigits:
      max / scale >= 1000
        ? 1
        : currencyFormat.resolvedOptions().maximumFractionDigits,
  });
  const x = (i: number) => 8 + (450 * i) / Math.max(1, length - 1),
    y = (n: number) => 170 - (145 * n) / max;
  const shown = current.slice(0, report.visible_buckets),
    points = shown.map((n, i) => `${x(i)},${y(n)}`).join(" "),
    step = 450 / length;
  const categories =
    selected === null
      ? []
      : (report.current.bucket_categories?.[selected] ?? []);
  const yearly = scope.mode === "year";
  const periodDate = new Date(scope.period + "-01T12:00:00Z");
  const periodFormat = new Intl.DateTimeFormat("en-GB", {
    month: yearly ? undefined : "long",
    year: "numeric",
    timeZone: "UTC",
  });
  const period = periodFormat.format(periodDate);
  const previousDate = new Date(periodDate);
  previousDate.setUTCMonth(previousDate.getUTCMonth() - (yearly ? 12 : 1));
  const bucketDate = (i: number) =>
    new Intl.DateTimeFormat("en-GB", {
      ...(scope.mode === "year"
        ? { month: "long" as const, year: "numeric" as const }
        : { day: "numeric" as const, month: "short" as const }),
      timeZone: "UTC",
    }).format(
      new Date(
        scope.mode === "year"
          ? Date.UTC(Number(scope.period.slice(0, 4)), i, 1)
          : Date.UTC(
              Number(scope.period.slice(0, 4)),
              Number(scope.period.slice(5)) - 1,
              i + 1,
            ),
      ),
    );
  const pick = (event: React.PointerEvent<SVGRectElement>) => {
    const svg = event.currentTarget.ownerSVGElement;
    const matrix = svg?.getScreenCTM();
    if (!svg || !matrix) return;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const local = point.matrixTransform(matrix.inverse());
    const outsideHeight = local.y < 25 || local.y > 170;
    if (local.x < 8 || local.x > 458 || (outsideHeight && event.pointerType !== "touch")) {
      setSelection(null);
      return;
    }
    const index =
      mode === "line"
        ? Math.round(((local.x - 8) / 450) * (length - 1))
        : Math.floor((local.x - 8) / step);
    setSelection(
      index >= 0 && index < shown.length ? { key: scopeKey, index } : null,
    );
  };
  return (
    <section
      className={
        scope.category
          ? "spending-report " + categoryClass(scope.category)
          : "spending-report"
      }
      aria-label={report.combined ? "Combined spending" : "Money out"}
    >
      <div
        className={"chart-heading" + (selected !== null ? " inspecting" : "")}
      >
        <div className="spending-summary">
          <div className="spending-summary-meta">
            <p>{period}</p>
            <p className="summary-account">{accountLabel}</p>
          </div>
          <strong className="m-amount">
            <span ref={reels} className="amount-reels" aria-hidden="true" /><span className="amount-final">{money(report.current.debits_minor, scope.currency)}</span>
          </strong>
        </div>
        {selected !== null && (
          <div className="chart-inspection" aria-live="off">
            <div>
              <p>{partialHistory ? "Recorded so far" : "Spent so far"}</p>
              <strong className="m-amount">
                {money(running[selected], scope.currency)}
              </strong>
            </div>
            <div className="chart-bucket">
              <p>{bucketDate(selected)}</p>
              <p>
                <strong>
                  {money(report.current.series[selected], scope.currency)}
                </strong>{" "}
                {partialHistory ? "recorded" : "spent"}
              </p>
              {categories.length > 0 && (
                <span
                  className="chart-category-dots"
                  role="img"
                  aria-label={categories.map(label).join(", ")}
                >
                  {categories.map((category) => (
                    <i key={category} className={categoryClass(category)} />
                  ))}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
      {partialHistory && (
        <details key={scopeKey} className="spending-history-note">
          <summary>
            Some payments may be missing.{" "}
            <span className="when-closed">View details</span>
            <span className="when-open">Hide details</span>
          </summary>
          <p>
            Bank history is incomplete. Totals include only recorded payments.
          </p>
        </details>
      )}
      <div className="spending-chart-tools">
        {mode === "bar" && (
          <h2>{yearly ? "Monthly spending" : "Daily spending"}</h2>
        )}
        <button
          type="button"
          className="text-button"
          onClick={() => setMode(mode === "line" ? "bar" : "line")}
          aria-label={
            mode === "line"
              ? "Show spending as bars"
              : "Show cumulative spending as a line"
          }
          title={mode === "line" ? "Show bars" : "Show cumulative line"}
        >
          <Icon name={mode === "line" ? "bar_chart" : "show_chart"} />
        </button>
      </div>
      <svg
        ref={svgRef}
        viewBox="0 0 560 210"
        className="chart"
        role="group"
        aria-label="Selected versus previous period. Use the graph to inspect spending."
      >
        {[0, 1, 2].map((i) => (
          <g key={i}>
            <line
              x1="8"
              x2="458"
              y1={25 + i * 72.5}
              y2={25 + i * 72.5}
              className="gridline"
            />
            <text
              x="554"
              y={29 + i * 72.5}
              textAnchor="end"
              className="chart-label"
            >
              {axisFormat.format((max * (1 - i / 2)) / scale)}
            </text>
          </g>
        ))}
        {mode === "line" ? (
          <>
            <polyline
              points={previous.map((n, i) => `${x(i)},${y(n)}`).join(" ")}
              className="chart-prev"
            />
            {shown.length > 0 && (
              <>
                <polyline points={points} className="chart-line" />
                {selected === null && (
                  <circle
                    cx={x(shown.length - 1)}
                    cy={y(shown[shown.length - 1])}
                    r="5"
                    className="chart-dot"
                  />
                )}
              </>
            )}
          </>
        ) : (
          <>
            {previous.map((n, i) => (
              <rect
                key={"p" + i}
                x={8 + i * step}
                y={y(n)}
                width={Math.max(1, step * 0.4)}
                height={170 - y(n)}
                rx="2"
                className="chart-bar-prev"
              />
            ))}
            {shown.map((n, i) => (
              <rect
                key={i}
                x={8 + i * step + step * 0.45}
                y={y(n)}
                width={Math.max(1, step * 0.4)}
                height={170 - y(n)}
                rx="2"
                className="chart-bar"
              />
            ))}
          </>
        )}
        {selected !== null && (
          <g className="chart-inspection-guide" pointerEvents="none">
            <line
              x1={mode === "line" ? x(selected) : 8 + (selected + 0.65) * step}
              x2={mode === "line" ? x(selected) : 8 + (selected + 0.65) * step}
              y1="25"
              y2="170"
            />
            <circle
              cx={mode === "line" ? x(selected) : 8 + (selected + 0.65) * step}
              cy={y(current[selected])}
              r="6"
              className="chart-dot"
            />
          </g>
        )}
        {shown.length > 0 && (
          <rect
            ref={plot}
            x="8"
            y="25"
            width="450"
            height="145"
            fill="transparent"
            className="chart-hit-area"
            tabIndex={0}
            role="slider"
            aria-label={
              scope.mode === "year"
                ? "Inspect spending by month"
                : "Inspect spending by day"
            }
            aria-valuemin={1}
            aria-valuemax={shown.length}
            aria-valuenow={(selected ?? 0) + 1}
            aria-valuetext={`${bucketDate(selected ?? 0)}: ${money(report.current.series[selected ?? 0], scope.currency)} ${partialHistory ? "recorded" : "spent"}; ${money(running[selected ?? 0], scope.currency)} so far`}
            onPointerMove={pick}
            onPointerDown={pick}
            onPointerLeave={(event) => {
              if (event.pointerType !== "touch") setSelection(null);
            }}
            onPointerCancel={() => setSelection(null)}
            onFocus={() =>
              setSelection((previous) =>
                previous?.key === scopeKey
                  ? previous
                  : { key: scopeKey, index: 0 },
              )
            }
            onBlur={() => setSelection(null)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setSelection(null);
                return;
              }
              if (
                !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              )
                return;
              event.preventDefault();
              const index =
                event.key === "Home"
                  ? 0
                  : event.key === "End"
                    ? shown.length - 1
                    : Math.max(
                        0,
                        Math.min(
                          shown.length - 1,
                          (selected ?? 0) +
                            (event.key === "ArrowLeft" ? -1 : 1),
                        ),
                      );
              setSelection({ key: scopeKey, index });
            }}
          />
        )}
        <text x="8" y="198" className="chart-label">
          {yearly ? "Jan" : bucketDate(0)}
        </text>
        <text x="458" y="198" textAnchor="end" className="chart-label">
          {yearly ? "Dec" : length}
        </text>
      </svg>
      <div className="legend">
        <span>{period}</span>
        <span>{periodFormat.format(previousDate)}</span>
      </div>
    </section>
  );
}
