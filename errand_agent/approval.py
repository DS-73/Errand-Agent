"""Application-level approval gate.

Approval is *not* inferred from model output or tool arguments.  The gate
creates a one-time grant bound to a fingerprint of the exact material action
(provider, slot, price, booking/hold id).  The executor refuses consequential
tools unless a matching, unexpired grant exists, and consumes it on the
attempt so repeated confirmations cannot double-book.
"""
from __future__ import annotations

import time
from typing import Any

from .models import ApprovalGrant, MaterialAction, PendingApproval
from .nlu import has_change, is_affirmative, is_negation


class ApprovalGate:
    def __init__(self, ttl_s: float = 300.0):
        self.ttl_s = ttl_s
        self._pending: dict[str, PendingApproval] = {}
        self._grants: dict[tuple[str, str], ApprovalGrant] = {}
        self.last_event: str | None = None

    # ------------------------------------------------------------------ pending
    def request(self, user_id: str, action: MaterialAction, tool: str,
                arguments: dict[str, Any], prompt: str,
                now: float | None = None) -> PendingApproval:
        now = now if now is not None else time.time()
        pending = PendingApproval(user_id=user_id, action=action, tool=tool,
                                  arguments=arguments, prompt=prompt, created_at=now)
        self._pending[user_id] = pending
        return pending

    def get_pending(self, user_id: str) -> PendingApproval | None:
        return self._pending.get(user_id)

    def clear_pending(self, user_id: str) -> None:
        self._pending.pop(user_id, None)

    def invalidate(self, user_id: str) -> None:
        """A material change (provider/slot/price) invalidates prior approval."""
        self._pending.pop(user_id, None)
        for key in [k for k in self._grants if k[0] == user_id]:
            self._grants.pop(key, None)

    # ------------------------------------------------------------------- grants
    def observe(self, user_id: str, message: str, now: float | None = None) -> ApprovalGrant | None:
        """Interpret the user's reply against the pending action.

        Returns a grant only for an explicit affirmative reply.  A negation or
        a request to change a material detail clears the pending action.
        """
        now = now if now is not None else time.time()
        pending = self._pending.get(user_id)
        if pending is None:
            return None
        if is_negation(message):
            self._pending.pop(user_id, None)
            self.last_event = "denied"
            return None
        if has_change(message) and not is_affirmative(message):
            # "actually make it 3pm" is a correction, not approval.
            self._pending.pop(user_id, None)
            self.last_event = "invalidated_by_change"
            return None
        if not is_affirmative(message):
            self.last_event = "no_decision"
            return None
        grant = ApprovalGrant(
            user_id=user_id,
            fingerprint=pending.fingerprint,
            action=pending.action,
            tool=pending.tool,
            arguments=pending.arguments,
            created_at=now,
            expires_at=now + self.ttl_s,
        )
        self._grants[(user_id, grant.fingerprint)] = grant
        # Keep the pending action visible to the model for this turn so it can
        # retry the exact same tool call; the executor clears it after execution.
        self.last_event = "granted"
        return grant

    def has_grant(self, user_id: str, action: MaterialAction,
                  now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        grant = self._grants.get((user_id, action.fingerprint()))
        if grant is None:
            return False
        if now > grant.expires_at:
            self._grants.pop((user_id, action.fingerprint()), None)
            return False
        return True

    def consume(self, user_id: str, action: MaterialAction) -> bool:
        return self._grants.pop((user_id, action.fingerprint()), None) is not None

    def restore(self, grant: ApprovalGrant, now: float | None = None) -> None:
        """Re-arm a grant for the same exact action after an ambiguous timeout.

        The idempotency key is unchanged, so this cannot create a duplicate;
        it only lets the agent retry the action the user already approved.
        """
        now = now if now is not None else time.time()
        if grant.expires_at > now:
            self._grants[(grant.user_id, grant.fingerprint)] = grant

    def grant_for(self, user_id: str, action: MaterialAction) -> ApprovalGrant | None:
        return self._grants.get((user_id, action.fingerprint()))

    # ------------------------------------------------------------------- prompt
    @staticmethod
    def format_prompt(action: MaterialAction, provider_name: str | None = None) -> str:
        if action.action == "book":
            what = f"book {provider_name or action.provider_id}"
            detail = f"on {action.slot} for ₹{action.price:,}" if action.price is not None else \
                f"on {action.slot}"
        elif action.action == "cancel":
            what = f"cancel booking {action.booking_id}"
            detail = f"({provider_name or action.provider_id}, {action.slot})"
        elif action.action == "reschedule":
            what = f"reschedule booking {action.booking_id}"
            detail = f"to {action.slot}"
        else:
            what = action.action
            detail = ""
        return (f"Please confirm: {what} {detail}. "
                f"Reply 'yes' to confirm or 'no' to cancel. (ref: {action.fingerprint()})")
