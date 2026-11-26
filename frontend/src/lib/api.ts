export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const method = options.method || "GET";
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    signal: AbortSignal.any([
      AbortSignal.timeout(20000),
      ...(options.signal ? [options.signal] : []),
    ]),
    headers: {
      ...(options.body !== undefined
        ? { "Content-Type": "application/json" }
        : {}),
      ...(method !== "GET" ? { "X-Dashboard-Request": "1" } : {}),
    },
    ...(options.body !== undefined
      ? { body: JSON.stringify(options.body) }
      : {}),
  });
  if (response.status === 401 && path !== "/dashboard/login") {
    window.dispatchEvent(new Event("session-expired"));
    throw new ApiError("Your session expired. Please sign in again.", 401);
  }
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      typeof data.detail === "string"
        ? data.detail
        : "The request could not be completed.",
      response.status,
    );
  return data as T;
}
