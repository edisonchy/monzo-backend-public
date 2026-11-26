"use client";
import { useEffect, useRef, useState } from "react";
import { FilterGrip } from "./filter-grip";
import { useBackdropDismiss } from "./use-backdrop-dismiss";
import { useQuery } from "@tanstack/react-query";
import type { Bootstrap, Scope } from "@/lib/types";
import { accountName, categoryClass, Icon, label } from "./ui";
import { useLive } from "./providers";
import { DatePicker } from "./date-picker";
import { api } from "@/lib/api";
import { amountFilter } from "@/lib/amount-filter";
const settlements = [
  ["all", "All payments"],
  ["settled", "Settled"],
  ["pending", "Pending"],
  ["declined", "Declined"],
];
const clear = {
  account_id: "combined",
  category: "",
  merchant: undefined,
  categories: [],
  merchants: [],
  accounts: [],
  min_amount: undefined,
  max_amount: undefined,
  date_from: undefined,
  date_to: undefined,
  status: "all",
  settlement: "all",
  source: "all",
};
type Step =
  "Filters" | "Date" | "Amount" | "Category" | "Account" | "Payment status";
const toggle = (values: string[], value: string) =>
  values.includes(value)
    ? values.filter((v) => v !== value)
    : [...values, value];
const textFor = (options: string[][], value: string) =>
  options.find(([key]) => key === value)?.[1] || value;
const dateText = (v?: string) =>
  v && !Number.isNaN(Date.parse(v))
    ? new Intl.DateTimeFormat("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      }).format(new Date(v + "T12:00:00Z"))
    : "Not selected";
function dateSummary(from?: string, to?: string) {
  if (from && from === to) return dateText(from);
  return [from, to]
    .filter(Boolean)
    .map((value) => dateText(value))
    .join(" – ");
}
function MerchantSearch({
  initial,
  submit,
  pick,
}: {
  initial: string;
  submit: (q: string) => void;
  pick: (name: string) => void;
}) {
  const [text, setText] = useState(initial);
  const [term, setTerm] = useState("");
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setTerm(text.trim()), 160);
    return () => clearTimeout(timer);
  }, [text]);
  const options = useQuery({
    queryKey: ["merchant-options", term],
    enabled: focused && !!term,
    queryFn: ({ signal }) =>
      api<{ items: string[] }>(
        "/v1/dashboard/merchant-options?q=" + encodeURIComponent(term),
        { signal },
      ),
  });
  return (
    <div
      className="merchant-search-wrap"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setFocused(false);
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") setFocused(false);
      }}
    >
      <form
        role="search"
        className="transaction-search"
        onSubmit={(e) => {
          e.preventDefault();
          setFocused(false);
          submit(text.trim());
        }}
      >
        <Icon name="search" />
        <input
          type="search"
          aria-label="Search by merchant or description"
          placeholder="Search by merchant or description"
          maxLength={200}
          value={text}
          onFocus={() => setFocused(true)}
          onChange={(e) => {
            setText(e.target.value);
            setFocused(true);
            if (!e.target.value && initial) submit("");
          }}
        />
      </form>
      {focused && term && term === text.trim() && (
        <div className="merchant-matches">
          <button
            onClick={() => {
              setFocused(false);
              submit(term);
            }}
          >
            Search for “{term}”
          </button>
          {options.data?.items.slice(0, 6).map((name) => (
            <button
              key={name}
              aria-label={"Filter by " + name}
              onClick={() => {
                setText("");
                setFocused(false);
                pick(name);
              }}
            >
              {name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function FilterSheet({
  initialStep,
  scope,
  bootstrap,
  close,
  apply,
}: {
  scope: Scope;
  bootstrap: Bootstrap;
  close: () => void;
  initialStep: Step;
  apply: (patch: Partial<Scope>) => void;
}) {
  const { freeze } = useLive();
  useEffect(() => {
    freeze(true);
    return () => freeze(false);
  }, [freeze]);
  const allAccounts = bootstrap.accounts.map((a) => a.id);
  const exiting = useRef<Animation | null>(null);
  function dismiss(finish = close) {
    const node = dialog.current;
    if (!node?.open || exiting.current) return;
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
      finish();
      return;
    }
    const style = getComputedStyle(node);
    const animation = node.animate(
      [
        { transform: style.transform },
        {
          transform: matchMedia("(max-width: 600px)").matches
            ? "translateY(100%)"
            : "scale(.98)",
          opacity: matchMedia("(max-width: 600px)").matches ? 1 : 0,
        },
      ],
      {
        duration: 220,
        easing: style.getPropertyValue("--motion-ease").trim(),
        fill: "forwards",
      },
    );
    exiting.current = animation;
    animation.onfinish = finish;
  }
  const backdrop = useBackdropDismiss(() => dismiss());
  const dialog = useRef<HTMLDialogElement>(null);
  const [step, setStep] = useState<Step | "From" | "To">(initialStep);
  const [calendar, setCalendar] = useState<{ field: "From" | "To"; visit: number }>({ field: "From", visit: 0 });
  const inCalendar = step === "From" || step === "To";
  const [draft, setDraft] = useState<Scope>({
    ...scope,
    category: "",
    merchant: undefined,
    account_id: "combined",
    categories: scope.categories?.length
      ? scope.categories
      : scope.category
        ? [scope.category]
        : [],
    merchants: scope.merchants?.length
      ? scope.merchants
      : scope.merchant !== undefined
        ? [scope.merchant]
        : [],
    accounts: scope.accounts?.length
      ? scope.accounts
      : scope.account_id !== "combined"
        ? [scope.account_id]
        : allAccounts,
  });
  const [singleDay, setSingleDay] = useState(scope.date_from === scope.date_to);
  const amounts = amountFilter(scope.currency);
  const [min, setMin] = useState(amounts.input(scope.min_amount)),
    [max, setMax] = useState(amounts.input(scope.max_amount));
  const [exact, setExact] = useState(scope.min_amount === scope.max_amount);
  const [picker, setPicker] = useState<Step>(initialStep);
  const [error, setError] = useState("");
  useEffect(() => {
    const node = dialog.current,
      before = document.activeElement as HTMLElement | null;
    node?.showModal();
    return () => {
      exiting.current?.cancel();
      exiting.current = null;
      node?.close();
      before?.focus();
    };
  }, []);
  useEffect(() => {
    dialog.current?.querySelector<HTMLElement>(".txn-heading h2")?.focus();
  }, [step]);
  const go = (value: Step) => {
    setError("");
    if (value !== "Filters") setPicker(value);
    setStep(value);
  };
  function openCalendar(field: "From" | "To") {
    setCalendar(previous => ({ field, visit: previous.visit + 1 }));
    setError("");
    setStep(field);
  }
  const clearStep = () => {
    setError("");
    if (step === "Filters") {
      setSingleDay(true);
      setDraft({ ...draft, ...clear, accounts: allAccounts });
      setMin("");
      setMax("");

    } else if (step === "Category") setDraft({ ...draft, categories: [] });
    else if (step === "Account") setDraft({ ...draft, accounts: allAccounts });
    else setDraft({ ...draft, settlement: "all" });
  };
  const noAccounts = !draft.accounts?.length;
  const combined = allAccounts.every((id) => draft.accounts?.includes(id));
  const done = () => {
    try {
      if (noAccounts) throw new Error("Select at least one account.");
      const low = amounts.minor(min),
        high = amounts.minor(exact ? min : max);
      if (low !== undefined && high !== undefined && Number(low) > Number(high))
        throw new Error("Minimum must not exceed maximum.");
      if (
        (draft.date_from || draft.date_to) &&
        (!draft.date_from ||
          !draft.date_to ||
          draft.date_from > draft.date_to ||
          draft.date_from < "2000-01-01" ||
          draft.date_to > bootstrap.today)
      )
        throw new Error(
          "Choose both dates, with From on or before To, between 2000 and today.",
        );
      if ((draft.merchants?.length || 0) > 100)
        throw new Error("Select up to 100 merchants.");
      const value = { ...draft, min_amount: low, max_amount: high, offset: 0 };
      setDraft(value);
      setError("");
      if (step === "Filters")
        dismiss(() =>
          apply({ ...value, accounts: combined ? [] : value.accounts }),
        );
      else go("Filters");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const presetDate = (days: number | "last-month" | null) => {
    if (days === null) {
      setDraft({ ...draft, date_from: undefined, date_to: undefined });
      return;
    }
    const end = new Date(bootstrap.today + "T12:00:00Z"),
      start = new Date(end);
    if (days === "last-month") {
      end.setUTCDate(0);
      start.setTime(end.getTime());
      start.setUTCDate(1);
    } else if (days === 90) {
      const day = start.getUTCDate();
      start.setUTCDate(1);
      start.setUTCMonth(start.getUTCMonth() - 3);
      const last = new Date(
        Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0),
      ).getUTCDate();
      start.setUTCDate(Math.min(day, last));
    } else start.setUTCDate(start.getUTCDate() - (days - 1));
    setDraft({
      ...draft,
      date_from: start.toISOString().slice(0, 10),
      date_to: end.toISOString().slice(0, 10),
    });
    setError("");
  };
  const summary = (title: Step) =>
    title === "Date"
      ? draft.date_from
        ? dateSummary(draft.date_from, draft.date_to)
        : "All time"
      : title === "Amount"
        ? exact && min
          ? amounts.symbol + min
          : min || (!exact && max)
            ? `${min ? amounts.symbol + min : "Any"} – ${max ? amounts.symbol + max : "Any"}`
            : "Any amount"
        : title === "Category"
          ? draft.categories?.length
            ? draft.categories.length === 1
              ? label(draft.categories[0])
              : `${draft.categories.length} selected`
            : "All categories"
          : title === "Account"
            ? noAccounts
              ? "None selected"
              : combined
                ? "Combined"
                : draft.accounts?.length === 1
                  ? accountName(draft.accounts[0], bootstrap)
                  : `${draft.accounts?.length} selected`
            : textFor(settlements, draft.settlement);
  const row = (title: Step) => (
    <button className="filter-nav-row" key={title} onClick={() => go(title)}>
      <span>{title}</span>
      <span className="txn-summary">
        {summary(title)} <i aria-hidden="true">›</i>
      </span>
    </button>
  );
  const values =
    picker === "Category"
      ? [...bootstrap.categories, "uncategorised"].map((c) => [c, label(c)])
      : picker === "Account"
        ? [
            ["combined", "Combined (Current + Flex)"],
            ...bootstrap.accounts.map((a) => [
              a.id,
              accountName(a.id, bootstrap),
            ]),
          ]
        : settlements;
  const selected =
    picker === "Category"
      ? draft.categories || []
      : picker === "Account"
        ? combined
          ? ["combined"]
          : draft.accounts || []
        : [draft.settlement];
  const filtered =
    !combined ||
    !!(
      min ||
      max ||
      draft.date_from ||
      draft.date_to ||
      draft.categories?.length ||
      draft.merchants?.length ||
      draft.settlement !== "all" ||
      draft.status !== "all" ||
      draft.source !== "all"
    );
  return (
    <dialog
      {...backdrop}
      ref={dialog}
      className="transaction-sheet reference-sheet"
      data-overview={step === "Filters"}
      aria-labelledby="reference-title"
      onCancel={(e) => {
        e.preventDefault();
        if (step === "Filters") dismiss();
        else go(inCalendar ? "Date" : "Filters");
      }}
    >
      <FilterGrip dialog={dialog} onDismiss={() => dismiss()} />
      <header className="filter-heading txn-heading">
        {step !== "Filters" && (
          <button
            className="txn-back"
            aria-label={inCalendar ? "Back to date range" : "Back to filters"}
            onClick={inCalendar ? () => go("Date") : done}
          >
            ‹
          </button>
        )}
        <h2 id="reference-title" tabIndex={-1}>
          {inCalendar ? "Date" : step}
        </h2>
        {step === "Filters" && (
          <button
            className="txn-close"
            aria-label="Close filters"
            onClick={() => dismiss()}
          >
            ×
          </button>
        )}
      </header>
      <div className="txn-filter-viewport">
        <div className="txn-filter-pages" data-picker={step !== "Filters"}>
          <div className="reference-body" inert={step !== "Filters"}>
            <div className="view-options-rows txn-overview">
              {row("Date")}
              {row("Account")}
              {row("Amount")}
              {row("Category")}
              {row("Payment status")}
            </div>
            {filtered && (
              <button className="txn-reset" onClick={clearStep}>
                Reset filters
              </button>
            )}
          </div>
          <div
            className="reference-body"
            key={picker}
            inert={step === "Filters"}
          >
            {picker === "Filters" ? null : picker === "Date" ? (
              <>
                <div
                  className="chooser-modes"
                  role="group"
                  aria-label="Date selection"
                >
                  <button
                    aria-pressed={singleDay}
                    onClick={() => {
                      if (inCalendar) go("Date");
                      setSingleDay(true);
                      const day = draft.date_from || draft.date_to;
                      setDraft({ ...draft, date_from: day, date_to: day });
                    }}
                  >
                    Single day
                  </button>
                  <button
                    aria-pressed={!singleDay}
                    onClick={() => setSingleDay(false)}
                  >
                    Date range
                  </button>
                </div>
                {singleDay ? (
                  <DatePicker
                    inline
                    title="Single day"
                    value={draft.date_from}
                    min="2000-01-01"
                    max={bootstrap.today}
                    change={(day) => {
                      setDraft({ ...draft, date_from: day, date_to: day });
                      go("Filters");
                    }}
                  />
                ) : (
                  <div className="txn-filter-viewport">
                    <div className="txn-filter-pages" data-picker={inCalendar}>
                      <div className="reference-body" inert={inCalendar}>
                        <div className="filter-options date-range-rows">
                          {(["From", "To"] as const).map(field => (
                            <button className="date-field" key={field} onClick={() => openCalendar(field)}>
                              <span>{field}</span>
                              <span>{dateText(field === "From" ? draft.date_from : draft.date_to)} <Icon name="date_range" /></span>
                            </button>
                          ))}
                        </div>
                        <div className="filter-presets">
                          {(
                            [
                              [null, "All time"],
                              ["last-month", "Last month"],
                              [7, "Last 7 days"],
                              [30, "Last 30 days"],
                              [90, "Last 3 months"],
                            ] as const
                          ).map(([days, title]) => (
                            <button
                              key={title}
                              onClick={() => { presetDate(days); go("Filters"); }}
                            >
                              {title}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="reference-body" inert={!inCalendar}>
                        {calendar.visit > 0 && (
                          <DatePicker
                            key={calendar.visit}
                            inline
                            title={calendar.field}
                            value={calendar.field === "From" ? draft.date_from : draft.date_to}
                            min={calendar.field === "To" && draft.date_from ? draft.date_from : "2000-01-01"}
                            max={calendar.field === "From" && draft.date_to && draft.date_to < bootstrap.today ? draft.date_to : bootstrap.today}
                            change={day => {
                              setDraft({ ...draft, [calendar.field === "From" ? "date_from" : "date_to"]: day });
                              go("Date");
                            }}
                          />
                        )}
                      </div>
                    </div>
                  </div>
                )}
              </>
            ) : picker === "Amount" ? (
              <>
                <div className="chooser-modes" aria-label="Amount selection">
                  <button
                    aria-pressed={exact}
                    onClick={() => {
                      setExact(true);
                      setMax("");
                    }}
                  >
                    Exact amount
                  </button>
                  <button aria-pressed={!exact} onClick={() => setExact(false)}>
                    Range
                  </button>
                </div>
                <div className="amount-fields">
                  <label>
                    <span>{exact ? "Amount" : "Minimum"}</span>
                    <span aria-hidden="true">{amounts.symbol}</span>
                    <input
                      aria-label={`${exact ? "Exact" : "Minimum"} amount in ${scope.currency}`}
                      inputMode={amounts.precision ? "decimal" : "numeric"}
                      placeholder={exact ? "0" : "No minimum"}
                      value={min}
                      onChange={(e) => setMin(e.target.value)}
                    />
                  </label>
                  {!exact && (
                    <label>
                      <span>Maximum</span>
                      <span aria-hidden="true">{amounts.symbol}</span>
                      <input
                        aria-label={`Maximum amount in ${scope.currency}`}
                        inputMode={amounts.precision ? "decimal" : "numeric"}
                        placeholder="No limit"
                        value={max}
                        onChange={(e) => setMax(e.target.value)}
                      />
                    </label>
                  )}
                </div>
                {!exact && (
                  <div className="filter-presets">
                    {[
                      ["", amounts.underFive, `Under ${amounts.symbol}5`],
                      ["5", "20", `${amounts.symbol}5–${amounts.symbol}20`],
                      ["20", "100", `${amounts.symbol}20–${amounts.symbol}100`],
                      [amounts.overHundred, "", `Over ${amounts.symbol}100`],
                    ].map(([a, b, title]) => (
                      <button
                        key={title}
                        onClick={() => {
                          setMin(a);
                          setMax(b);
                          go("Filters");
                        }}
                      >
                        {title}
                      </button>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <div className="filter-options">
                {values.map(([value, title]) => (
                  <label className="filter-option" key={value}>
                    {picker === "Category" && (
                      <i
                        aria-hidden="true"
                        className={"filter-dot " + categoryClass(value)}
                      />
                    )}
                    <span>{title}</span>
                    <input
                      type={picker === "Category" ? "checkbox" : "radio"}
                      name="filter-choice"
                      checked={selected.includes(value)}
                      onChange={() => {
                        if (picker === "Category")
                          setDraft({
                            ...draft,
                            categories: toggle(draft.categories || [], value),
                          });
                        else if (picker === "Account") {
                          setDraft({
                            ...draft,
                            accounts:
                              value === "combined" ? allAccounts : [value],
                          });
                          go("Filters");
                        } else {
                          setDraft({ ...draft, settlement: value });
                          go("Filters");
                        }
                      }}
                    />
                  </label>
                ))}
              </div>
            )}
            {step === "Category" && !!draft.categories?.length && (
                <button className="txn-reset" onClick={clearStep}>
                  Reset filter
                </button>
              )}
          </div>

        </div>
      </div>
      {noAccounts && (step === "Account" || step === "Filters") && (
        <p className="m-error" role="status">
          Select at least one account.
        </p>
      )}
      {error && (
        <p className="m-error" role="alert">
          {error}
        </p>
      )}
      <footer className="reference-footer">
        {(step === "Filters" || step === "Amount" || step === "Category") && (
          <button className="txn-apply" onClick={done} disabled={noAccounts}>
            {step === "Filters" ? "Apply" : "Done"}
          </button>
        )}
      </footer>
    </dialog>
  );
}
export function TransactionFilters({
  scope,
  bootstrap,
  navigate,
}: {
  scope: Scope;
  bootstrap: Bootstrap;
  navigate: (patch: Partial<Scope>) => void;
}) {
  const [open, setOpen] = useState<Step | null>(null);
  const selectedAccounts = scope.accounts?.length
    ? scope.accounts
    : scope.account_id !== "combined"
      ? [scope.account_id]
      : [];
  const accountFiltered =
    selectedAccounts.length &&
    !bootstrap.accounts.every((account) =>
      selectedAccounts.includes(account.id),
    );
  const categories = scope.categories?.length
    ? scope.categories
    : scope.category
      ? [scope.category]
      : [];
  const merchants = scope.merchants?.length
    ? scope.merchants
    : scope.merchant !== undefined
      ? [scope.merchant]
      : [];
  const accountSummary = accountFiltered
    ? selectedAccounts.map((id) => accountName(id, bootstrap)).join(", ")
    : "";
  const dates = dateSummary(scope.date_from, scope.date_to);
  const chips: { value: string; clear: Partial<Scope> }[] = [];
  if (accountSummary)
    chips.push({
      value: accountSummary,
      clear: { accounts: [], account_id: "combined" },
    });
  if (dates)
    chips.push({
      value: dates,
      clear: { date_from: undefined, date_to: undefined },
    });
  if (scope.min_amount !== undefined || scope.max_amount !== undefined) {
    const amt = amountFilter(scope.currency);
    const lower = amt.input(scope.min_amount),
      upper = amt.input(scope.max_amount);
    chips.push({
      value:
        lower === upper
          ? amt.symbol + lower
          : [
              lower && "From " + amt.symbol + lower,
              upper && "Up to " + amt.symbol + upper,
            ]
              .filter(Boolean)
              .join(" · "),
      clear: { min_amount: undefined, max_amount: undefined },
    });
  }
  for (const name of merchants)
    chips.push({
      value: name || "Unnamed payment",
      clear: {
        merchant: undefined,
        merchants: merchants.filter((m) => m !== name),
      },
    });
  for (const c of categories)
    chips.push({
      value: label(c),
      clear: { category: "", categories: categories.filter((v) => v !== c) },
    });
  if (scope.settlement !== "all")
    chips.push({
      value: textFor(settlements, scope.settlement),
      clear: { settlement: "all" },
    });
  // Keep filters from older bookmarked links visible and removable.
  if (scope.status !== "all")
    chips.push({ value: label(scope.status), clear: { status: "all" } });
  if (scope.source !== "all")
    chips.push({ value: label(scope.source), clear: { source: "all" } });
  const count = chips.length;
  return (
    <header className="transaction-tools">
      <div className="transaction-toolbar">
        <h1>Transactions</h1>
        <MerchantSearch
          key={scope.q}
          initial={scope.q}
          submit={(q) => navigate({ q, offset: 0 })}
          pick={(name) =>
            navigate({
              q: "",
              merchant: undefined,
              merchants: [...new Set([...merchants, name])],
              offset: 0,
            })
          }
        />
        <button
          type="button"
          className="transaction-filter-trigger"
          aria-label={count ? "Filters, " + count + " active" : "Filters"}
          aria-haspopup="dialog"
          data-active={!!count}
          onClick={() => setOpen("Filters")}
        >
          <Icon name="filter_list" />
        </button>
      </div>
      {!!count && (
        <div className="transaction-chips">
          {chips.map((chip) => (
            <button
              key={JSON.stringify(chip.clear)}
              aria-label={"Remove " + chip.value + " filter"}
              onClick={() => navigate({ ...chip.clear, offset: 0 })}
            >
              {chip.value} <span aria-hidden="true">×</span>
            </button>
          ))}
        </div>
      )}
      {open && (
        <FilterSheet
          initialStep={open}
          scope={scope}
          bootstrap={bootstrap}
          close={() => setOpen(null)}
          apply={(patch) => {
            navigate(patch);
            setOpen(null);
          }}
        />
      )}
    </header>
  );
}
