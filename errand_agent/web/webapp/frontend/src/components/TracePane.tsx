import { useEffect, useRef } from "react";
import type { TraceEvent } from "../types";

type LooseEvent = TraceEvent & Record<string, unknown>;

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function label(event: TraceEvent): { kind: string; text: string; cls: string } {
  const e = event as LooseEvent;
  switch (event.event) {
    case "state_transition":
      return {
        kind: "STATE",
        text: `${asString(e.from)} → ${asString(e.to)}${e.tool ? ` · ${asString(e.tool)}` : ""}`,
        cls: "state",
      };
    case "tool_call": {
      const result = record(e.result);
      const status = asString(result.status) || "unknown";
      const latency =
        typeof e.latency_ms === "number" ? `${e.latency_ms.toFixed(2)} ms` : "";
      return {
        kind: "TOOL",
        text: `${asString(e.name)} → ${status}${latency ? ` · ${latency}` : ""}`,
        cls: `tool status-${status}`,
      };
    }
    case "model_call": {
      const usage = record(e.usage);
      const prompt = typeof usage.prompt_tokens === "number" ? usage.prompt_tokens : 0;
      const completion =
        typeof usage.completion_tokens === "number" ? usage.completion_tokens : 0;
      return {
        kind: "MODEL",
        text: `turn ${e.turn ?? "?"} · ${asString(e.model)} · ${prompt}+${completion} tok · $${e.estimated_cost_usd ?? 0}`,
        cls: "model",
      };
    }
    case "context": {
      const included = Array.isArray(e.included_memory_ids)
        ? e.included_memory_ids.length
        : 0;
      return {
        kind: "CONTEXT",
        text: `turn ${e.turn ?? "?"} · ${e.token_count ?? 0} tokens · ${included} memories`,
        cls: "context",
      };
    }
    case "outcome":
      return {
        kind: "OUTCOME",
        text: `${asString(e.outcome)} · ${asString(e.state)}`,
        cls: "outcome",
      };
    case "user_message":
      return { kind: "USER", text: asString(e.text), cls: "user" };
    case "intent": {
      const intent = record(e.intent);
      return {
        kind: "INTENT",
        text: `action=${asString(intent.action) || "?"} · service=${asString(intent.service) || "?"} · area=${asString(intent.area) || "?"}`,
        cls: "intent",
      };
    }
    case "memory_changes": {
      const changes = Array.isArray(e.changes) ? e.changes.join(", ") : "";
      return { kind: "MEMORY", text: changes, cls: "memory" };
    }
    case "approval_granted":
      return {
        kind: "APPROVAL",
        text: `granted · ${asString(e.fingerprint)}`,
        cls: "approval",
      };
    case "approval_invalidated":
      return {
        kind: "APPROVAL",
        text: `invalidated · ${asString(e.reason)}`,
        cls: "approval",
      };
    case "state_after_tool":
      return {
        kind: "STATE",
        text: `${asString(e.state)} · ${asString(e.tool)} → ${asString(e.status)}`,
        cls: "state",
      };
    case "run_start":
      return {
        kind: "RUN",
        text: `${asString(e.kind)} · ${asString(e.user_id)}`,
        cls: "run",
      };
    case "model_error":
      return { kind: "ERROR", text: asString(e.error), cls: "error" };
    default:
      return { kind: event.event.toUpperCase(), text: "", cls: "other" };
  }
}

export default function TracePane({
  events,
  runId,
}: {
  events: TraceEvent[];
  runId: string;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);

  return (
    <section className="pane trace-pane">
      <div className="pane-head">
        <div>
          <span className="pane-title">Live trace</span>
          <span className="muted">state · model · tools · latency</span>
        </div>
        <span className="pill mono">{runId ? runId.slice(0, 12) : "no run yet"}</span>
      </div>

      <div className="trace">
        {events.length === 0 && (
          <div className="empty">
            Run a request to see state transitions, model calls, tool arguments,
            and outcomes.
          </div>
        )}

        {events.map((event, index) => {
          const row = label(event);
          return (
            <div key={`${event.run_id}-${index}`} className={`trace-row ${row.cls}`}>
              <div className="trace-line">
                <span className="trace-kind">{row.kind}</span>
                <span className="trace-text">{row.text}</span>
                <span className="trace-ts">
                  {new Date(event.ts * 1000).toLocaleTimeString()}
                </span>
              </div>
              <details>
                <summary>payload</summary>
                <pre>{JSON.stringify(event, null, 2)}</pre>
              </details>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
    </section>
  );
}
