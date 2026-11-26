"use client";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useCalendarSwipe } from "./use-calendar-swipe";
import type { ReactNode } from "react";
import type { Bootstrap, Scope } from "@/lib/types";
import { accountName } from "./ui";
import { useBackdropDismiss } from "./use-backdrop-dismiss";
import "./scope-controls.css";
import { FilterIcon } from "./filter-icon";
import { FilterGrip } from "./filter-grip";

export function Menu({
  title,
  value,
  children,
}: {
  title: string;
  value: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  const close = () => {
    ref.current?.removeAttribute("open");
    ref.current?.querySelector("summary")?.focus();
  };
  useEffect(() => {
    const outside = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node))
        ref.current?.removeAttribute("open");
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && ref.current?.open) {
        e.preventDefault();
        ref.current.removeAttribute("open");
        ref.current.querySelector("summary")?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, []);
  return (
    <div className="scope-control">
      <span className="scope-label">{title}</span>
      <details ref={ref} className="scope-menu">
        <summary aria-label={`${title}: ${value}`}>
          <span>{value}</span>
          <span aria-hidden="true">⌄</span>
        </summary>
        <div
          className="scope-popover"
          onClick={(e) => {
            if ((e.target as HTMLElement).closest("button[aria-pressed]"))
              close();
          }}
        >
          {children}
        </div>
      </details>
    </div>
  );
}

const months = Array.from({ length: 12 }, (_, index) =>
  new Intl.DateTimeFormat("en-GB", { month: "long", timeZone: "UTC" }).format(
    new Date(Date.UTC(2000, index, 1)),
  ),
);

type Chooser = "options" | "account" | "period";

export function ScopeControls({
  scope,
  bootstrap,
  navigate,
  hideAccount = false,
  preventFuture = false,
}: {
  hideAccount?: boolean;
  preventFuture?: boolean;
  scope: Scope;
  bootstrap: Bootstrap;
  navigate: (patch: Partial<Scope>) => void;
}) {
  const [open, setOpen] = useState<Chooser | null>(null);
  const [detailPage, setDetailPage] = useState<"account" | "period">("account");
  const [slidePages, setSlidePages] = useState(false);
  function openDetail(page: "account" | "period") {
    setDetailPage(page);
    setOpen(page);
  }
  const [active, setActive] = useState(false);
  const [pointerMotion, setPointerMotion] = useState(false);
  const [mode, setMode] = useState(scope.mode);
  const [period, setPeriod] = useState(scope.period);
  const [sheet, setSheet] = useState(false);
  const [account, setAccount] = useState(scope.account_id);
  const previousPeriod = useRef({ mode, period });
  const year = Number(period.slice(0, 4));
  const maxPeriod = preventFuture ? bootstrap.today.slice(0, 7) : "2100-12";
  const latestYear = Number(bootstrap.today.slice(0, 4));
  const pageEndFor = (value: number) =>
    latestYear - 6 * Math.floor((latestYear - value) / 6);
  const [pageEnd, setPageEnd] = useState(pageEndFor(year));
  const years = Array.from({ length: 6 }, (_, index) => pageEnd - 5 + index);
  const selectedAccount = sheet ? account : scope.account_id;
  const recorded = new Set(
    bootstrap.accounts
      .filter(
        (account) =>
          selectedAccount === "combined" || account.id === selectedAccount,
      )
      .flatMap((account) => account.recorded_years?.[scope.currency] ?? []),
  );
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  function dismiss(pointer: boolean) {
    const box = dialog.current;
    if (!box?.open) return;
    box.dataset.motion = pointer ? "pointer" : "instant";
    delete box.dataset.dragging;
    box.style.removeProperty("--drag-y");
    box.close();
    setActive(false);
  }
  const backdrop = useBackdropDismiss(() => dismiss(true));

  function show(chooser: Chooser, button: HTMLButtonElement, pointer: boolean) {
    trigger.current = button;
    setSlidePages(false);
    if (chooser !== "options") setDetailPage(chooser);
    setSheet(chooser === "options");
    setAccount(scope.account_id);
    setPointerMotion(pointer);
    setActive(true);
    setPageEnd(pageEndFor(Number(scope.period.slice(0, 4))));
    setMode(scope.mode);
    setPeriod(scope.period);
    setOpen(chooser);
  }
  useLayoutEffect(() => {
    const box = dialog.current;
    const button = trigger.current;
    if (!box || !button || !open || !active) return;
    function position() {
      if (!box || !button || sheet) return;
      const anchor = button.getBoundingClientRect();
      const left = Math.max(
        12,
        Math.min(
          window.innerWidth - box.offsetWidth - 12,
          anchor.right - box.offsetWidth,
        ),
      );
      const top = Math.max(
        12,
        Math.min(window.innerHeight - box.offsetHeight - 12, anchor.bottom + 5),
      );
      box.style.transformOrigin = `${Math.max(0, Math.min(box.offsetWidth, anchor.left + anchor.width / 2 - left))}px top`;
      box.style.left = left + "px";
      box.style.top = top + "px";
    }
    if (!box.open) {
      box.dataset.motion = pointerMotion ? "pointer" : "instant";
      box.showModal();
    }
    position();
    box
      .querySelector<HTMLElement>(
        sheet ? "#spending-chooser-title" : '[aria-pressed="true"]',
      )
      ?.focus({ preventScroll: true });
    const observer = new ResizeObserver(position);
    observer.observe(box);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
    };
  }, [open, active, pointerMotion, sheet]);

  function changeYear(value: number) {
    const next = value + "-" + period.slice(5);
    if (value === 2000 || value === Number(maxPeriod.slice(0, 4))) {
      dialog.current
        ?.querySelector<HTMLElement>(".chooser-year strong")
        ?.focus();
    }
    setPeriod(next > maxPeriod ? maxPeriod : next);
  }
  const previousAvailable =
    mode === "year"
      ? [...recorded].some((value) => value >= 2000 && value < years[0])
      : year > 2000;
  const nextAvailable =
    mode === "year"
      ? pageEnd < latestYear
      : year < Number(maxPeriod.slice(0, 4));
  const { track, turnPage, gestures } = useCalendarSwipe(
    previousAvailable,
    nextAvailable,
    (direction) => {
      if (mode === "year") setPageEnd(pageEnd + direction * 6);
      else changeYear(year + direction);
    },
    (forward) => {
      setMode(forward ? "year" : "month");
      setPageEnd(pageEndFor(year));
    },
  );
  const accounts = [
    { id: "combined", name: "Combined (Current + Flex)" },
    ...bootstrap.accounts.map((a) => ({
      id: a.id,
      name: accountName(a.id, bootstrap),
    })),
  ];

  return (
    <div className="spending-tools">
      {!hideAccount && (
        <button
          type="button"
          className="spending-tool view-options-trigger"
          aria-label="View options"
          aria-haspopup="dialog"
          aria-expanded={active}
          onClick={(e) => show("options", e.currentTarget, e.detail > 0)}
        >
          <FilterIcon />
        </button>
      )}
      {!hideAccount && (
        <button
          type="button"
          className="spending-tool"
          title="Choose account"
          aria-label="Choose account"
          aria-haspopup="dialog"
          onClick={(e) => show("account", e.currentTarget, e.detail > 0)}
        >
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <rect x="3" y="6" width="16" height="12" rx="2.5" />
            <path d="M6 3.5h12a4 4 0 0 1 4 4V15M4 10h14m-11 5h3" />
          </svg>
        </button>
      )}
      <button
        type="button"
        className="spending-tool"
        title="Choose reporting period"
        aria-label="Choose reporting period"
        aria-haspopup="dialog"
        onClick={(e) => show("period", e.currentTarget, e.detail > 0)}
      >
        <svg aria-hidden="true" viewBox="0 0 24 24">
          <rect x="3" y="5" width="18" height="16" rx="3" />
          <path d="M7 3v4m10-4v4M3 10h18m-13 4h2m3 0h2" />
        </svg>
      </button>
      {open && (
        <dialog
          className={
            "spending-chooser" +
            (open === "options" ? " view-options-menu" : "")
          }
          data-sheet={sheet || undefined}
          data-page={open}
          ref={dialog}
          {...backdrop}
          aria-label={open === "options" ? "View options" : undefined}
          aria-labelledby={
            open === "options" ? undefined : "spending-chooser-title"
          }
          onCancel={(event) => {
            event.preventDefault();
            dismiss(false);
          }}
          onClose={() => {
            if (!dialog.current?.open) {
              setActive(false);
              trigger.current?.focus({ preventScroll: true });
            }
          }}
        >
          {sheet && (
            <>
              <FilterGrip dialog={dialog} onDismiss={() => dismiss(true)} />
              <div className="filter-heading">
                {open !== "options" && (
                  <button
                    className="filter-back"
                    aria-label="Back to view options"
                    onClick={(event) => {
                      setSlidePages(event.detail > 0);
                      if (open === "period") {
                        setPeriod(previousPeriod.current.period);
                        setMode(previousPeriod.current.mode);
                      }
                      setOpen("options");
                    }}
                  >
                    ‹
                  </button>
                )}
                <h2 id="spending-chooser-title" tabIndex={-1}>
                  {open === "options"
                    ? "View options"
                    : open === "account"
                      ? "Account"
                      : "Reporting period"}
                </h2>
                {open === "options" && (
                  <button
                    aria-label="Close filters"
                    onClick={(e) => dismiss(e.detail > 0)}
                  >
                    ×
                  </button>
                )}
              </div>
            </>
          )}
          {!sheet && open !== "options" && (
            <div className="chooser-heading">
              <h2 id="spending-chooser-title">
                {open === "account" ? "Account" : "Reporting period"}
              </h2>
              {open === "account" && (
                <button
                  type="button"
                  className="chooser-action"
                  onClick={(e) => dismiss(e.detail > 0)}
                >
                  Close
                </button>
              )}
            </div>
          )}
          <div
            className="filter-viewport"
            onClickCapture={(event) => {
              if (sheet) setSlidePages(event.detail > 0);
            }}
          >
            <div
              className="filter-pages"
              data-slide={(sheet && slidePages) || undefined}
            >
              {sheet && (
                <div
                  className="filter-page filter-summary"
                  inert={open !== "options"}
                  aria-hidden={open !== "options" || undefined}
                >
                  <div className="view-options-rows">
                    <button type="button" onClick={() => openDetail("account")}>
                      Account{" "}
                      <span>
                        {account === "combined"
                          ? "Combined"
                          : accountName(account, bootstrap)}{" "}
                        <i aria-hidden="true">›</i>
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        previousPeriod.current = { mode, period };
                        setPageEnd(pageEndFor(year));
                        openDetail("period");
                      }}
                    >
                      Period{" "}
                      <span>
                        {mode === "year"
                          ? year
                          : months[Number(period.slice(5)) - 1] +
                            " " +
                            year}{" "}
                        <i aria-hidden="true">›</i>
                      </span>
                    </button>
                  </div>
                  <button
                    className="filter-apply"
                    disabled={mode === "year" && !recorded.has(year)}
                    onClick={(e) => {
                      dismiss(e.detail > 0);
                      if (
                        account !== scope.account_id ||
                        mode !== scope.mode ||
                        period !== scope.period
                      )
                        navigate({
                          account_id: account,
                          mode,
                          period,
                          ...(account !== scope.account_id && {
                            category: "",
                            merchant: undefined,
                          }),
                        });
                    }}
                  >
                    Apply
                  </button>
                </div>
              )}
              <div
                className="filter-page"
                inert={open === "options"}
                aria-hidden={open === "options" || undefined}
              >
                {detailPage === "account" ? (
                  <div className="chooser-accounts">
                    {accounts.map((a) => (
                      <button
                        type="button"
                        key={a.id}
                        aria-pressed={a.id === selectedAccount}
                        onClick={(e) => {
                          if (sheet) {
                            setAccount(a.id);
                            setOpen("options");
                            return;
                          }
                          dismiss(e.detail > 0);
                          if (a.id !== scope.account_id)
                            navigate({
                              account_id: a.id,
                              category: "",
                              merchant: undefined,
                            });
                        }}
                      >
                        {a.name}
                        {a.id === selectedAccount && (
                          <span aria-hidden="true">✓</span>
                        )}
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="calendar-swipe" {...gestures}>
                    <div
                      className="chooser-modes"
                      role="group"
                      aria-label="Period length"
                    >
                      {(["month", "year"] as const).map((value) => (
                        <button
                          type="button"
                          aria-pressed={mode === value}
                          key={value}
                          onClick={() => {
                            setMode(value);
                            setPageEnd(pageEndFor(year));
                          }}
                        >
                          {value === "month" ? "Monthly" : "Yearly"}
                        </button>
                      ))}
                    </div>
                    <div className="chooser-year">
                      <button
                        type="button"
                        disabled={!previousAvailable}
                        aria-label={
                          mode === "year"
                            ? "Previous six years"
                            : "Previous year"
                        }
                        onClick={() => turnPage(-1)}
                      >
                        ‹
                      </button>
                      <strong tabIndex={-1} aria-live="polite">
                        {mode === "year" ? years[0] + " – " + pageEnd : year}
                      </strong>
                      <button
                        type="button"
                        disabled={!nextAvailable}
                        aria-label={
                          mode === "year" ? "Next six years" : "Next year"
                        }
                        onClick={() => turnPage(1)}
                      >
                        ›
                      </button>
                    </div>
                    <div className="calendar-window">
                      <div className="calendar-track" ref={track}>
                        {[0, -1, 1].map((delta) => {
                          if (
                            (delta < 0 && !previousAvailable) ||
                            (delta > 0 && !nextAvailable)
                          )
                            return null;
                          const pageYear = year + delta;
                          const pageYears = years.map(
                            (value) => value + delta * 6,
                          );
                          return (
                            <div
                              key={delta}
                              className="calendar-page"
                              style={{ left: delta * 100 + "%" }}
                              inert={delta !== 0}
                              aria-hidden={delta !== 0 || undefined}
                            >
                              {mode === "year" ? (
                                <div
                                  className="chooser-months chooser-years"
                                  role="group"
                                  aria-label="Choose year"
                                >
                                  {pageYears.map((value) => (
                                    <button
                                      type="button"
                                      key={value}
                                      disabled={
                                        !recorded.has(value) || value < 2000
                                      }
                                      aria-pressed={
                                        recorded.has(value) && value === year
                                      }
                                      onClick={() => {
                                        changeYear(value);
                                        if (sheet) setOpen("options");
                                      }}
                                    >
                                      {value}
                                    </button>
                                  ))}
                                </div>
                              ) : (
                                <div
                                  className="chooser-months"
                                  role="group"
                                  aria-label="Choose month"
                                >
                                  {months.map((name, i) => {
                                    const value =
                                      pageYear +
                                      "-" +
                                      String(i + 1).padStart(2, "0");
                                    return (
                                      <button
                                        type="button"
                                        aria-label={name + " " + pageYear}
                                        key={name}
                                        disabled={value > maxPeriod}
                                        aria-pressed={value === period}
                                        onClick={() => {
                                          setPeriod(value);
                                          if (sheet) setOpen("options");
                                        }}
                                      >
                                        {name.slice(0, 3)}
                                      </button>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                    {!sheet && (
                      <div className="chooser-actions">
                        <button
                          type="button"
                          className="chooser-action"
                          onClick={(e) => dismiss(e.detail > 0)}
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="chooser-action"
                          disabled={mode === "year" && !recorded.has(year)}
                          onClick={(e) => {
                            dismiss(e.detail > 0);
                            if (mode !== scope.mode || period !== scope.period)
                              navigate({ mode, period });
                          }}
                        >
                          Done
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </dialog>
      )}
    </div>
  );
}
