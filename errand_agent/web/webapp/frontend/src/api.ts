import type {
  Booking,
  MemoryFact,
  SessionInfo,
  TraceEvent,
  TurnResult,
} from "./types";

let token = "";
let userId = "u1";

export function currentUserId(): string {
  return userId;
}

export async function initSession(): Promise<SessionInfo> {
  const res = await fetch("/api/session");
  if (!res.ok) {
    throw new Error(`Session failed: ${res.status}`);
  }
  const data = (await res.json()) as SessionInfo;
  token = data.token;
  userId = data.user_id;
  return data;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Web-Token": token,
    },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${body}`);
  }
  return (await res.json()) as T;
}

export const api = {
  message: (text: string) =>
    request<TurnResult>("/api/message", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, text }),
    }),

  approve: (decision: "yes" | "no") =>
    request<TurnResult>("/api/approve", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, decision }),
    }),

  events: () =>
    request<{ event: TurnResult | null }>("/api/events", {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    }),

  trace: (limit = 300) =>
    request<{ events: TraceEvent[] }>(
      `/api/trace?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
    ),

  memories: () =>
    request<{ memories: MemoryFact[] }>(
      `/api/memories?user_id=${encodeURIComponent(userId)}`,
    ),

  forget: (key: string) =>
    request<{ forgotten: boolean }>("/api/forget", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, key }),
    }),

  bookings: () =>
    request<{ bookings: Booking[] }>(
      `/api/bookings?user_id=${encodeURIComponent(userId)}`,
    ),

  simulateCancel: (providerId?: string) =>
    request<{
      affected: Booking[];
      event: TurnResult | null;
      provider_id?: string;
      message?: string;
    }>("/api/simulate-cancel", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, provider_id: providerId }),
    }),

  state: () =>
    request<Record<string, unknown>>(
      `/api/state?user_id=${encodeURIComponent(userId)}`,
    ),
};
