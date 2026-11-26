"use client";
import { useEffect, useRef, useState } from "react";
import { ChatAnswer } from "./chat-answer";
import "./spending-chat.css";

type Usage = {
  details?: {
    stage: string;
    input: number | null;
    output: number | null;
    cached: number | null;
    elapsed_ms: number | null;
  }[];
  calls: number;
  input: number;
  output: number;
  total: number;
  partial: boolean;
};
type Turn = {
  tool_trace?: { tool: string }[];
  payment_list?: { turn_id: number; count: number; more: boolean; note: string };
  interrupted?: boolean;
  message: string;
  answer: string;
  usage?: Usage;
  activity?: string[];
};
type State = {
  preference_offer?: PreferenceOffer | null;
  preference_removal?: Removal | null;
  review_due?: boolean;
  version: number;
  messages: Turn[];
  usage_today?: Usage;
  pending?: Ask;
  progress?: string;
};
type Ask = {
  version: number;
  request_id: string;
  message: string;
  replaces?: { request_id: string; message: string };
};
type Update =
  { type: "draft"; text: string } | { type: "progress"; status: string };
const activityLabels = new Map([
  ["Reading your spending question…", "Reviewing your question"],
  ["Checking get breakdown…", "Looking up spending totals"],
  ["Checking get time series…", "Checking spending over time"],
  ["Checking search transactions…", "Searching payments"],
  ["Checking get transaction…", "Checking payment details"],
  ["Checking get recurring payments…", "Checking recurring payments"],
  ["Checking find unusual activity…", "Checking for unusual spending"],
  ["Recalling earlier conversation…", "Looking up earlier messages"],
  [
    "Summarising the earlier spending conversation…",
    "Updating conversation context",
  ],
  ["Using the forecast skill…", "Preparing a spending forecast"],
  ["Using the pattern skill…", "Checking spending patterns"],
  [
    "Model temporarily unavailable. Retrying the current step…",
    "Retrying the current step",
  ],
]);

function Activity({
  steps: statuses,
  usage,
  pending = false,
}: {
  steps: string[];
  usage?: Usage;
  pending?: boolean;
}) {
  const steps = statuses.flatMap((status) => activityLabels.get(status) ?? []);
  if (!steps.length && !usage) return null;
  return (
    <details className="chat-activity">
      <summary>{pending ? steps.at(-1) + "…" : "View activity"}</summary>
      <ol>
        {steps.map((label, index) => (
          <li key={index}>{label}</li>
        ))}
      </ol>
      {usage && (
        <p className="chat-token-count">
          Tokens used: {usage.total.toLocaleString("en-GB")}
          {usage.partial ? " (partial)" : ""}
          <br />
          {usage.input.toLocaleString("en-GB")} input ·{" "}
          {usage.output.toLocaleString("en-GB")} output · {usage.calls} model{" "}
          {usage.calls === 1 ? "call" : "calls"}
        </p>
      )}
      {usage?.details && (
        <ol aria-label="Model calls">
          {usage.details.map((call, i) => (
            <li key={i}>
              {call.stage} · {call.input ?? "?"} in · {call.output ?? "?"} out ·{" "}
              {call.cached === null
                ? "cache unreported"
                : call.cached + " cached input"}{" "}
              ·{" "}
              {call.elapsed_ms === null
                ? "time unavailable"
                : (call.elapsed_ms / 1000).toFixed(2) + " s"}
            </li>
          ))}
        </ol>
      )}
    </details>
  );
}

async function agent<T>(
  action: string,
  body?: object,
  update?: (event: Update) => void,
): Promise<T> {
  const r = await fetch("/api/chat/" + action, {
    method: body ? "POST" : "GET",
    cache: "no-store",
    headers: body
      ? {
          "X-Dashboard-Request": "1",
          "Content-Type": "application/json",
          ...(update && { Accept: "application/x-ndjson" }),
        }
      : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok)
    throw Object.assign(new Error("Agent request failed"), {
      status: r.status,
    });
  if (
    !update ||
    !r.headers.get("content-type")?.startsWith("application/x-ndjson")
  )
    return r.json();
  if (!r.body) throw new Error("Missing reply stream");
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) throw new Error("Reply interrupted");
      buffer += value;
      let boundary;
      while ((boundary = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 1);
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "done") return event.state;
        if (event.type === "error")
          throw Object.assign(new Error("Agent request failed"), {
            status: event.status,
            detail: typeof event.detail === "string" ? event.detail : undefined,
          });
        if (event.type === "draft" || event.type === "progress") update(event);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
const preferenceNames = {
  concise: "Use shorter replies", detailed: "Use detailed replies",
  merchant_first: "Show the merchant first", amount_first: "Show the amount first",
  inline: "Show merchants on one line", bullets: "List merchants in bullets",
};
type PreferenceOffer = { id: string; preference: keyof typeof preferenceNames };
type Removal = PreferenceOffer & { status: "pending" | "cancelled" | "removed" | "expired" };
type Choice = "remove" | "cancel" | "save" | "dismiss";

function PreferenceChoice({item, removing, onChoice}: {
  item: PreferenceOffer; removing: boolean; onChoice: (choice: Choice) => Promise<void>
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function choose(choice: Choice) {
    if (saving) return;
    setSaving(true);
    try { await onChoice(choice); }
    catch { setError("Couldn’t confirm your choice. Try again or refresh."); }
    finally { setSaving(false); }
  }
  return <aside className="chat-choice" aria-labelledby="chat-choice-title">
    <h2 id="chat-choice-title">{removing ? "Remove preference?" : "Save preference?"}</h2>
    <p>{preferenceNames[item.preference]}</p>
    <div>
      <button autoFocus type="button" disabled={saving} onClick={() => void choose(removing ? "cancel" : "dismiss")}>
        {removing ? "Cancel" : "Dismiss"}
      </button>
      <button type="button" disabled={saving} onClick={() => void choose(removing ? "remove" : "save")}>
        {removing ? "Remove" : "Save"}
      </button>
    </div>
    {error && <small role="alert">{error}</small>}
  </aside>;
}

export function SpendingChat() {
  const [state, setState] = useState<State | null>(null);
  const [text, setText] = useState("");
  const [steps, setSteps] = useState<string[]>([]);
  const [replyDraft, setReplyDraft] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Ask | null>(null);
  const generation = useRef(0);
  const locked = useRef(false);
  const retry = useRef<Ask | null>(null);
  const confirm = useRef<HTMLDialogElement>(null);
  const options = useRef<HTMLDetailsElement>(null);
  const log = useRef<HTMLDivElement>(null);
  const preserveScroll = useRef(false);
  const turns = state?.messages ?? [];
  const choosingRemoval = state?.preference_removal?.status === "pending";
  const choice = choosingRemoval ? state?.preference_removal : state?.preference_offer;
  const watchReview = !!state?.review_due && !choice && !busy;
  const version = state?.version;
  useEffect(() => {
    if (!watchReview) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await agent<{proposal: PreferenceOffer | null; waiting: boolean}>("preference");
        if (!active) return;
        if (next.proposal || !next.waiting) {
          setState(current => current?.version === version && current
            ? {...current, preference_offer: next.proposal, review_due: next.waiting} : current);
          return;
        }
      } catch { /* Retry while the review is due. */ }
      if (active) timer = setTimeout(poll, 2500);
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [watchReview, version]);
  async function decidePreference(decision: Choice) {
    if (!choice) return;
    try { await agent("preference", {decision, proposal_id: choice.id}); }
    catch (e) { if ((e as {status?: number}).status !== 409) throw e; }
    const refreshed = await agent<State>("state");
    setState(refreshed);
    requestAnimationFrame(() => inputRef.current?.focus());
  }
  const inputRef = useRef<HTMLInputElement>(null);
  async function load(signal?: AbortSignal) {
    const g = ++generation.current;
    let attached: Ask | undefined;
    while (g === generation.current && !signal?.aborted) {
      try {
        const next = await agent<State>("state");
        if (g !== generation.current || signal?.aborted) return;
        setState(next);
        setPending(next.pending ?? null);
        setBusy(!!next.pending);
        locked.current = !!next.pending;
        if (!next.pending) {
          if (attached && next.version === attached.version) {
            setText(attached.message);
            retry.current = attached;
            setStatus("Couldn’t confirm a reply. Send again to retry.");
          } else setStatus("");
          return;
        }
        attached = next.pending;
        setSteps(next.progress ? [next.progress] : []);
        setStatus(next.progress ? "" : "Checking your question…");
      } catch {
        if (g !== generation.current || signal?.aborted) return;
        setStatus(
          attached
            ? "Reconnecting to your lookup…"
            : "Couldn’t connect to your spending agent.",
        );
        if (!attached) return;
      }
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => void load(controller.signal), 0);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    if (!preserveScroll.current && log.current) log.current.scrollTop = log.current.scrollHeight;
    preserveScroll.current = false;
  }, [state, pending, replyDraft]);
  async function send(input: string, replace?: Ask) {
    const message = input.trim();
    if (choice || !message || (locked.current && !replace) || !state) return;
    const g = ++generation.current;
    const body =
      retry.current?.version === state.version &&
      retry.current.message === message
        ? retry.current
        : {
            version: state.version,
            request_id: crypto.randomUUID(),
            message,
            ...(replace && {
              replaces: {
                request_id: replace.request_id,
                message: replace.message,
              },
            }),
          };
    const activity: string[] = [];
    retry.current = body;
    setSteps([]);
    setReplyDraft("");
    locked.current = true;
    setBusy(true);
    if (replace) {
      setState({ ...state, messages: [...turns, { message: replace.message, answer: "", interrupted: true }] });
    }
    setPending(body);
    setText("");
    setStatus("Checking your question…");
    try {
      const next = await agent<State>("ask", body, (event) => {
        if (generation.current !== g) return;
        if (event.type === "draft") {
          setReplyDraft(event.text);
          if (event.text) setStatus("");
        } else {
          const label = event.status;
          if (label && activity.at(-1) !== label) {
            activity.push(label);
            setSteps([...activity]);
            setStatus("");
          }
        }
      });
      if (g === generation.current) {
        setState(next);
        setStatus("");
        retry.current = null;
      }
    } catch (e) {
      if (g === generation.current) {
        setText(message);
        const failure = e as { status?: number; detail?: string };
        setStatus(failure.detail ?? "Couldn’t confirm a reply. Send again to retry.");
        if (failure.status === 409) {
          const next = await agent<State>("state").catch(() => null);
          if (next && g === generation.current) setState(next);
        }
      }
    } finally {
      if (g === generation.current) {
        setReplyDraft("");
        setPending(null);
        setBusy(false);
        locked.current = false;
      }
    }
  }
  async function showMore(turnId: number) {
    if (!state || locked.current) return;
    locked.current = true;
    setBusy(true);
    setStatus("");
    try {
      const next = await agent<State>("more", { version: state.version, turn_id: turnId });
      preserveScroll.current = true;
      setState(next);
    } catch (err) {
      const latest = await agent<State>("state").catch(() => null);
      if (latest) setState(latest);
      setStatus((err as {status?: number}).status === 409
        ? "The results changed. Run the search again."
        : "Couldn’t load more payments. Try again.");
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  async function control(action: "clear" | "compact" | "stop") {
    if (!state || (locked.current && !(action === "stop" && pending))) return;
    const g = ++generation.current;
    locked.current = true;
    setBusy(true);
    setReplyDraft("");
    setPending(null);
    try {
      const next = await agent<State>(
        action,
        action === "stop" ? pending! : { version: state.version },
      );
      if (g === generation.current) {
        const unchanged =
          next.messages.length === turns.length &&
          turns.every(
            (turn, i) =>
              turn.message === next.messages[i].message &&
              turn.answer === next.messages[i].answer,
          );
        setState(
          action === "stop" && unchanged ? { ...next, messages: turns } : next,
        );
        setText(action === "stop" ? pending!.message : "");
        setStatus(action === "stop" ? "Stopped." : action === "compact" ? "Context compacted." : "");
        retry.current = null;
      }
    } catch {
      const next = await agent<State>("state").catch(() => null);
      if (g === generation.current) {
        if (next) setState(next);
        setStatus(
          "Couldn’t confirm that change. Check the conversation and try again.",
        );
      }
    } finally {
      if (g === generation.current) {
        locked.current = false;
        setBusy(false);
      }
    }
  }
  const ready = !!state && !busy;
  return (
    <>
      <header className="page-heading chat-header">
        <h1>Chat</h1>
        <details
          className="chat-options"
          ref={options}
          onBlur={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget))
              e.currentTarget.open = false;
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.currentTarget.open = false;
              e.currentTarget.querySelector("summary")?.focus();
            }
          }}
        >
          <summary aria-label="Chat options" title="Chat options">
            •••
          </summary>
          <div>
            {state?.usage_today && (
              <p className="chat-token-count">
                Today’s tokens:{" "}
                {state.usage_today.total.toLocaleString("en-GB")}
                {state.usage_today.partial ? " (partial)" : ""}
              </p>
            )}
            <button disabled={!ready || !turns.length} onClick={() => {
              if (options.current) options.current.open = false;
              void control("compact");
            }}>
              Compact
            </button>
            <button
              disabled={!ready || !turns.length}
              onClick={() => {
                if (options.current) options.current.open = false;
                confirm.current?.showModal();
              }}
            >
              Clear chat
            </button>
          </div>
        </details>
      </header>
      <dialog
        className="chat-confirm"
        ref={confirm}
        aria-labelledby="clear-chat-title"
        aria-describedby="clear-chat-detail"
        onClose={() => options.current?.querySelector("summary")?.focus()}
      >
        <h2 id="clear-chat-title">Clear this chat?</h2>
        <p id="clear-chat-detail">
          This clears your conversation and its remembered context. Your
          transactions stay in your account.
        </p>
        <div>
          <button autoFocus onClick={() => confirm.current?.close()}>
            Cancel
          </button>
          <button
            onClick={() => {
              void control("clear");
              confirm.current?.close();
            }}
          >
            Clear chat
          </button>
        </div>
      </dialog>
      <section className="spending-chat" aria-label="Spending conversation">
        <div className="chat-log" role="log" aria-live="polite" ref={log}>
          {turns.map((t, i) => (
            <div key={i} className="chat-turn">
              <p className="chat-question">{t.message}</p>
              <Activity steps={t.activity ?? []} usage={t.usage} />
              {t.interrupted ? (
                <small className="chat-status">Interrupted by your next message</small>
              ) : (
                <>
                  {!(t.tool_trace?.some(e => e.tool === "request_preference_removal") &&
                    t.tool_trace.every(e => ["request_preference_removal", "list_learned_preferences"].includes(e.tool))) &&
                    <ChatAnswer text={t.answer} />}
                  {t.payment_list && (
                    <div className="chat-payment-page">
                      <small>Showing {t.payment_list.more ? "the first " : ""}{t.payment_list.count} payments</small>
                      {t.payment_list.more && (
                        <button type="button" className="chat-prompt" disabled={!ready}
                          onClick={() => void showMore(t.payment_list!.turn_id)}>
                          <span>Show more</span>
                        </button>
                      )}
                      {t.payment_list.note && <small>{t.payment_list.note}</small>}
                    </div>
                  )}
                </>
              )}
            </div>
          ))}
          {pending && <p className="chat-question">{pending.message}</p>}
          {pending && <Activity steps={steps} pending />}
          {pending && replyDraft && <ChatAnswer text={replyDraft} streaming />}
        </div>
        {status && (
          <div role="status" className="chat-status">
            {status}{" "}
            {!state && <button onClick={() => void load()}>Retry</button>}
          </div>
        )}
        {state && !turns.length && !pending && (
          <small className="chat-status">
            Your question and relevant spending reports go through your VPS to
            the model.
          </small>
        )}
        {ready && choice && <PreferenceChoice key={choice.id} item={choice} removing={choosingRemoval} onChoice={decidePreference} />}
        {state && !choice && <form
          className="chat-compose"
          onSubmit={(e) => {
            e.preventDefault();
            if (pending && text.trim()) void send(text, pending);
            else if (pending) void control("stop");
            else void send(text);
          }}
        >
          <input
            ref={inputRef}
            aria-label="Message"
            placeholder="Ask about your spending"
            value={text}
            maxLength={1000}
            onChange={(e) => setText(e.target.value)}
          />
          <button
            aria-label={
              pending
                ? text.trim()
                  ? "Send correction"
                  : "Stop response"
                : "Send message"
            }
            disabled={!pending && (!ready || !text.trim())}
          >
            {pending && !text.trim() ? "■" : "↑"}
          </button>
        </form>}
      </section>
    </>
  );
}
