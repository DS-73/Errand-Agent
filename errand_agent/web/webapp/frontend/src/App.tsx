import { useCallback, useEffect, useRef, useState } from "react";
import { api, initSession } from "./api";
import ChatPane from "./components/ChatPane";
import MemoryPanel from "./components/MemoryPanel";
import TracePane from "./components/TracePane";
import type {
  ChatMessage,
  MemoryFact,
  PendingApproval,
  SessionInfo,
  TraceEvent,
  TurnResult,
} from "./types";

let nextId = 1;

function message(
  role: ChatMessage["role"],
  text: string,
  meta?: ChatMessage["meta"],
): ChatMessage {
  return { id: `m${nextId++}`, role, text, meta };
}

const WELCOME: ChatMessage[] = [
  message(
    "assistant",
    "Hi. Tell me what you need — service, area, and a day or date range. I'll compare providers, hold a slot, and ask before booking anything.",
    { state: "NEW" },
  ),
];

interface Status {
  state: string;
  outcome: string;
  runId: string;
  toolCalls: number;
}

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>(WELCOME);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [pending, setPending] = useState<PendingApproval | null>(null);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [memories, setMemories] = useState<MemoryFact[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [status, setStatus] = useState<Status>({
    state: "NEW",
    outcome: "",
    runId: "",
    toolCalls: 0,
  });

  const busyRef = useRef(false);
  const pollRef = useRef(false);

  const refreshTrace = useCallback(async () => {
    try {
      const data = await api.trace();
      setTrace(data.events);
    } catch {
      // Transient polling failures should not spam the chat.
    }
  }, []);

  const loadMemories = useCallback(async () => {
    try {
      const data = await api.memories();
      setMemories(data.memories);
    } catch {
      // The drawer can retry on the next open.
    }
  }, []);

  const applyResult = useCallback(
    (result: TurnResult, options: { proactive?: boolean } = {}) => {
      setMessages((prev) => [
        ...prev,
        message("assistant", result.reply, {
          state: result.state,
          outcome: result.outcome,
          proactive: options.proactive,
          toolCalls: result.tool_calls,
        }),
      ]);
      setPending(result.pending_approval);
      setStatus({
        state: result.state,
        outcome: result.outcome,
        runId: result.run_id,
        toolCalls: result.tool_calls,
      });
    },
    [],
  );

  useEffect(() => {
    let mounted = true;
    initSession()
      .then((info) => {
        if (!mounted) return;
        setSession(info);
        if (info.recovery.length > 0) {
          setMessages((prev) => [
            ...prev,
            ...info.recovery.map((line) => message("system", `Recovery: ${line}`)),
          ]);
        }
        void refreshTrace();
        void loadMemories();
      })
      .catch((err: unknown) => {
        if (mounted) setError(String(err));
      });
    return () => {
      mounted = false;
    };
  }, [refreshTrace, loadMemories]);

  useEffect(() => {
    const id = window.setInterval(() => {
      void refreshTrace();
    }, 1500);
    return () => window.clearInterval(id);
  }, [refreshTrace]);

  useEffect(() => {
    const id = window.setInterval(async () => {
      if (busyRef.current || pollRef.current) return;
      pollRef.current = true;
      try {
        const data = await api.events();
        if (data.event) {
          applyResult(data.event, { proactive: true });
          await refreshTrace();
        }
      } catch {
        // A missed poll is harmless; the next one retries.
      } finally {
        pollRef.current = false;
      }
    }, 4000);
    return () => window.clearInterval(id);
  }, [applyResult, refreshTrace]);

  const send = async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setMessages((prev) => [...prev, message("user", trimmed)]);
    try {
      const result = await api.message(trimmed);
      applyResult(result);
      await refreshTrace();
      await loadMemories();
    } catch (err: unknown) {
      setError(String(err));
      setMessages((prev) => [
        ...prev,
        message("system", "Request failed. Check that the API server is running and try again."),
      ]);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const decide = async (decision: "yes" | "no") => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setMessages((prev) => [
      ...prev,
      message("user", decision === "yes" ? "Yes, confirm." : "No, cancel."),
    ]);
    try {
      const result = await api.approve(decision);
      applyResult(result);
      await refreshTrace();
    } catch (err: unknown) {
      setError(String(err));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const simulateCancel = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const data = await api.simulateCancel();
      if (data.affected.length === 0) {
        setMessages((prev) => [
          ...prev,
          message("system", data.message ?? "No confirmed booking to cancel."),
        ]);
      } else {
        setMessages((prev) => [
          ...prev,
          message(
            "system",
            `Simulated provider cancellation for ${data.affected.length} booking(s).`,
          ),
        ]);
        if (data.event) applyResult(data.event, { proactive: true });
      }
      await refreshTrace();
    } catch (err: unknown) {
      setError(String(err));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const forget = async (key: string) => {
    try {
      await api.forget(key);
      await loadMemories();
    } catch (err: unknown) {
      setError(String(err));
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          <div>
            <h1>
              OrangeInvest <span>Errand Agent</span>
            </h1>
            <p>Local mock environment · approval-safe booking</p>
          </div>
        </div>
        <div className="status-cluster">
          <span className={`badge state-${status.state.toLowerCase()}`}>
            {status.state}
          </span>
          {status.outcome && <span className="badge outcome">{status.outcome}</span>}
          {session && <span className="muted model-name">{session.model}</span>}
        </div>
        <div className="top-actions">
          <button className="ghost" onClick={() => setMemoryOpen(true)}>
            Memories
          </button>
          <button className="ghost" onClick={simulateCancel} disabled={busy}>
            Simulate cancellation
          </button>
        </div>
      </header>

      {error && (
        <div className="error-banner" role="alert">
          <strong>Error:</strong> {error}
        </div>
      )}

      <main className="workspace">
        <ChatPane
          messages={messages}
          pending={pending}
          busy={busy}
          onSend={send}
          onDecide={decide}
        />
        <TracePane events={trace} runId={status.runId} />
      </main>

      <MemoryPanel
        open={memoryOpen}
        memories={memories}
        onClose={() => setMemoryOpen(false)}
        onForget={forget}
      />
    </div>
  );
}
