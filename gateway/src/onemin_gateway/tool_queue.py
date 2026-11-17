"""In-memory, per-session FIFO of tool calls parsed from a single 1min.ai
response but not yet dispensed to OpenCode.

Each queued call is bound to the id of the tool call that produced it: the
queue is only popped when the next request acknowledges that id in a
`role: "tool"` message. This prevents a cancelled tool or a new user turn in
the same session from receiving a stale call. Entries expire after five
minutes, and are dropped on process restart (no persistence).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .protocol import ParsedToolCall

DEFAULT_QUEUE_TTL_SECONDS = 300.0


@dataclass
class QueuedCall:
    call: ParsedToolCall
    previous_call_id: str
    expires_at: float


class ToolCallQueue:
    def __init__(self, ttl_seconds: float = DEFAULT_QUEUE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._queues: dict[str, list[QueuedCall]] = {}

    def extend(
        self, session_id: str, calls: list[ParsedToolCall], previous_call_id: str
    ) -> None:
        if not calls:
            return
        now = time.monotonic()
        self._queues.setdefault(session_id, []).extend(
            QueuedCall(call, previous_call_id, now + self._ttl) for call in calls
        )

    def pop_next(self, session_id: str, acknowledged_ids: set[str] | None = None) -> ParsedToolCall | None:
        queue = self._queues.get(session_id)
        if not queue:
            return None
        now = time.monotonic()
        queue[:] = [entry for entry in queue if entry.expires_at > now]
        if not queue:
            del self._queues[session_id]
            return None
        first = queue[0]
        # Oldest entry first: only dispense when the preceding tool call has
        # actually been executed and its result echoed back by the client.
        if acknowledged_ids is None or first.previous_call_id not in acknowledged_ids:
            return None
        queue.pop(0)
        if not queue:
            del self._queues[session_id]
        return first.call

    def rebase_next(self, session_id: str, new_previous_call_id: str) -> None:
        """Bind the next queued call to the id of the call just dispensed.

        All calls from one backend response are initially attributed to the
        first call's id. Each time one is handed to the client, the following
        entry must instead wait for *that* call's result.
        """
        queue = self._queues.get(session_id)
        if queue:
            queue[0].previous_call_id = new_previous_call_id

    def pending_count(self, session_id: str) -> int:
        queue = self._queues.get(session_id)
        if not queue:
            return 0
        now = time.monotonic()
        live = [entry for entry in queue if entry.expires_at > now]
        if len(live) != len(queue):
            if live:
                self._queues[session_id] = live
            else:
                self._queues.pop(session_id, None)
        return len(live)

    def discard_session(self, session_id: str) -> None:
        self._queues.pop(session_id, None)
