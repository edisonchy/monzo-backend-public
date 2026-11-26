"use client";
import { positionTooltip } from "./position-tooltip";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useLayoutEffect, useRef, useState, useTransition } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import {
  spendingReportPath,
  spendingPaymentsPath,
  transactionsPath,
  scopeParams,
} from "@/lib/scope";
import type { Bootstrap, Report, Scope, View } from "@/lib/types";
import { categoryClass, accountName, Empty, Icon, label, money, spendingDetailTitle } from "./ui";
import { TransactionFeed } from "./transaction-feed";
import { TransactionFilters } from "./transaction-filters";
import { ScopeControls } from "./scope-controls";
import { SpendingSkeleton } from "./spending-skeleton";
import { Chart } from "./chart";
import { SpendingChat } from "./spending-chat";
import { Review } from "./review";
import { PageNotice } from "./page-notice";
import { PaymentDialog } from "./payment-dialog";
import { Settings } from "./settings";
import { Budgets } from "./budgets";
import { Balances } from "./balances";
import { useLive } from "./providers";
const navigation = [
  ["spending", "home", "Spending"],
  ["transactions", "swap_horiz", "Transactions"],
  ["chat", "chat", "Chat"],
  ["review", "fact_check", "Review"],
  ["balances", "account_balance_wallet", "Balances"],
  ["budgets", "bar_chart", "Budgets"],
  ["settings", "settings", "Settings"],
] as const;
export function Dashboard({
  view,
  scope,
  nonce,
  fromSettings = false,
  chatEnabled = false,
}: {
  view: View;
  scope: Scope;
  nonce?: string;
  fromSettings?: boolean;
  chatEnabled?: boolean;
}) {
  const router = useRouter(),
    live = useLive();
  const bootstrapQuery = useQuery<Bootstrap>({
    queryKey: ["/v1/dashboard/bootstrap"],
  });
  const [payment, setPayment] = useState<string | null>(null),
    [message, setMessage] = useState(""),
    [confirmation, setConfirmation] = useState<string | null>(null),
    [undo, setUndo] = useState<string | null>(null),
    [busy, setBusy] = useState(false);
  const queryClient = useQueryClient();
  const [chartChange, setChartChange] = useState<{ path: string; stamp: number } | null>(null);
  function markChartChange() {
    if (view !== "spending") return;
    const path = spendingReportPath(scope);
    setChartChange({ path, stamp: queryClient.getQueryState([path])?.dataUpdatedAt ?? 0 });
  }
  const bootstrap = bootstrapQuery.data;
  const [updating, startNavigation] = useTransition();
  const [pendingScope, setPendingScope] = useState({ scope, label: "" });
  const destination = updating ? pendingScope.scope : scope;
  const detailTitle = view === "spending"
    ? spendingDetailTitle(destination.category, destination.merchant)
    : null;
  const content = useRef<HTMLElement>(null);
  const spendingLevel =
    view !== "spending" ? null : detailTitle ? "detail" : "overview";
  const previousLevel = useRef(spendingLevel);
  useLayoutEffect(() => {
    const previous = previousLevel.current;
    previousLevel.current = spendingLevel;
    const node = content.current;
    if (
      !node ||
      !previous ||
      !spendingLevel ||
      previous === spendingLevel ||
      matchMedia("(prefers-reduced-motion: reduce)").matches
    )
      return;
    const distance = Math.min(node.clientWidth * 0.3, 120);
    const start = spendingLevel === "detail" ? distance : -distance;
    const animation = node.animate(
      [
        { transform: `translateX(${start}px)`, opacity: 0.35 },
        { transform: "translateX(0)", opacity: 1 },
      ],
      {
        duration: 220,
        easing: getComputedStyle(node).getPropertyValue("--motion-ease").trim(),
      },
    );
    if (spendingLevel === "detail") node.dataset.enteringDetail = "";
    const reveal = () => { delete node.dataset.enteringDetail; };
    animation.addEventListener("finish", reveal);
    return () => {
      animation.removeEventListener("finish", reveal);
      reveal();
      animation.cancel();
    };
  }, [spendingLevel]);
  const budgetsHref =
    "/budgets?" +
    new URLSearchParams({
      period: scope.period,
      account_id: scope.account_id,
    });
  const navigate = (patch: Partial<Scope>, destination: View = view) => {
    const next = { ...scope, offset: 0, ...patch };
    const href = "/" + destination + "?" + scopeParams(next)
      + (fromSettings && destination === view ? "&from=settings" : "");
    if (view !== "spending" || destination !== "spending" || !bootstrap) {
      router.push(href, { scroll: false });
      return;
    }
    const period = next.mode === "year" ? next.period.slice(0, 4)
      : new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })
          .format(new Date(next.period + "-01T00:00:00Z"));
    const account = next.account_id === "combined" ? "all accounts" : accountName(next.account_id, bootstrap);
    const detail = !!next.category || next.merchant !== undefined;
    const subject = next.category ? label(next.category) : next.merchant || "Unnamed payment";
    setPendingScope({ scope: next, label: detail
      ? `Loading payments for ${subject} · ${period}…`
      : `Loading spending for ${period} · ${account}…` });
    startNavigation(() => router.push(href, { scroll: false }));
  };
  const open = (id: string) => {
    live.freeze(true);
    setPayment(id);
  };
  const close = () => {
    if (
      scope.category?.startsWith("custom:") &&
      bootstrap &&
      !bootstrap.categories.includes(scope.category)
    ) {
      router.replace(
        "/" + view + "?" + scopeParams({ ...scope, category: "", offset: 0 }),
        { scroll: false },
      );
    }
    setPayment(null);
    live.freeze(false);
  };
  if (!bootstrap)
    return (
      <Empty title="Unable to load dashboard">
        The data service is unavailable. Check your connection and refresh.
      </Empty>
    );
  const detailPage = detailTitle !== null;
  const backLink = (detailTitle ||
    view === "balances" ||
    view === "budgets") && (
    <Link
      className={detailPage ? "parent-link category-back" : "parent-link"}
      prefetch={false}
      aria-label={
        detailPage
          ? `${detailTitle}, back to Spending`
          : fromSettings
            ? "Back to settings"
            : "Back to spending overview"
      }
      href={
        detailTitle
          ? "/spending?" +
            scopeParams({
              ...scope,
              category: "",
              merchant: undefined,
              offset: 0,
            })
          : fromSettings
            ? "/settings"
            : "/spending"
      }
    >
      <svg
        aria-hidden="true"
        width="13"
        height="22"
        viewBox="0 0 13 22"
        fill="none"
      >
        <path
          d="M10 3 2.5 11l7.5 8"
          stroke="currentColor"
          strokeWidth="2.35"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span>{detailTitle ?? (fromSettings ? "Settings" : "Spending")}</span>
    </Link>
  );
  return (
    <>
      <style
        nonce={
          typeof document === "undefined"
            ? nonce
            : document.querySelector<HTMLScriptElement>("script[nonce]")
                ?.nonce || nonce
        }
      >
        {Object.entries(bootstrap.category_colours ?? {})
          .filter(
            ([key, colour]) =>
              key.startsWith("custom:") && /^#[0-9a-f]{6}$/i.test(colour),
          )
          .map(
            ([key, colour]) =>
              "." + categoryClass(key) + "{--chart-color:" + colour + "}",
          )
          .join("\n")}
      </style>
      <a className="skip" href="#content">
        Skip to content
      </a>
      <div id="app">
        <div className="workspace">
          <aside>
            <Link
              className="brand sidebar-brand"
              href="/spending"
              prefetch={false}
            >
              <span className="brand-mark">
                <Icon name="north_east" />
              </span>
              <span>Personal finance</span>
            </Link>
            <nav aria-label="Main navigation">
              {navigation.filter(([key]) => key !== "chat" || chatEnabled).map(([key, icon, title]) => (
                <Link
                  key={key}
                  className={
                    key === "balances" || key === "budgets"
                      ? "sidebar-only"
                      : undefined
                  }
                  href={
                    key === "transactions" || key === "chat" ||
                    key === "review" ||
                    key === "balances"
                      ? "/" + key
                      : key === "budgets"
                        ? budgetsHref
                        : "/" +
                          key +
                          "?" +
                          scopeParams({
                            ...scope,
                            category: "",
                            merchant: undefined,
                            offset: 0,
                            date_from: undefined,
                            date_to: undefined,
                          })
                  }
                  prefetch={false}
                  aria-current={view === key ? "page" : undefined}
                >
                  <Icon name={icon} className="nav-icon" />
                  <span>{title}</span>
                </Link>
              ))}
            </nav>
          </aside>
          <main ref={content} id="content" tabIndex={-1} data-view={view}>
            {!detailPage && backLink}
            {view === "transactions" && bootstrap.accounts.length > 0 ? (
              <TransactionFilters
                scope={scope}
                bootstrap={bootstrap}
                navigate={navigate}
              />
            ) : view === "chat" && bootstrap.accounts.length > 0 ? null : (
              <header
                className={
                  detailPage
                    ? "page-heading category-heading"
                    : "page-heading"
                }
              >
                <div>
                  <h1>
                    {detailPage ? backLink : (
                      {
                        balances: "Balances",
                        budgets: "Budgets",
                        spending: "Spending",
                        transactions: "Transactions",
                        chat: "Chat",
                        review: "Review",
                        settings: "Settings",
                      }[view]
                    )}
                  </h1>
                </div>
                {view === "spending" &&
                  !detailPage &&
                  bootstrap.accounts.length > 0 && (
                    <ScopeControls
                      preventFuture
                      scope={scope}
                      bootstrap={bootstrap}
                      navigate={navigate}
                    />
                  )}
              </header>
            )}
            {view === "spending" && (
              <div role="status" className="sr-only spending-update">
                {updating ? pendingScope.label : ""}
              </div>
            )}
            {live.updates && (
              <p className="update-note" role="status">
                New data will appear when you finish editing.
              </p>
            )}
            {bootstrapQuery.error && (
              <p role="alert" className="m-error">
                Connection unavailable. Showing the last successful data.
              </p>
            )}
            <PageNotice
              message={message}
              confirmation={confirmation !== null}
              busy={busy}
              dismiss={() => {
                setMessage("");
                setConfirmation(null);
                setUndo(null);
              }}
            >
              {message &&
                (confirmation === null ? (
                  message
                ) : (
                  <>
                    <svg
                      aria-hidden="true"
                      width="20"
                      height="20"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <circle cx="12" cy="12" r="9" />
                      <path d="m8 12 2.6 2.6 5.4-5.4" />
                    </svg>
                    <span className="confirmation-copy">
                      <strong>{message}</strong>
                      {confirmation && <small>{confirmation}</small>}
                    </span>
                  </>
                ))}
              {undo && (
                <button
                  className="undo-action"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    live.freeze(true);
                    try {
                      const result = await api<{ restored: number }>(
                        "/v1/dashboard/category-changes/" +
                          encodeURIComponent(undo) +
                          "/undo",
                        { method: "POST" },
                      );
                      setUndo(null);
                      markChartChange();
                      setConfirmation("Later category changes are kept.");
                      setMessage(
                        `${result.restored} payment${result.restored === 1 ? "" : "s"} restored`,
                      );
                    } catch (error) {
                      setConfirmation(null);
                      if (
                        error instanceof ApiError &&
                        (error.status === 404 || error.status === 409)
                      ) {
                        setUndo(null);
                        setMessage(error.message);
                      } else {
                        setMessage(
                          "Undo could not be confirmed. Tap Undo to retry.",
                        );
                      }
                    } finally {
                      setBusy(false);
                      live.refresh();
                      live.freeze(false);
                    }
                  }}
                >
                  {busy ? "Undoing…" : "Undo"}
                </button>
              )}
            </PageNotice>
            <div id="view">
              {bootstrap.accounts.length === 0 && view !== "settings" ? (
                <Empty title="No supported accounts connected">
                  <Link href="/settings" prefetch={false}>
                    Connect Monzo
                  </Link>{" "}
                  to see your Current account or Monzo Flex payments.
                </Empty>
              ) : view === "settings" ? (
                <>
                  <div
                    className="report-links"
                    role="navigation"
                    aria-label="Financial reports"
                  >
                    <Link href="/balances?from=settings" prefetch={false}>
                      Balances
                    </Link>
                    <Link
                      href={budgetsHref + "&from=settings"}
                      prefetch={false}
                    >
                      Budgets
                    </Link>
                  </div>
                  <Settings
                    bootstrap={bootstrap}
                    notify={(text) => {
                      setConfirmation(null);
                      setMessage(text);
                    }}
                  />
                </>
              ) : view === "budgets" ? (
                <Budgets
                  key={scope.period + scope.account_id}
                  scope={scope}
                  bootstrap={bootstrap}
                  navigate={navigate}
                />
              ) : view === "balances" ? (
                <Balances />
              ) : view === "spending" ? (
                <Spending
                  pendingDetail={updating ? detailPage : undefined}
                  chartChange={chartChange}
                  scope={scope}
                  bootstrap={bootstrap}
                  navigate={navigate}
                  open={open}
                />
              ) : view === "chat" ? (
                <SpendingChat />
              ) : view === "review" ? (
                <Review scope={scope} navigate={navigate} open={open} />
              ) : (
                <TransactionFeed
                  key={transactionsPath(scope)}
                  path={transactionsPath(scope)}
                  open={open}
                />
              )}
            </div>
          </main>
        </div>
      </div>
      {payment && (
        <PaymentDialog
          id={payment}
          reviewMode={view === "review" && scope.review_tab !== "payments"}
          clearSuggestion={
            view === "review" && scope.review_tab === "needs_review"
          }
          bootstrap={bootstrap}
          close={close}
          saved={(text, batch, detail) => {
            markChartChange();
            setConfirmation(detail ?? "");
            setMessage(text);
            setUndo(batch || null);
            live.refresh();
            close();
          }}
        />
      )}
    </>
  );
}
function Spending({
  pendingDetail,
  chartChange,
  scope,
  bootstrap,
  navigate,
  open,
}: {
  scope: Scope;
  bootstrap: Bootstrap;
  navigate: (patch: Partial<Scope>, view?: View) => void;
  pendingDetail?: boolean;
  chartChange: { path: string; stamp: number } | null;
  open: (id: string) => void;
}) {
  const result = useQuery<Report>({
    queryKey: [spendingReportPath(scope)],
  });
  const [group, setGroup] = useState<"categories" | "merchants">("categories");
  const report = result.data,
    drill = !!scope.category || scope.merchant !== undefined;
  if (pendingDetail !== undefined || (!report && !result.error)) return <SpendingSkeleton group={group} detail={pendingDetail ?? drill} />;
  if (!report)
    return (
      <p role="alert" className="m-error">
        {result.error?.message}
      </p>
    );
  return (
    <>
      {result.error && (
        <p className="m-error">
          Unable to refresh. Showing previously loaded figures.
        </p>
      )}
      <div className="spending-chart">
        <Chart
          change={chartChange?.path === spendingReportPath(scope) && chartChange.stamp !== result.dataUpdatedAt ? chartChange : null}
          report={report}
          scope={scope}
          accountLabel={
            report.combined
              ? "Combined (Current + Flex)"
              : accountName(scope.account_id, bootstrap)
          }
        />
      </div>
      {drill ? (
        <section
          className="detail-payments"
          data-category={scope.category || undefined}
          aria-label="Payments"
        >
          <TransactionFeed
            key={spendingPaymentsPath(scope)}
            path={spendingPaymentsPath(scope)}
            open={open}
            showEnd={false}
          />
        </section>
      ) : (
        <section className="spending-breakdown">
          <div
            className="breakdown-switch"
            role="group"
            aria-label="Group spending by"
          >
            {(["categories", "merchants"] as const).map((option) => (
              <button
                key={option}
                aria-pressed={group === option}
                onClick={() => setGroup(option)}
              >
                {option === "categories" ? "Categories" : "Merchants"}
              </button>
            ))}
          </div>
          {report.current.debits_minor > 0 && (
            <div className="breakdown-bar-slot">
              {group === "categories" && (
                <CategoryShareBar
                  key={spendingReportPath(scope)}
                  categories={report.categories}
                  total={report.current.debits_minor}
                />
              )}
            </div>
          )}
          {!report[group].length && (
            <Empty
              title={
                report.history_status === "imported"
                  ? "No settled money out"
                  : "No payments recorded for this period"
              }
            >
              Try another period. Pending payments are in Transactions.
            </Empty>
          )}
          <div className="breakdown-list" data-group={group}>
            {report[group].map((item) => (
              <button
                className="data-row"
                key={item.key}
                onPointerDown={(event) => {
                  if (event.pointerType === "touch")
                    event.currentTarget.dataset.pressed = "";
                }}
                onLostPointerCapture={(event) => {
                  delete event.currentTarget.dataset.pressed;
                }}
                onClick={() =>
                  navigate(
                    group === "categories"
                      ? { category: item.key }
                      : { merchant: item.key },
                  )
                }
              >
                {group === "categories" && (
                  <span
                    aria-hidden="true"
                    className={"spending-swatch " + categoryClass(item.key)}
                  />
                )}
                <span className="row-text">
                  <strong>
                    {group === "categories"
                      ? label(item.key)
                      : item.key || "Unnamed payment"}
                  </strong>
                  <small>
                    {item.count} payment{item.count === 1 ? "" : "s"}
                    {item.needs_review_count
                      ? ` · ${item.needs_review_count} ${item.needs_review_count === 1 ? "needs" : "need"} review`
                      : ""}
                  </small>
                </span>
                <span className="row-amount">
                  {money(item.amount_minor, scope.currency)}
                </span>
                <svg className="material-icon row-chevron" viewBox="0 -960 960 960" aria-hidden="true" focusable="false">
                  <path d="M504-480 348-636q-11-11-11-28t11-28q11-11 28-11t28 11l184 184q6 6 8.5 13t2.5 15q0 8-2.5 15t-8.5 13L404-268q-11 11-28 11t-28-11q-11-11-11-28t11-28l156-156Z" />
                </svg>
              </button>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
function CategoryShareBar({
  categories,
  total,
}: {
  categories: Report["categories"];
  total: number;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const bar = useRef<HTMLDivElement>(null);
  const segment = useRef<SVGSVGElement>(null);
  const tooltip = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const track = bar.current;
    const target = segment.current;
    const bubble = tooltip.current;
    if (!track || !target || !bubble) return;
    return positionTooltip(track, target, bubble);
  }, [selected, categories]);
  useEffect(() => {
    if (selected === null) return;
    function dismiss(event: PointerEvent) {
      if (
        event.target instanceof Node &&
        !bar.current?.contains(event.target)
      ) {
        setSelected(null);
      }
    }
    function escape(event: KeyboardEvent) {
      if (event.key === "Escape") setSelected(null);
    }
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [selected]);
  const describe = (item: Report["categories"][number]) =>
    label(item.key) +
    " · " +
    Math.round((100 * item.amount_minor) / total) +
    "%";
  const active = categories.find((item) => item.key === selected);
  return (
    <div
      className="spending-share-bar"
      ref={bar}
      role="group"
      aria-label="Share of spending by category"
      onPointerLeave={(event) => {
        if (event.pointerType === "mouse") setSelected(null);
      }}
    >
      {categories
        .filter((item) => item.amount_minor > 0)
        .map((item) => (
          <svg
            key={item.key}
            ref={selected === item.key ? segment : undefined}
            role="button"
            tabIndex={0}
            aria-label={describe(item)}
            className={categoryClass(item.key)}
            width={(100 * item.amount_minor) / total + "%"}
            height="44"
            onPointerEnter={(event) => {
              if (event.pointerType === "mouse") setSelected(item.key);
            }}
            onFocus={() => setSelected(item.key)}
            onBlur={() => setSelected(null)}
            onClick={() => setSelected(item.key)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                setSelected(item.key);
              }
            }}
          >
            <rect width="100%" height="7" y="18.5" rx="2" />
          </svg>
        ))}
      {active && (
        <span ref={tooltip} className="share-tooltip" aria-hidden="true">
          {describe(active)}
        </span>
      )}
    </div>
  );
}
