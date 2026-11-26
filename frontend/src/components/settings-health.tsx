"use client";
import { useState } from "react";
import type { Status } from "@/lib/types";
import { api } from "@/lib/api";
import { date, label } from "./ui";

export function HealthDetails({ status }: { status: Status }) {
  return (
    <details
      className="settings-disclosure"
      open={
        status.queues.sync.delayed ||
        status.queues.category.delayed ||
        status.notifications.state === "delayed" ||
        status.queues.sync.overdue > 0 ||
        status.queues.category.overdue > 0 ||
        status.accounts.some((account) =>
          ["stale", "never_synced", "unknown"].includes(account.state),
        ) ||
        undefined
      }
    >
      <summary>Service health</summary>
      <div className="disclosure-body">
        <p className="m-help">
          Last checked {date(status.observed_at)}. Sync is stale after{" "}
          {status.stale_after_minutes} minutes.
        </p>
        {status.accounts.map((a) => (
          <div key={a.id} className="breakdown">
            <h3>{a.kind === "uk_retail" ? "Current account" : "Flex"}</h3>
            <p>
              {label(a.state)} · Payments checked through:{" "}
              {a.checked_through ? date(a.checked_through) : "Unknown"}
            </p>
            <p className="m-help">
              Last sync finished: {date(a.last_sync_at)}. Last webhook received:{" "}
              {a.last_webhook_at
                ? date(a.last_webhook_at)
                : "No receipt recorded"}
              . History: {label(a.history_status)}.
            </p>
            {["stale", "never_synced", "unknown"].includes(a.state) ? (
              <p className="m-notice">
                Sync recent payments. If this stays stale, check the sync queue
                and bank connection above.
              </p>
            ) : null}
          </div>
        ))}
        {!status.accounts.length && (
          <p>No Current or Flex account has been discovered yet.</p>
        )}
        {(["sync", "category"] as const).map((key) => {
          const q = status.queues[key];
          return (
            <div key={key} className="breakdown">
              <h3>{key === "sync" ? "Bank sync" : "Categorisation"} queue</h3>
              <p>
                {q.pending} queued · {q.running} running · {q.failed} failed
              </p>
              {q.pending > 0 && (
                <p className="m-help">
                  Oldest queued: {date(q.oldest_pending_at)}
                </p>
              )}
              {(q.delayed || q.overdue > 0) && (
                <p className="m-notice">
                  Work is delayed
                  {q.overdue
                    ? `; ${q.overdue} worker leases expired`
                    : ""}.{" "}
                  {key === "category" &&
                  status.categorisation.state === "paused"
                    ? "Resume categorisation when ready."
                    : "If it persists, check the worker and scheduling service."}
                </p>
              )}
              {q.failed > 0 && (
                <p className="m-help">
                  Failed jobs remain in this count after later recovery.{" "}
                  {key === "sync"
                    ? "Check last successful sync; request a recent sync if stale."
                    : "Check processing status; resume if paused."}
                </p>
              )}
            </div>
          );
        })}
        <div className="breakdown">
          <h3>Email alerts</h3>
          <p>
            {status.notifications.pending} awaiting acknowledgement ·{" "}
            {label(status.notifications.state)}
          </p>
          <p className="m-help">
            Last workflow acknowledgement:{" "}
            {date(status.notifications.last_acknowledged_at)}. This confirms the
            sending workflow completed, not inbox delivery.
          </p>
          {status.notifications.state === "delayed" && (
            <p className="m-notice">
              An alert has waited more than five minutes. Check the n8n delivery
              workflow and SMTP service. Oldest:{" "}
              {date(status.notifications.oldest_pending_at)}; up to{" "}
              {status.notifications.max_attempts} attempts.
            </p>
          )}
        </div>
      </div>
    </details>
  );
}

export function Reconnect({
  status,
  busy,
  notify,
  action,
}: {
  status: Status;
  busy: boolean;
  notify: (message: string) => void;
  action: (path: string) => Promise<void>;
}) {
  const [confirm, setConfirm] = useState(false),
    [starting, setStarting] = useState(false);
  const c = status.connection;
  const begin = async () => {
    setStarting(true);
    try {
      const result = await api<{ authorization_url: string }>(
        "/v1/oauth/start",
        { method: "POST" },
      );
      const url = new URL(result.authorization_url);
      if (url.origin !== "https://auth.monzo.com")
        throw new Error("Unexpected bank sign-in address");
      window.location.assign(url.href);
    } catch (error) {
      notify(
        error instanceof Error
          ? error.message
          : "Could not start Monzo reconnection",
      );
      setStarting(false);
    }
  };
  return (
    <>
      <p className="setting-row">
        <span>Bank access</span>
        <strong>{label(c.state)}</strong>
      </p>
      {c.reconnect_state === "pending" || c.reconnect_state === "exchanging" ? (
        <p className="m-notice">
          Reconnecting. Complete Monzo sign-in and approve access in the Monzo
          app. An interrupted attempt expires after ten minutes; starting again
          replaces it.
        </p>
      ) : null}
      {["expired", "failed", "cancelled"].includes(c.reconnect_state) && (
        <p className="m-notice">
          The last reconnection did not complete. Start again when ready; saved
          payments and categories are retained.
        </p>
      )}
      {c.state === "awaiting_approval" && (
        <p className="m-notice">
          Approve data access in the Monzo app, then check access below. This
          queues a seven-day reconciliation.
        </p>
      )}
      {c.state === "reauthorisation_required" && (
        <p className="m-notice">
          Bank access has expired or been rejected. Reconnect Monzo to resume
          sync.
        </p>
      )}
      {!c.oauth_configured && (
        <p className="m-notice">
          Dashboard reconnection needs the Monzo developer callback configured
          by the administrator.
        </p>
      )}
      {confirm ? (
        <div className="m-notice">
          <p>
            Continue to Monzo to restore bank access? Your saved history,
            categories and rules stay here. Approve data access in the Monzo app
            after signing in. This is separate from Personal Finance sign-in.
          </p>
          <button
            className="m-button"
            disabled={busy || starting}
            onClick={() => void begin()}
          >
            Continue to Monzo
          </button>{" "}
          <button
            className="m-button secondary"
            disabled={starting}
            onClick={() => setConfirm(false)}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          className="m-button secondary"
          disabled={busy || !c.oauth_configured}
          onClick={() => setConfirm(true)}
        >
          {c.state === "not_connected" ? "Connect Monzo" : "Reconnect Monzo"}
        </button>
      )}
      {c.state === "awaiting_approval" && (
        <button
          className="m-button secondary"
          disabled={busy || starting}
          onClick={() => void action("/v1/connection/discover")}
        >
          Check approved access
        </button>
      )}
    </>
  );
}
