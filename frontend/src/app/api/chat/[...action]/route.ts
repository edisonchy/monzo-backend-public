import { NextRequest, NextResponse } from "next/server";
import { serverApi } from "@/lib/server";
import { ApiError } from "@/lib/api";

type Params = { params: Promise<{ action: string[] }> };
const error = (detail: string, status: number) =>
  NextResponse.json({ detail }, { status });
async function handle(req: NextRequest, { params }: Params) {
  if (process.env.SPENDING_CHAT_ENABLED !== "true") return error("Chat unavailable.", 404);
  const action = (await params).action.join("/");
  const read = action === "state" || (action === "preference" && req.method === "GET") || /^progress\/[0-9a-f-]{36}$/i.test(action);
  if (!read && !["ask", "compact", "clear", "stop", "more", "preference"].includes(action))
    return error("Unknown action.", 404);
  if (req.method !== (read ? "GET" : "POST"))
    return error("Unsupported method.", 405);
  const origin = process.env.SPENDING_CHAT_PUBLIC_ORIGIN;
  if (
    !origin ||
    req.headers.get("host") !== new URL(origin).host ||
    req.headers.get("sec-fetch-site") === "cross-site" ||
    (!read &&
      (req.headers.get("origin") !== origin ||
        req.headers.get("x-dashboard-request") !== "1"))
  ) {
    return error("Chat access denied.", 403);
  }
  try {
    await serverApi("/v1/dashboard/revision");
    const token = process.env.SPENDING_CHAT_OWNER_TOKEN;
    if (!token || !/^[\w-]{43}$/.test(token))
      return error("The spending agent is not configured.", 503);
    let body;
    if (!read) {
      if (!req.headers.get("content-type")?.startsWith("application/json"))
        return error("Expected JSON.", 415);
      const raw = await req.text();
      if (raw.length > 8192) return error("Message too large.", 413);
      try {
        body = JSON.parse(raw);
      } catch {
        return error("Invalid request.", 400);
      }
      if (
        !body ||
        typeof body !== "object" ||
        Object.keys(body).some(
          (k) =>
            !(
              action === "preference" ? ["proposal_id", "decision"] : action === "more" ? ["version", "turn_id"] : (action === "clear" || action === "compact")
                ? ["version"]
                : ["version", "message", "request_id", "replaces"]
            ).includes(k),
        )
      )
        return error("Invalid request.", 400);
    }
    const upstream = await fetch(
      new URL(
        "/api/chat/" + action,
        process.env.SPENDING_AGENT_ORIGIN || "http://127.0.0.1:8770",
      ),
      {
        method: req.method,
        cache: "no-store",
        redirect: "error",
        signal: AbortSignal.timeout(180000),
        headers: {
          Accept:
            action === "ask" &&
            req.headers.get("accept") === "application/x-ndjson"
              ? "application/x-ndjson"
              : "application/json",
          cookie: "monzo_spending_trial=" + token,
          "Content-Type": "application/json",
        },
        body: body && JSON.stringify(body),
      },
    );
    if (upstream.headers.has("set-cookie"))
      return error("The agent conversation needs reconnecting.", 503);
    if (
      action === "ask" &&
      upstream.headers.get("content-type")?.startsWith("application/x-ndjson")
    ) {
      return new Response(upstream.body, {
        status: upstream.status,
        headers: {
          "Content-Type": "application/x-ndjson",
          "Cache-Control": "no-store",
          "X-Accel-Buffering": "no",
        },
      });
    }
    return NextResponse.json(await upstream.json(), {
      status: upstream.status,
      headers: { "Cache-Control": "no-store" },
    });
  } catch (e) {
    if (e instanceof ApiError)
      return error("Dashboard session unavailable.", e.status);
    return error("The spending agent is unavailable. Please retry.", 503);
  }
}
export { handle as GET, handle as POST };
