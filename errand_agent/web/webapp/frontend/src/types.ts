export type Role = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: Role;
  text: string;
  meta?: {
    state?: string;
    outcome?: string;
    proactive?: boolean;
    toolCalls?: number;
  };
}

export interface MaterialAction {
  action: string;
  provider_id?: string | null;
  slot?: string | null;
  price?: number | null;
  booking_id?: string | null;
  hold_id?: string | null;
  fingerprint: string;
}

export interface PendingApproval {
  user_id: string;
  action: MaterialAction;
  tool: string;
  arguments: Record<string, unknown>;
  prompt: string;
  fingerprint: string;
  created_at: number;
}

export interface TurnResult {
  reply: string;
  state: string;
  outcome: string;
  tool_calls: number;
  booking: Record<string, unknown> | null;
  pending_approval: PendingApproval | null;
  run_id: string;
}

export interface TraceEvent {
  run_id: string;
  ts: number;
  event: string;
  [key: string]: unknown;
}

export interface MemoryFact {
  id: number;
  user_id: string;
  key: string;
  value: string;
  source: "explicit" | "inferred" | string;
  confirmed: number;
  forgotten: number;
  created_at: number;
  updated_at: number;
}

export interface Booking {
  booking_id: string;
  provider_id: string;
  provider_name?: string | null;
  service: string;
  slot: string;
  price: number;
  status: string;
}

export interface SessionInfo {
  token: string;
  user_id: string;
  model: string;
  limits: Record<string, number>;
  recovery: string[];
}
