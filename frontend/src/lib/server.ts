import "server-only";
import { cookies } from "next/headers";
import { ApiError } from "./api";
// Fixed internal destination; no owner key or user-controlled proxy target.
export async function serverApi<T>(path: string): Promise<T> {
  const token = (await cookies()).get("__Host-monzo_session")?.value;
  if (!token) throw new ApiError("Please sign in", 401);
  const response = await fetch("http://api:8080" + path, {
    cache: "no-store",
    redirect: "error",
    headers: { cookie: "__Host-monzo_session=" + encodeURIComponent(token) },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok)
    throw new ApiError("Unable to load dashboard", response.status);
  return response.json();
}
