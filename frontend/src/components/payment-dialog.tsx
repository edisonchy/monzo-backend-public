"use client";
import { positionTooltip } from "./position-tooltip";
import { useLayoutEffect, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useBackdropDismiss } from "./use-backdrop-dismiss";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Bootstrap, Detail, Draft, Preview } from "@/lib/types";
import {
  categoryClass,
  accountName,
  Badge,
  date,
  Icon,
  label,
  money,
} from "./ui";
export function PaymentDialog({
  id,
  bootstrap,
  close,
  saved,
  reviewMode = false,
  clearSuggestion = false,
}: {
  id: string;
  reviewMode?: boolean;
  clearSuggestion?: boolean;
  bootstrap: Bootstrap;
  close: () => void;
  saved: (message: string, batch?: string, detail?: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const dragStart = useRef<number | null>(null);
  const motion = useRef<Animation | null>(null);
  const closing = useRef(false);
  function offset(node: HTMLDialogElement) {
    const transform = getComputedStyle(node).transform;
    return transform === "none" ? 0 : new DOMMatrix(transform).m42;
  }
  function settle(
    node: HTMLDialogElement,
    from: number,
    to: number,
    done?: () => void,
  ) {
    motion.current?.cancel();
    node.style.transform = to ? `translateY(${to}px)` : "";
    if (
      matchMedia("(prefers-reduced-motion: reduce)").matches ||
      Math.abs(to - from) < 1
    ) {
      done?.();
      return;
    }
    const style = getComputedStyle(node);
    const duration = style.getPropertyValue("--motion-normal").trim();
    const animation = node.animate(
      [
        { transform: `translateY(${from}px)` },
        { transform: `translateY(${to}px)` },
      ],
      {
        duration: parseFloat(duration) * (duration.endsWith("ms") ? 1 : 1000),
        easing: style.getPropertyValue("--motion-ease").trim(),
      },
    );
    motion.current = animation;
    animation.onfinish = () => {
      if (motion.current !== animation) return;
      motion.current = null;
      done?.();
    };
  }
  function dismiss() {
    const node = dialog.current;
    if (!node || closing.current) return;
    closing.current = true;
    dragStart.current = null;
    settle(node, offset(node), node.offsetHeight, close);
  }
  const backdrop = useBackdropDismiss(dismiss);
  const [editing, setEditing] = useState(false);
  const [entry, setEntry] = useState(0);
  const [slide, setSlide] = useState(false);
  const [header, setHeader] = useState<HTMLDivElement | null>(null);
  const categoryButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!editing && entry)
      categoryButton.current?.focus({ preventScroll: true });
  }, [editing, entry]);
  const detail = useQuery<Detail>({
    queryKey: ["/v1/dashboard/transactions/" + encodeURIComponent(id)],
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
  });
  useEffect(() => {
    const node = dialog.current;
    const before = document.activeElement as HTMLElement | null;
    if (!node) return;
    node.showModal();
    settle(node, node.offsetHeight, 0);
    return () => {
      motion.current?.cancel();
      node.close();
      before?.focus();
    };
  }, []);
  const tx = detail.data;
  const grip = (
    <div
      className="receipt-grip"
      aria-hidden="true"
      onPointerDown={(event) => {
        if (
          !event.isPrimary ||
          event.button !== 0 ||
          dialog.current?.querySelector('[aria-busy="true"]')
        )
          return;
        event.currentTarget.setPointerCapture(event.pointerId);
        const node = dialog.current!;
        const current = offset(node);
        motion.current?.cancel();
        motion.current = null;
        closing.current = false;
        node.style.transform = `translateY(${current}px)`;
        dragStart.current = event.clientY - current;
      }}
      onPointerMove={(event) => {
        if (dragStart.current === null || !event.isPrimary) return;
        dialog.current!.style.transform = `translateY(${Math.max(0, event.clientY - dragStart.current)}px)`;
      }}
      onPointerUp={(event) => {
        if (dragStart.current === null || !event.isPrimary) return;
        const shouldDismiss = event.clientY - dragStart.current >= 72;
        dragStart.current = null;
        const node = dialog.current!;
        if (shouldDismiss) dismiss();
        else settle(node, offset(node), 0);
      }}
      onLostPointerCapture={() => {
        if (dragStart.current === null) return;
        dragStart.current = null;
        const node = dialog.current!;
        settle(node, offset(node), 0);
      }}
    />
  );
  return (
    <dialog
      {...backdrop}
      ref={dialog}
      id="detail"
      className="payment-sheet"
      data-editing={editing || undefined}
      aria-labelledby="detail-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!event.currentTarget.querySelector('[aria-busy="true"]')) close();
      }}
    >
      <div id="detail-content">
        {grip}
        <div className="receipt-navbar" ref={setHeader}>
          {!editing && (
            <header className="receipt-heading">
              <h2 id="detail-title">{tx?.merchant || "Payment details"}</h2>
              <button
                type="button"
                onClick={dismiss}
                aria-label="Close payment details"
              >
                ×
              </button>
            </header>
          )}
        </div>
        <div className="receipt-viewport">
          <div className="receipt-pages" data-slide={slide || undefined}>
            <div
              className="receipt-page"
              inert={editing}
              aria-hidden={editing || undefined}
            >
              {detail.error && (
                <p className="m-error" role="alert">
                  {detail.error.message}
                </p>
              )}
              {!tx && !detail.error && <p>Loading payment…</p>}
              {tx && (
                <>
                  <p className="receipt-amount">
                    {money(tx.amount_minor, tx.currency)}
                  </p>
                  <p className="receipt-date">
                    <time dateTime={tx.created_at}>{date(tx.created_at)}</time>
                  </p>
                  <dl className="receipt-facts">
                    <div>
                      <dt>Account</dt>
                      <dd>{accountName(tx.account_id, bootstrap)}</dd>
                    </div>
                    <div>
                      <dt>Status</dt>
                      <dd>
                        {tx.declined
                          ? "Declined"
                          : tx.settled_at
                            ? "Settled"
                            : "Pending"}
                      </dd>
                    </div>
                    <div>
                      <dt>Category status</dt>
                      <dd>
                        {tx.declined ? "Not applicable" : <Badge tx={tx} />}
                      </dd>
                    </div>
                    <div>
                      <dt>Description</dt>
                      <dd>{tx.description || "No description"}</dd>
                    </div>
                  </dl>
                  {tx.card_verification || tx.declined ? (
                    <dl className="receipt-facts">
                      <div>
                        <dt>Category</dt>
                        <dd>
                          {tx.card_verification
                            ? "Not applicable"
                            : label(tx.category)}
                        </dd>
                      </div>
                    </dl>
                  ) : (
                    <button
                      type="button"
                      className="receipt-category"
                      aria-label={"Change category: " + label(tx.category)}
                      ref={categoryButton}
                      onClick={(event) => {
                        setSlide(event.detail > 0);
                        setEntry(entry + 1);
                        setEditing(true);
                      }}
                    >
                      <span>Category</span>
                      <span>
                        <i
                          aria-hidden="true"
                          className={categoryClass(tx.category)}
                        />
                        {label(tx.category)} <Icon name="chevron_right" />
                      </span>
                    </button>
                  )}
                </>
              )}
            </div>
            <div
              className="receipt-page"
              inert={!editing}
              aria-hidden={!editing || undefined}
            >
              {entry > 0 && tx && header && (
                <Picker
                  key={tx.id + ":" + tx.version + ":" + entry}
                  tx={tx}
                  reviewMode={reviewMode}
                  clearSuggestion={clearSuggestion}
                  categories={bootstrap.categories}
                  saved={saved}
                  header={header}
                  active={editing}
                  back={(animate) => {
                    setSlide(animate);
                    setEditing(false);
                  }}
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </dialog>
  );
}
function PaymentInfo({ name, children }: { name: string; children: string }) {
  const tipId = useId();
  const [state, setState] = useState<"closed" | "open" | "pinned">("closed");
  const root = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLSpanElement>(null);
  const visible = state !== "closed";
  useLayoutEffect(() => {
    const row = root.current?.parentElement;
    if (visible && row && root.current && bubble.current)
      return positionTooltip(row, root.current, bubble.current);
  }, [visible]);
  useEffect(() => {
    if (!visible) return;
    function outside(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setState("closed");
    }
    function escape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setState("closed");
    }
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [visible]);
  return (
    <span
      ref={root}
      className="future-info"
      onPointerEnter={(e) => {
        if (e.pointerType === "mouse" && !visible) setState("open");
      }}
      onPointerLeave={(e) => {
        if (
          e.pointerType === "mouse" &&
          state === "open" &&
          !root.current?.matches(":has(:focus-visible)")
        )
          setState("closed");
      }}
    >
      <button
        type="button"
        aria-label={`About ${name}`}
        aria-describedby={tipId}
        aria-expanded={visible}
        onFocus={() => setState("open")}
        onBlur={() => setState("closed")}
        onClick={() => setState(state === "pinned" ? "closed" : "pinned")}
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 20 20"
          width="18"
          height="18"
          fill="none"
        >
          <circle
            cx="10"
            cy="10"
            r="8"
            stroke="currentColor"
            strokeWidth="1.4"
          />
          <circle cx="10" cy="6.4" r="1" fill="currentColor" />
          <path
            d="M10 9v4.7"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
      </button>
      <span
        ref={bubble}
        className="share-tooltip"
        id={tipId}
        role="tooltip"
        hidden={!visible}
      >
        {children}
      </span>
    </span>
  );
}

const scopeOptions = [
  ["this", "This payment", ""],
  [
    "unverified",
    "Unverified payments",
    "Update this merchant’s payments that still need category verification.",
  ],
  [
    "all",
    "All existing payments",
    "Update all payments from this merchant, even those with verified categories.",
  ],
] as const;

function Picker({
  tx,
  categories,
  saved,
  back,
  header,
  active,
  reviewMode = false,
  clearSuggestion = false,
}: {
  tx: Detail;
  reviewMode?: boolean;
  clearSuggestion?: boolean;
  categories: string[];
  back: (animate: boolean) => void;
  header: HTMLElement;
  active: boolean;
  saved: (message: string, batch?: string, detail?: string) => void;
}) {
  const [step, setStep] = useState<"category" | "apply" | "scope">("category");
  const heading = useRef<HTMLHeadingElement>(null);
  const selectedRow = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!active) return;
    heading.current?.focus({ preventScroll: true });
    const row = selectedRow.current?.closest("label");
    const body = row?.closest(".picker-body");
    if (step === "category" && row && body) {
      const bounds = body.getBoundingClientRect(),
        item = row.getBoundingClientRect();
      if (item.bottom > bounds.bottom)
        body.scrollTop += item.bottom - bounds.bottom;
      else if (item.top < bounds.top) body.scrollTop += item.top - bounds.top;
    }
  }, [step, active]);
  const [draft, setDraft] = useState<Draft>({
    category: clearSuggestion ? "" : tx.category || "",
    scope:
      reviewMode && (tx.category_identity_id || tx.merchant_id)
        ? "unverified"
        : "this",
    future_rule: false,
    group_merchant: reviewMode && !!(tx.category_identity_id || tx.merchant_id),
    expected_version: tx.version,
  });
  const [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [uncertain, setUncertain] = useState(false);
  const preview = useQuery<Preview>({
    queryKey: ["category-preview", tx.id, draft],
    queryFn: ({ signal }) =>
      api(
        "/v1/dashboard/transactions/" +
          encodeURIComponent(tx.id) +
          "/category-preview",
        { method: "POST", body: draft, signal },
      ),
    enabled: step === "apply" && !!draft.category,
    retry: false,
    staleTime: 0,
    refetchOnMount: false,
  });
  const change = (patch: Partial<Draft>) => {
    setDraft({ ...draft, ...patch });
    setConfirmed(false);
  };
  const hasMerchant = !!(tx.category_identity_id || tx.merchant_id);
  const canChooseScope = hasMerchant && !reviewMode;
  const canContinue =
    Boolean(draft.category) &&
    !uncertain &&
    (reviewMode || draft.category !== tx.category);
  const result = preview.data;
  const disabled =
    busy ||
    uncertain ||
    !!preview.error ||
    preview.isFetching ||
    !result ||
    (!result.change_count && !draft.future_rule) ||
    (draft.scope === "all" && result.verified_replaced_count > 0 && !confirmed);
  return (
    <form
      aria-busy={busy}
      className={"category-picker" + (reviewMode ? " review-picker" : "")}
      onSubmit={async (event) => {
        event.preventDefault();
        if (step === "category") {
          if (canContinue) setStep("apply");
          return;
        }
        if (step !== "apply" || disabled || !result) return;
        setBusy(true);
        setError("");
        try {
          const applied = await api<{
            changed: number;
            batch_id: string;
            future_rule: boolean;
          }>(
            "/v1/dashboard/transactions/" +
              encodeURIComponent(tx.id) +
              "/category-change",
            {
              method: "PUT",
              body: {
                ...draft,
                preview_token: result.preview_token,
                confirm_verified: confirmed,
              },
            },
          );
          saved(
            `${applied.changed} payment${applied.changed === 1 ? "" : "s"} updated`,
            applied.batch_id,
            applied.future_rule ? "Future rule saved." : undefined,
          );
        } catch (error) {
          setError(
            (error instanceof Error
              ? error.message
              : "Unable to apply change") +
              " Close and reopen this payment to check the saved state before trying again.",
          );
          setUncertain(true);
        } finally {
          setBusy(false);
        }
      }}
    >
      {active &&
        createPortal(
          <header className="payment-heading">
            <button
              type="button"
              className="text-button"
              aria-label={
                step === "category"
                  ? "Back to receipt"
                  : step === "scope"
                    ? "Back to apply category"
                    : "Back to category"
              }
              disabled={busy}
              onClick={(event) => {
                setError("");
                if (step === "category") back(event.detail > 0);
                else setStep(step === "scope" ? "apply" : "category");
              }}
            >
              ‹
            </button>
            <h2 id="detail-title" ref={heading} tabIndex={-1}>
              {step === "category"
                ? "Category"
                : step === "scope"
                  ? "Apply to"
                  : "Apply category"}
            </h2>
            <span />
          </header>,
          header,
        )}
      <div className="receipt-viewport">
        <div className="receipt-pages picker-pages" data-step={step}>
          <div
            className="receipt-page picker-body"
            inert={step !== "category"}
            aria-hidden={step !== "category" || undefined}
          >
            <fieldset
              className="category-rows"
              disabled={busy || uncertain}
              aria-label="Choose a category"
            >
              {categories.map((category) => (
                <label key={category}>
                  <input
                    type="radio"
                    ref={draft.category === category ? selectedRow : undefined}
                    name="payment-category"
                    aria-label={label(category)}
                    checked={draft.category === category}
                    onChange={() => change({ category })}
                  />
                  <i aria-hidden="true" className={categoryClass(category)} />
                  <span>{label(category)}</span>
                  {draft.category === category && <b aria-hidden="true">✓</b>}
                </label>
              ))}
            </fieldset>
          </div>
          <div
            className="receipt-page picker-body"
            inert={step !== "apply"}
            aria-hidden={step !== "apply" || undefined}
          >
            <section className="category-scope-panel">
              <h3 className="scope-category">
                <i
                  aria-hidden="true"
                  className={categoryClass(draft.category)}
                />
                {label(draft.category)}
              </h3>
              <div className="scope-group">
                <button
                  type="button"
                  className="scope-row"
                  disabled={!canChooseScope || busy || uncertain}
                  onClick={() => setStep("scope")}
                >
                  Apply to{" "}
                  <span className="scope-value">
                    {scopeOptions.find(([value]) => value === draft.scope)?.[1]}
                    {canChooseScope && <Icon name="chevron_right" />}
                  </span>
                </button>
                <div className="future-switch">
                  <label htmlFor="future-payment-switch">Future payments</label>
                  <PaymentInfo name="future payments">
                    {tx.directional_group
                      ? "Use this category for future payments in the same group and direction."
                      : "Use this category for future payments from this merchant."}
                  </PaymentInfo>
                  <label className="future-switch-control">
                    <input
                      type="checkbox"
                      role="switch"
                      id="future-payment-switch"
                      checked={draft.future_rule}
                      disabled={
                        !(tx.category_identity_id || tx.merchant_id) ||
                        busy ||
                        uncertain
                      }
                      onChange={(e) =>
                        change({ future_rule: e.target.checked })
                      }
                    />
                    <span className="switch-track" />
                  </label>
                </div>
              </div>
              {!hasMerchant && (
                <p className="m-help">Only this payment can be updated.</p>
              )}
              {result && result.unchanged_count > 0 && (
                <p className="m-help">{result.unchanged_count} already in this category.</p>
              )}
              {(error || preview.error) && (
                <p className="m-error" role="alert">
                  {error || preview.error?.message}
                </p>
              )}
              {preview.error && !uncertain && (
                <button
                  type="button"
                  className="m-button secondary"
                  onClick={() => void preview.refetch()}
                >
                  Refresh preview
                </button>
              )}
            </section>
          </div>
          <div
            className="receipt-page picker-body"
            inert={step !== "scope"}
            aria-hidden={step !== "scope" || undefined}
          >
            <section className="category-scope-panel">
              <h3 className="scope-title">Which payments?</h3>
              <div className="scope-group">
                {scopeOptions.map(([value, name, hint]) => (
                  <div className="scope-option-row" key={value}>
                    <button
                      type="button"
                      className="scope-row"
                      aria-pressed={draft.scope === value}
                      onClick={() => {
                        change({ scope: value });
                        setStep("apply");
                      }}
                    >
                      <span>{name}</span>
                    </button>
                    {hint && (
                      <PaymentInfo name={name.toLowerCase()}>
                        {hint}
                      </PaymentInfo>
                    )}
                    {draft.scope === value && <b aria-hidden="true">✓</b>}
                  </div>
                ))}
              </div>
            </section>
          </div>
        </div>
      </div>
      {step !== "scope" && (
        <footer className="picker-actions">
          {step === "apply" &&
            draft.scope === "all" &&
            !!result?.verified_replaced_count && (
              <label className="check-label verified-confirmation">
                <svg
                  aria-hidden="true"
                  width="19"
                  height="19"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinejoin="round"
                  strokeLinecap="round"
                >
                  <path d="M10.4 4.4a1.85 1.85 0 0 1 3.2 0l8 13.9a1.85 1.85 0 0 1-1.6 2.8H4a1.85 1.85 0 0 1-1.6-2.8zM12 9v4.3" />
                  <circle
                    cx="12"
                    cy="16.7"
                    r=".95"
                    fill="currentColor"
                    stroke="none"
                  />
                </svg>
                <span>
                  {result.verified_replaced_count} verified{" "}
                  {result.verified_replaced_count === 1
                    ? "payment"
                    : "payments"}{" "}
                  will change to {label(draft.category)}.
                </span>
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={busy || uncertain}
                  onChange={(e) => setConfirmed(e.target.checked)}
                />
              </label>
            )}
          <button
            className="m-button"
            disabled={step === "category" ? !canContinue : disabled}
          >
            {step === "category"
              ? "Continue"
              : busy
                ? "Applying…"
                : preview.isFetching
                  ? "Checking payments…"
                  : result?.change_count
                    ? `Update ${result.change_count} payment${result.change_count === 1 ? "" : "s"}`
                    : draft.future_rule && result
                      ? "Save future rule"
                      : result
                        ? "No payments to update"
                        : "Update payments"}
          </button>
        </footer>
      )}
    </form>
  );
}
