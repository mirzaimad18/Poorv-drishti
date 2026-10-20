export const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message: string, public status?: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem("purva-token");
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `${localStorage.getItem("purva-token-type") || "Bearer"} ${token}`);
  if (options.body && !(options.body instanceof URLSearchParams) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (options.body instanceof URLSearchParams && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/x-www-form-urlencoded");
  }
  try {
    const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const errorBody = await response.json() as { detail?: string; message?: string };
        message = errorBody.detail || errorBody.message || message;
      } catch { /* Non-JSON error responses use the status message. */ }
      throw new ApiError(message, response.status);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("The API is unavailable. Showing prototype data.");
  }
}

export async function withFallback<T>(request: Promise<T>, fallback: T): Promise<T> {
  try {
    return await request;
  } catch {
    return fallback;
  }
}
