"""The limits of what a caller reads back: how many messages and characters a
read returns, the pending-approval title bound, the summary bounds and the
roles a read leaves out. The read verbs themselves are still in
``session_control.py``; they move here with the rest of the split.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_persistence import _TRANSIENT_ROLES as _PERSISTENCE_TRANSIENT_ROLES

# Reads are cheap but not free — each one walks the target's in-memory window.
MAX_READ_MESSAGES = 100


DEFAULT_READ_MESSAGES = 20


# Per-message content cap for reads, so pulling a transcript tail cannot return
# a multi-megabyte tool payload verbatim.
MAX_READ_CONTENT_CHARS = 4000


# Cap on the tool title a read or status row names as waiting for approval.
MAX_PENDING_APPROVAL_TOOL_CHARS = 200


# Bounds on a ``read_summary`` response. The stored summary caps intent and note
# COUNTS at 50 but neither per-intent list lengths nor string lengths, so the
# response is cut here, once, and says how much it left out. Intents arrive
# most-recently-touched first, so the intent cut drops the oldest.
MAX_SUMMARY_INTENTS = 10


MAX_SUMMARY_ITEMS = 5


MAX_SUMMARY_NOTES = 10


MAX_SUMMARY_CHARS = 500


# Roles a read must not count, taken from the persistence layer's own list rather
# than restated here: those are exactly the rows rehydration DROPS, so any cursor
# that counted them would name a different position after a restart than before
# it. ``chunk`` runs are deleted when a segment flushes and ``done`` markers never
# persist at all, so counting either inflates ``total``, the list shrinks back
# under it, and the next ``since=next_since`` read skips the finished reply for good.
TRANSIENT_ROLES = _PERSISTENCE_TRANSIENT_ROLES
