import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { ChatMessage, PendingApproval } from "../types";
import ApprovalCard from "./ApprovalCard";

interface Props {
  messages: ChatMessage[];
  pending: PendingApproval | null;
  busy: boolean;
  onSend: (text: string) => void;
  onDecide: (decision: "yes" | "no") => void;
}

export default function ChatPane({
  messages,
  pending,
  busy,
  onSend,
  onDecide,
}: Props) {
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setText("");
    onSend(trimmed);
  };

  return (
    <section className="pane chat-pane">
      <div className="pane-head">
        <div>
          <span className="pane-title">Chat</span>
          <span className="muted">clarify → compare → approve → book</span>
        </div>
        <span className={`pill ${busy ? "working" : ""}`}>
          {busy ? "working…" : "ready"}
        </span>
      </div>

      <div className="messages">
        {messages.map((item) => (
          <div key={item.id} className={`message ${item.role}`}>
            <div className="bubble">
              <div className="msg-meta">
                <span className="who">
                  {item.role === "user"
                    ? "You"
                    : item.role === "assistant"
                      ? "Agent"
                      : "System"}
                </span>
                {item.meta?.proactive && <span className="tag proactive">proactive</span>}
                {item.meta?.state && <span className="tag">{item.meta.state}</span>}
                {typeof item.meta?.toolCalls === "number" && item.meta.toolCalls > 0 && (
                  <span className="tag">{item.meta.toolCalls} tools</span>
                )}
              </div>
              <p>{item.text}</p>
            </div>
          </div>
        ))}

        {pending && (
          <ApprovalCard pending={pending} busy={busy} onDecide={onDecide} />
        )}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={submit}>
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="e.g. Find a plumber in Indiranagar this week, mornings only, under ₹1,500."
          disabled={busy}
          aria-label="Message"
        />
        <button type="submit" disabled={busy || !text.trim()}>
          Send
        </button>
      </form>
      <div className="hint">
        Try “Move my AC servicing to Saturday”, “Show my bookings”, or “Forget my
        preferred area”.
      </div>
    </section>
  );
}
