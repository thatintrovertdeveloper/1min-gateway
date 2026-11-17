import time

from onemin_gateway.protocol import ParsedToolCall
from onemin_gateway.tool_queue import ToolCallQueue


def make_call(name: str) -> ParsedToolCall:
    return ParsedToolCall(name=name, arguments={})


class TestToolCallQueue:
    def test_pop_next_on_unknown_session_returns_none(self):
        queue = ToolCallQueue()
        assert queue.pop_next("session-1") is None

    def test_pending_count_on_unknown_session_is_zero(self):
        queue = ToolCallQueue()
        assert queue.pending_count("session-1") == 0

    def test_extend_with_empty_list_is_a_noop(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [], "call-prev")
        assert queue.pending_count("session-1") == 0
        assert queue.pop_next("session-1", {"call-prev"}) is None

    def test_fifo_order(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [make_call("a"), make_call("b"), make_call("c")], "call-0")
        popped = queue.pop_next("session-1", {"call-0"})
        assert popped.name == "a"
        # app.py rebinds the next entry to the id just handed to the client.
        queue.rebase_next("session-1", "call-a")
        popped = queue.pop_next("session-1", {"call-a"})
        assert popped.name == "b"
        queue.rebase_next("session-1", "call-b")
        assert queue.pop_next("session-1", {"call-b"}).name == "c"
        assert queue.pop_next("session-1", {"call-c"}) is None

    def test_pending_count_reflects_remaining_items(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [make_call("a"), make_call("b")], "call-prev")
        assert queue.pending_count("session-1") == 2
        queue.pop_next("session-1", {"call-prev"})
        assert queue.pending_count("session-1") == 1

    def test_popping_last_item_removes_the_session_entry(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [make_call("a")], "call-prev")
        queue.pop_next("session-1", {"call-prev"})
        assert "session-1" not in queue._queues

    def test_unacknowledged_queue_does_not_dispense(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [make_call("a")], "call-prev")
        assert queue.pop_next("session-1", set()) is None
        assert queue.pop_next("session-1", {"unrelated-call"}) is None
        assert queue.pending_count("session-1") == 1

    def test_entries_expire(self):
        queue = ToolCallQueue(ttl_seconds=0.01)
        queue.extend("session-1", [make_call("a")], "call-prev")
        time.sleep(0.02)
        assert queue.pop_next("session-1", {"call-prev"}) is None
        assert queue.pending_count("session-1") == 0

    def test_discard_session(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [make_call("a")], "call-prev")
        queue.discard_session("session-1")
        assert queue.pending_count("session-1") == 0

    def test_queues_are_isolated_per_session(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [make_call("a")], "call-prev")
        queue.extend("session-2", [make_call("b")], "call-prev")
        assert queue.pop_next("session-1", {"call-prev"}).name == "a"
        assert queue.pop_next("session-2", {"call-prev"}).name == "b"

    def test_extend_appends_to_existing_queue(self):
        queue = ToolCallQueue()
        queue.extend("session-1", [make_call("a")], "call-prev")
        queue.extend("session-1", [make_call("b")], "call-a")
        assert queue.pending_count("session-1") == 2
        assert queue.pop_next("session-1", {"call-prev"}).name == "a"
