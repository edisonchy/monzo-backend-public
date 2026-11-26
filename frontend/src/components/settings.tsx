"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Bootstrap, Rule, Status } from "@/lib/types";
import { Empty, label } from "./ui";
import { HealthDetails, Reconnect } from "./settings-health";
import { Coverage } from "./quality-summary";
import { ManageCategories } from "./manage-categories";
import { useLive } from "./providers";
export function Settings({
  bootstrap,
  notify,
}: {
  bootstrap: Bootstrap;
  notify: (value: string) => void;
}) {
  const statusQuery = useQuery<Status>({
    queryKey: ["/v1/status"],
    refetchInterval: 30000,
  });
  const rules = useQuery<Rule[]>({ queryKey: ["/v1/dashboard/rules"] });
  const [busy, setBusy] = useState(false),
    [confirm, setConfirm] = useState(false);
  const live = useLive();
  const action = async (path: string, body?: unknown) => {
    setBusy(true);
    live.freeze(true);
    try {
      await api(path, { method: "POST", body });
      notify("Request saved.");
      live.refresh();
    } catch (error) {
      notify(
        (error instanceof Error ? error.message : "Request failed") +
          " Check the current state before retrying.",
      );
    } finally {
      live.freeze(false);
      setBusy(false);
      setConfirm(false);
    }
  };
  const status = statusQuery.data;
  if (!status)
    return (
      <Empty title="Unable to load settings">
        The data service is unavailable. Check your connection and refresh.
      </Empty>
    );
  const paused = status.categorisation.state === "paused";
  return (
    <>
      {(status.categorisation.state !== "running" ||
        status.connection.state !== "healthy") && (
        <p className="m-notice">
          Categorisation: {status.categorisation.state}. Bank connection:{" "}
          {status.connection.state}. Stored transactions remain available.
        </p>
      )}
      {statusQuery.error && (
        <p role="alert" className="m-error">
          Connection unavailable. Showing the last successful data.
        </p>
      )}
      <div className="settings-grid">
        <section className="m-card">
          <h2>Monzo connection</h2>
          <Reconnect
            status={status}
            busy={busy}
            notify={notify}
            action={action}
          />
          <div className="actions">
            <button
              className="m-button secondary"
              disabled={busy}
              onClick={() => void action("/v1/sync", { broad: false })}
            >
              Sync recent payments
            </button>
          </div>
        </section>
        <section className="m-card">
          <h2>Categorisation</h2>
          <Row name="Processing" value={status.categorisation.state} />
          <Row name="Queued jobs" value={status.categorisation.pending} />
          <Row name="AI" value={status.ai_enabled ? "Enabled" : "Disabled"} />
          <Row
            name="Automatic acceptance"
            value={status.ai_auto_accept ? "Enabled" : "Disabled"}
          />
          <p className="m-help">
            Bank sync and alerts continue while categorisation is paused.
          </p>
          {confirm ? (
            <div className="m-notice">
              <p>
                {paused
                  ? "Resume categorisation and retry the failed job?"
                  : "Pause categorisation until you resume?"}
              </p>
              <button
                className="m-button"
                disabled={busy}
                onClick={() =>
                  void action("/v1/processing/" + (paused ? "resume" : "pause"))
                }
              >
                Confirm {paused ? "resume" : "pause"}
              </button>{" "}
              <button
                className="m-button secondary"
                disabled={busy}
                onClick={() => setConfirm(false)}
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              className="m-button secondary"
              onClick={() => setConfirm(true)}
            >
              {paused ? "Resume" : "Pause"} categorisation
            </button>
          )}
        </section>
      </div>
      <ManageCategories bootstrap={bootstrap} notify={notify} />
      <HealthDetails status={status} />
      <Coverage />
      <section className="m-card breakdown">
        <h2>Your merchant rules</h2>
        <p className="m-help">These rules categorise future payments.</p>
        {rules.error && <p className="m-error">{rules.error.message}</p>}
        {rules.data?.length === 0 && (
          <p className="m-help">
            No saved rules. You can add one when choosing a payment’s category.
          </p>
        )}
        {rules.data?.map((rule) => (
          <RuleForm
            key={rule.id + ":" + rule.category}
            rule={rule}
            categories={bootstrap.categories}
            notify={notify}
          />
        ))}
      </section>
      <section className="m-card breakdown">
        <h2>Session</h2>
        <button
          className="m-button secondary"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await api("/dashboard/logout", { method: "POST" });
              window.dispatchEvent(new Event("session-expired"));
              window.location.replace("/spending");
            } catch (error) {
              notify(
                error instanceof Error ? error.message : "Sign out failed",
              );
              setBusy(false);
            }
          }}
        >
          Sign out
        </button>
      </section>
    </>
  );
}
function Row({ name, value }: { name: string; value: string | number }) {
  return (
    <div className="setting-row">
      <span>{name}</span>
      <strong>{value}</strong>
    </div>
  );
}
function RuleForm({
  rule,
  categories,
  notify,
}: {
  rule: Rule;
  categories: string[];
  notify: (message: string) => void;
}) {
  const [category, setCategory] = useState(rule.category),
    [busy, setBusy] = useState(false),
    [confirm, setConfirm] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const live = useLive();
  const save = async (remove: boolean) => {
    setBusy(true);
    live.freeze(true);
    try {
      await api(
        "/v1/dashboard/rules/" +
          encodeURIComponent(rule.id) +
          (remove
            ? "?" + new URLSearchParams({ expected_category: rule.category })
            : ""),
        {
          method: remove ? "DELETE" : "PUT",
          ...(!remove
            ? { body: { category, expected_category: rule.category } }
            : {}),
        },
      );
      notify(remove ? "Merchant rule disabled." : "Merchant rule saved.");
      live.refresh();
    } catch (error) {
      notify(
        (error instanceof Error ? error.message : "Unable to save rule") +
          " Refresh to check the current state before retrying.",
      );
      setUncertain(true);
    } finally {
      setBusy(false);
      live.freeze(false);
    }
  };
  return (
    <details className="rule-row merchant-rule">
      <summary>
        <strong>{rule.merchant}</strong>
        <span>{label(rule.category)}</span>
      </summary>
      <form
        onFocus={() => live.freeze(true)}
        onBlur={(event) => {
          if (
            !event.currentTarget.contains(event.relatedTarget as Node) &&
            !busy
          )
            live.freeze(false);
        }}
        onSubmit={(event) => {
          event.preventDefault();
          void save(false);
        }}
      >
        <select
          className="m-input"
          aria-label={"Category for " + rule.merchant}
          value={category}
          disabled={busy || uncertain}
          onChange={(e) => setCategory(e.target.value)}
        >
          {categories.map((c) => (
            <option key={c} value={c}>
              {label(c)}
            </option>
          ))}
        </select>
        <button className="m-button secondary" disabled={busy || uncertain}>
          Save rule
        </button>
        <button
          type="button"
          className="m-button secondary"
          disabled={busy || uncertain}
          onClick={() => setConfirm(!confirm)}
        >
          {confirm ? "Cancel" : "Disable"}
        </button>
        {confirm && (
          <button
            type="button"
            className="m-button danger"
            disabled={busy || uncertain}
            onClick={() => void save(true)}
          >
            Confirm disable
          </button>
        )}
      </form>
    </details>
  );
}
