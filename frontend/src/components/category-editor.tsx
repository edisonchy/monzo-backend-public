"use client";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { categoryClass, label } from "./ui";

type Usage = {
  payments: number;
  rules: number;
  budgets: number;
  revision: string;
};
type Props = {
  categories: string[];
  removed: (category: string, replacement: string | null) => void;
};

export function CategoryEditor({ categories, removed }: Props) {
  const [target, setTarget] = useState("");
  const custom = categories.filter((c) => c.startsWith("custom:"));
  return (
    <>
      <h3 className="category-editor-title">Your categories</h3>
      {custom.length ? (
        <div className="category-edit-rows">
          {custom.map((c) => (
            <button
              type="button"
              key={c}
              aria-label={"Delete " + label(c)}
              onClick={() => setTarget(c)}
            >
              <span className="remove-symbol" aria-hidden="true">
                −
              </span>
              <i aria-hidden="true" className={categoryClass(c)} />
              {label(c)}
            </button>
          ))}
        </div>
      ) : (
        <p>You haven’t added any categories yet.</p>
      )}
      {target && (
        <RemovalAlert
          key={target}
          target={target}
          categories={categories}
          cancel={() => setTarget("")}
          removed={(category, replacement) => {
            setTarget("");
            removed(category, replacement);
          }}
        />
      )}
    </>
  );
}

function RemovalAlert({
  target,
  categories,
  cancel,
  removed,
}: Props & { target: string; cancel: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [replacement, setReplacement] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path =
    "/v1/dashboard/categories?" + new URLSearchParams({ category: target });
  const usage = useQuery<Usage>({
    queryKey: [path],
    staleTime: 0,
    refetchOnMount: "always",
  });
  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => node?.close();
  }, []);
  const info = usage.data;
  const move = !!info && (info.payments > 0 || info.rules > 0);
  const disabled =
    busy ||
    !info ||
    usage.isFetching ||
    !!usage.error ||
    !!info.budgets ||
    (move && !replacement);
  async function confirm() {
    if (disabled || !info) return;
    setBusy(true);
    setError("");
    try {
      const dest = move ? replacement : null;
      await api("/v1/dashboard/categories", {
        method: "DELETE",
        body: { category: target, replacement: dest, revision: info.revision },
      });
      removed(target, dest);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Couldn’t delete this category.",
      );
      if (e instanceof ApiError && e.status === 409) await usage.refetch();
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="category-remove-alert"
      aria-labelledby="remove-title"
      aria-busy={busy}
      onCancel={(e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!busy) cancel();
      }}
    >
      <h3 id="remove-title">Delete “{label(target)}”?</h3>
      {info ? (
        <>
          {info.budgets > 0 ? (
            <p>
              Clear the{" "}
              {info.budgets === 1 ? "budget target" : "budget targets"} for this
              category in Budgets first.
            </p>
          ) : move ? (
            <>
              <p>
                The category will be deleted. Your payments stay in the app. Choose a new category for{" "}
                {[
                  info.payments > 0 &&
                    `${info.payments} payment${info.payments === 1 ? "" : "s"}`,
                  info.rules > 0 &&
                    `${info.rules} merchant rule${info.rules === 1 ? "" : "s"}`,
                ]
                  .filter(Boolean)
                  .join(" and ")}
                .
              </p>
              <select
                aria-label="Move to category"
                value={replacement}
                disabled={busy}
                onChange={(e) => setReplacement(e.target.value)}
              >
                <option value="">Move to…</option>
                {categories
                  .filter((c) => c !== target)
                  .map((c) => (
                    <option key={c} value={c}>
                      {label(c)}
                    </option>
                  ))}
              </select>
            </>
          ) : (
            <p>No payments use this category. It will be deleted.</p>
          )}
        </>
      ) : (
        !usage.error && <p>Checking category…</p>
      )}
      {(error || usage.error) && (
        <p role="alert">{error || usage.error?.message}</p>
      )}
      <footer>
        <button autoFocus type="button" disabled={busy} onClick={cancel}>
          Cancel
        </button>
        <button
          type="button"
          disabled={disabled}
          className="delete-action"
          onClick={confirm}
        >
          {busy ? "Deleting…" : move ? "Move and delete category" : "Delete category"}
        </button>
      </footer>
    </dialog>
  );
}
