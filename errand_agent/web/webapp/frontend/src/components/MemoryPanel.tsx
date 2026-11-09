import { useMemo, useState } from "react";
import type { MemoryFact } from "../types";

interface Props {
  open: boolean;
  memories: MemoryFact[];
  onClose: () => void;
  onForget: (key: string) => void;
}

export default function MemoryPanel({
  open,
  memories,
  onClose,
  onForget,
}: Props) {
  const [filter, setFilter] = useState("");
  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return memories;
    return memories.filter((memory) =>
      `${memory.key} ${memory.value}`.toLowerCase().includes(needle),
    );
  }, [filter, memories]);

  const explicit = memories.filter((m) => m.source === "explicit").length;
  const inferred = memories.filter((m) => m.source === "inferred").length;

  return (
    <>
      <div
        className={`scrim ${open ? "show" : ""}`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <aside className={`drawer ${open ? "open" : ""}`} aria-hidden={!open}>
        <div className="drawer-head">
          <div>
            <h2>Stored memories</h2>
            <p>
              {memories.length} facts · {explicit} explicit · {inferred} inferred
            </p>
          </div>
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>

        <input
          className="filter"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter by key or value…"
          aria-label="Filter memories"
        />

        <div className="memory-list">
          {filtered.map((memory) => (
            <div
              key={memory.id}
              className={`memory ${memory.forgotten ? "forgotten" : ""}`}
            >
              <div className="memory-main">
                <span className="mono memory-key">{memory.key}</span>
                <span className="memory-value">{memory.value}</span>
              </div>
              <div className="memory-tags">
                <span className={`tag source-${memory.source}`}>{memory.source}</span>
                {memory.source === "inferred" && (
                  <span className="tag">
                    {memory.confirmed ? "confirmed" : "unconfirmed"}
                  </span>
                )}
                {memory.forgotten ? (
                  <span className="tag forgotten-tag">forgotten</span>
                ) : (
                  <button className="link" onClick={() => onForget(memory.key)}>
                    Forget
                  </button>
                )}
              </div>
            </div>
          ))}
          {filtered.length === 0 && <div className="empty">No matching facts.</div>}
        </div>
      </aside>
    </>
  );
}
