import type { PendingApproval } from "../types";

function providerName(pending: PendingApproval): string {
  const name = pending.arguments?.provider_name;
  if (typeof name === "string" && name.length > 0) return name;
  return pending.action.provider_id ?? "provider";
}

function title(action: string): string {
  if (action === "book") return "Confirm booking";
  if (action === "cancel") return "Confirm cancellation";
  if (action === "reschedule") return "Confirm reschedule";
  return "Confirm action";
}

interface Props {
  pending: PendingApproval;
  busy: boolean;
  onDecide: (decision: "yes" | "no") => void;
}

export default function ApprovalCard({ pending, busy, onDecide }: Props) {
  const action = pending.action;
  return (
    <div className="approval-card">
      <div className="approval-head">
        <span className="approval-mark">!</span>
        <div>
          <strong>{title(action.action)}</strong>
          <p>{providerName(pending)}</p>
        </div>
      </div>

      <dl className="approval-details">
        {action.slot && (
          <>
            <dt>Slot</dt>
            <dd>{action.slot.replace("T", " · ")}</dd>
          </>
        )}
        {typeof action.price === "number" && (
          <>
            <dt>Price</dt>
            <dd>₹{action.price.toLocaleString("en-IN")}</dd>
          </>
        )}
        {action.booking_id && (
          <>
            <dt>Booking</dt>
            <dd className="mono">{action.booking_id}</dd>
          </>
        )}
        <dt>Ref</dt>
        <dd className="mono">{action.fingerprint}</dd>
      </dl>

      <p className="approval-prompt">{pending.prompt}</p>

      <div className="approval-actions">
        <button className="danger" disabled={busy} onClick={() => onDecide("no")}>
          Cancel
        </button>
        <button className="primary" disabled={busy} onClick={() => onDecide("yes")}>
          Confirm
        </button>
      </div>
    </div>
  );
}
