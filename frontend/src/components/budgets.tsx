"use client";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { budgetMinor } from "@/lib/budgets";
import { budgetsPath } from "@/lib/scope";
import type { Bootstrap, BudgetItem, BudgetReport, Scope } from "@/lib/types";
import { BudgetDetails } from "./budget-details";
import { useLive } from "./providers";
import { label } from "./ui";

export function Budgets({
  scope,
  bootstrap,
  navigate,
}: {
  scope: Scope;
  bootstrap: Bootstrap;
  navigate: (patch: Partial<Scope>) => void;
}) {
  const live = useLive();
  const result = useQuery<BudgetReport>({
    queryKey: [budgetsPath(scope)],
  });
  const [editing, setEditing] = useState<BudgetItem | null>(null);
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [message, setMessage] = useState("");
  const form = useRef<HTMLFormElement>(null);
  const submitting = useRef(false);
  const freeze = live.freeze;
  useEffect(() => {
    freeze(editing !== null);
    return () => freeze(false);
  }, [editing, freeze]);
  useEffect(() => {
    if (editing) {
      form.current?.scrollIntoView({ block: "center" });
      form.current?.querySelector("input")?.focus();
    }
  }, [editing]);
  async function save(value: number | null) {
    if (!editing || submitting.current || blocked) return;
    submitting.current = true;
    setBusy(true);
    setMessage("");
    try {
      await api("/v1/dashboard/budgets", {
        method: "PUT",
        body: {
          period: scope.period,
          account_id: scope.account_id,
          currency: "GBP",
          category: editing.category,
          expected_version: editing.version,
          amount_minor: value,
        },
      });
      setEditing(null);
      setMessage(value === null ? "Target removed." : "Target saved.");
      await result.refetch();
    } catch (error) {
      setBlocked(true);
      setMessage(
        error instanceof ApiError && error.status === 409
          ? "This target changed or the month is now read-only. Reload the latest target before editing again."
          : "The save could not be confirmed. Reload the latest target to check whether it was saved before trying again.",
      );
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <section className="filters budget-controls" aria-label="Budget scope">
        <label>
          Month
          <input
            className="m-input"
            type="month"
            min="2000-01"
            max="2100-12"
            value={scope.period}
            disabled={!!editing || busy}
            onChange={(event) => {
              if (/^(20\d{2}|2100)-(0[1-9]|1[0-2])$/.test(event.target.value))
                navigate({
                  period: event.target.value,
                  mode: "month",
                  currency: "GBP",
                });
            }}
          />
        </label>
        <label>
          Accounts
          <select
            className="m-input"
            value={scope.account_id}
            disabled={!!editing || busy}
            onChange={(event) => navigate({ account_id: event.target.value })}
          >
            <option value="combined">Combined (Current + Flex)</option>
            {bootstrap.accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.kind === "uk_monzo_flex" ? "Flex" : "Current"} ·{" "}
                {account.id.slice(-6)}
              </option>
            ))}
          </select>
        </label>
      </section>
      <p
        id="budget-message"
        role={blocked ? "alert" : "status"}
        className={blocked ? "m-error" : "m-help"}
      >
        {message}
      </p>
      {result.error && (
        <p role="alert" className="m-error">
          Unable to refresh budgets. Any figures below are from the last
          successful load. Editing is unavailable until the connection recovers.
        </p>
      )}
      {editing && (
        <form
          ref={form}
          className="m-card budget-editor"
          onSubmit={(event) => {
            event.preventDefault();
            const value = budgetMinor(amount);
            if (value === null) {
              setMessage(
                "Enter a GBP amount from 0 to 10,000,000,000 with at most two decimal places.",
              );
              return;
            }
            void save(value);
          }}
        >
          <h2>
            {editing.category ? label(editing.category) : "Overall"} target ·{" "}
            {scope.period}
          </h2>
          <label>
            Monthly target (£)
            <input
              className="m-input"
              aria-describedby="budget-edit-help budget-message"
              inputMode="decimal"
              value={amount}
              disabled={busy || blocked}
              onChange={(event) => setAmount(event.target.value)}
              required
            />
          </label>
          <p id="budget-edit-help" className="m-help">
            Applies to this month and the selected accounts only. Zero is a
            valid target.
          </p>
          <div className="budget-actions">
            <button
              className="m-button"
              disabled={busy || blocked || !!result.error}
              type="submit"
            >
              {busy ? "Saving…" : "Save target"}
            </button>
            {editing.target_minor !== null && (
              <button
                type="button"
                className="m-button secondary"
                disabled={busy || blocked || !!result.error}
                onClick={() => void save(null)}
              >
                Remove target
              </button>
            )}
            {blocked ? (
              <button
                type="button"
                className="m-button secondary"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  const loaded = await result.refetch();
                  setBusy(false);
                  if (!loaded.isError) {
                    setBlocked(false);
                    setEditing(null);
                    setMessage(
                      "Latest targets loaded. Select a target to edit again.",
                    );
                  }
                }}
              >
                Discard draft and reload
              </button>
            ) : (
              <button
                type="button"
                className="m-button secondary"
                disabled={busy}
                onClick={() => {
                  setEditing(null);
                  setMessage("");
                }}
              >
                Cancel
              </button>
            )}
          </div>
        </form>
      )}
      {result.data ? (
        <BudgetDetails
          report={result.data}
          categories={bootstrap.categories}
          disabled={!!editing || busy || !!result.error}
          edit={(item) => {
            setEditing(item);
            setAmount(
              item.target_minor === null
                ? ""
                : (item.target_minor / 100).toFixed(2),
            );
            setBlocked(false);
            setMessage("");
          }}
        />
      ) : (
        !result.error && <p role="status">Loading budgets…</p>
      )}
    </>
  );
}
