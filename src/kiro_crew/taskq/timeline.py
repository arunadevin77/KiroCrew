"""One readable timeline per task, merged from the streams that already record it.

The history of one task is spread over three stores that share only an id: the
``task_events`` rows of the durable queue, the ``subagent/*`` entries of the
spawning session's crew log, and the run events of the workflow a
workflow-agent row belongs to. :func:`render_timeline` reads each through its
owner's existing read surface, merges the three into one chronological list and
renders it as markdown.

It is a read-only view. It stores nothing, writes nothing back into any of the
three streams, and adds no reader of its own to the crew log or the workflow
store: the crew-log side goes through ``unit_holding_child``, the ``subagents``
fold and ``read_page``; the workflow side goes through the run registry.

Scrubbing belongs to the caller and is not optional. ``scrub`` is a required
argument with no default, so there is no path that renders unscrubbed text. It
is applied to every event's data before anything is flattened, to the text of
every value and label before it is cut (serialization brings back mapping keys
and leaves rendered through ``str``, which the data pass leaves as they are,
and a credential is matched whole, never as the prefix a cut leaves), and to
every rendered line, which also covers the labels a hostile or damaged store
could put in a ``kind`` or a ``type``. The dashboard route passes the same
helper it scrubs its other task payloads with.

A stream that cannot be read is reported, not dropped: each stream carries a
:class:`StreamReport` whose status is ``ok``, ``absent``, ``truncated`` or
``unavailable`` with a closed reason, and the markdown ends with the same
table. An empty timeline therefore never reads as "nothing happened" when a
stream was simply unreadable.

Public surface: :func:`render_timeline`, :func:`events_from_registry`,
:func:`read_child_entries`, :class:`Timeline`, :class:`StreamReport`,
:class:`Sources`, :class:`CrewRead`, the stream, status and reason names, and
the bounds below.

Specification: ``docs/system-specs/modules/taskq.md`` (Task timeline).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.taskq import model as taskq_model
from kiro_crew.taskq.adapters.runner import OWNER_WORKFLOW, owner_of

if TYPE_CHECKING:
    from kiro_crew.taskq.store import TaskStore

logger = logging.getLogger(__name__)

__all__ = [
    "CREW_ENTRIES_MAX",
    "CREW_SCAN_MAX",
    "REASON_KEPT_LIMIT",
    "REASON_NOT_APPLICABLE",
    "REASON_NO_RECORD",
    "REASON_NO_SOURCE",
    "REASON_READ_FAILED",
    "REASON_SCAN_LIMIT",
    "REASON_TAIL_LIMIT",
    "STATUS_ABSENT",
    "STATUS_OK",
    "STATUS_TRUNCATED",
    "STATUS_UNAVAILABLE",
    "STREAMS",
    "STREAM_CREW_LOG",
    "STREAM_TASK_EVENTS",
    "STREAM_WORKFLOW",
    "VALUE_MAX_CHARS",
    "WORKFLOW_EVENTS_MAX",
    "CrewRead",
    "Sources",
    "StreamReport",
    "Timeline",
    "events_from_registry",
    "read_child_entries",
    "render_timeline",
]

STREAM_TASK_EVENTS: Final = "task_events"
STREAM_CREW_LOG: Final = "crew_log"
STREAM_WORKFLOW: Final = "workflow"
#: The streams in tie-break order: two entries stamped in the same instant list
#: the queue's own record first, then the session log's, then the workflow's.
STREAMS: Final = (STREAM_TASK_EVENTS, STREAM_CREW_LOG, STREAM_WORKFLOW)
_RANK: Final = {name: index for index, name in enumerate(STREAMS)}

STATUS_OK: Final = "ok"
#: Nothing is recorded in this stream for the task, or the stream does not
#: apply to this kind of row.
STATUS_ABSENT: Final = "absent"
#: Entries exist that this view does not show; the reason names the bound.
STATUS_TRUNCATED: Final = "truncated"
#: The stream could not be read; what it holds is unknown, not empty.
STATUS_UNAVAILABLE: Final = "unavailable"

REASON_NOT_APPLICABLE: Final = "not_applicable"
REASON_NO_RECORD: Final = "no_record"
REASON_READ_FAILED: Final = "read_failed"
REASON_NO_SOURCE: Final = "no_source"
REASON_TAIL_LIMIT: Final = "tail_limit"
REASON_SCAN_LIMIT: Final = "scan_limit"
REASON_KEPT_LIMIT: Final = "kept_limit"

#: Entries of the spawning session's log read from the child's dispatch onward.
#: The read is one ``read_page`` call, so memory is bounded by this and the walk
#: to the end of the unit is paid once.
CREW_SCAN_MAX: Final = 5000
#: Crew-log entries kept for one child (the newest when more exist).
CREW_ENTRIES_MAX: Final = 200
#: Workflow run events kept for one run (the newest when more exist).
WORKFLOW_EVENTS_MAX: Final = 200
#: Characters kept of one rendered value, so one event cannot dominate a page.
VALUE_MAX_CHARS: Final = 300

_ELLIPSIS: Final = "..."


@dataclass(frozen=True)
class CrewRead:
    """What a crew-log reader found for one child.

    ``found`` is False when no unit of the session records the child's
    dispatch (an absence, not a failure). ``complete`` is False when the scan
    stopped at :data:`CREW_SCAN_MAX` before the end of the unit.
    """

    rows: tuple[Mapping[str, Any], ...] = ()
    found: bool = False
    complete: bool = True


@dataclass(frozen=True)
class Sources:
    """The reads this view needs from outside the task store.

    ``slot_of`` maps a row's session key to the slot key the crew log files its
    session under. ``workflow_events`` answers one run id with that run's event
    dicts, or ``None`` for a run it does not know; ``None`` here means the
    gateway has no workflow service. ``crew_reader`` replaces
    :func:`read_child_entries` (tests inject faults through it).
    """

    slot_of: Callable[[str], str]
    workflow_events: Callable[[str], Sequence[Mapping[str, Any]] | None] | None = None
    crew_reader: Callable[[str, str], CrewRead] | None = None


@dataclass(frozen=True)
class StreamReport:
    """How one stream fared: its status, entry count and, when not plain ok, why."""

    stream: str
    status: str
    count: int = 0
    reason: str = ""
    #: Entries read but not placed because their timestamp was unreadable.
    dropped: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "status": self.status,
            "count": self.count,
            "reason": self.reason,
            "dropped": self.dropped,
        }


@dataclass(frozen=True)
class Timeline:
    """The rendered view of one task."""

    task_id: str
    markdown: str
    entries: int
    streams: tuple[StreamReport, ...] = field(default_factory=tuple)

    def public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "markdown": self.markdown,
            "entries": self.entries,
            "streams": [report.public() for report in self.streams],
        }


@dataclass(frozen=True)
class _Item:
    ts: float
    stream: str
    order: int
    label: str
    detail: str


# ── value rendering ──────────────────────────────────────────────────────────


def _one_line(text: str, scrub: Callable[[Any], Any]) -> str:
    """*text* scrubbed whole, then put on one line and cut at :data:`VALUE_MAX_CHARS`.

    The scrubber runs before the cut, because cutting first could leave the
    prefix of a credential that no pattern recognises. Every value and label
    is cut here, so none is cut unscrubbed.
    """
    flat = " ".join(str(scrub(text)).split())
    if len(flat) > VALUE_MAX_CHARS:
        return flat[: VALUE_MAX_CHARS - len(_ELLIPSIS)] + _ELLIPSIS
    return flat


def _render_mapping_keys(value: Any, scrub: Callable[[Any], Any]) -> Any:
    """Return a rendering-only copy with mapping keys scrubbed before JSON encoding."""
    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                candidate = scrub(key)
                rendered_key: Any = candidate if isinstance(candidate, str) else str(candidate)
            else:
                rendered = str(key)
                candidate = scrub(rendered)
                rendered_key = (
                    candidate if isinstance(candidate, str) and candidate != rendered else key
                )
            cleaned[rendered_key] = _render_mapping_keys(item, scrub)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_render_mapping_keys(item, scrub) for item in value]
    return value


def _value(value: Any, scrub: Callable[[Any], Any]) -> str:
    """One value on one scrubbed line.

    A value that is not a string is serialized, and the serialized text is what
    gets scrubbed: it carries mapping keys and leaves rendered through ``str``,
    which the scrub of the data in :func:`_mapping` leaves as they are. Mapping
    keys are scrubbed on a rendering-only copy before serialization, and both
    the JSON encoding and its ``repr`` fallback read that copy, so multiline
    credentials remain matchable after either one escapes them.
    """
    if isinstance(value, str):
        return _one_line(value, scrub)
    rendered = _render_mapping_keys(value, scrub)
    try:
        text = json.dumps(
            rendered,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        text = repr(rendered)
    return _one_line(text, scrub)


def _describe(
    data: Mapping[str, Any],
    scrub: Callable[[Any], Any],
    *,
    skip: frozenset[str] = frozenset(),
) -> str:
    """``from -> to`` when the data carries a transition, then ``key=value`` pairs."""
    head = ""
    omit = set(skip)
    if "from" in data and "to" in data:
        head = f"{_value(data['from'], scrub)} -> {_value(data['to'], scrub)}"
        omit |= {"from", "to"}
    pairs = [
        f"{_text(scrub, key)}={_value(val, scrub)}"
        for key, val in sorted(data.items(), key=lambda kv: str(kv[0]))
        if key not in omit
    ]
    return " ".join(part for part in (head, " ".join(pairs)) if part)


def _stamp(ts: float) -> str:
    """UTC instant to the millisecond, the same spelling for every stream."""
    moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _seconds(value: Any, *, per_second: float = 1.0) -> float | None:
    """A stored time as epoch seconds :func:`_stamp` can spell, else ``None``.

    A damaged or hostile store can hold any number that parses: ``inf``,
    ``nan``, or an instant outside the years the clock can spell. Each would
    raise when its line is stamped and fail the whole view, so the caller
    counts it as an unreadable time instead.
    """
    try:
        seconds = float(value) / per_second
        if not math.isfinite(seconds):
            return None
        _stamp(seconds)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return seconds


def _order(value: Any) -> int:
    """A stream's own order for one entry; 0 when it does not read as an integer."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _mapping(scrub: Callable[[Any], Any], data: Any) -> dict[str, Any]:
    """*data* scrubbed leaf by leaf, as a plain dict ({} when it is not a mapping)."""
    if not isinstance(data, Mapping):
        return {}
    cleaned = scrub(dict(data))
    return dict(cleaned) if isinstance(cleaned, Mapping) else {}


def _text(scrub: Callable[[Any], Any], value: Any) -> str:
    return _one_line(str(value if value is not None else ""), scrub)


# ── the three streams ────────────────────────────────────────────────────────


def _task_event_items(
    events: Sequence[Any], scrub: Callable[[Any], Any]
) -> tuple[list[_Item], int]:
    items: list[_Item] = []
    dropped = 0
    for event in events:
        ts = _seconds(getattr(event, "ts", 0.0) or 0.0)
        if ts is None:
            dropped += 1
            continue
        data = _mapping(scrub, getattr(event, "data", None))
        items.append(
            _Item(
                ts=ts,
                stream=STREAM_TASK_EVENTS,
                order=_order(getattr(event, "seq", 0)),
                label=_text(scrub, getattr(event, "kind", "")),
                detail=_describe(data, scrub),
            )
        )
    return items, dropped


def read_child_entries(slot_key: str, agent_id: str) -> CrewRead:
    """The ``subagent/*`` entries naming *agent_id* in the session that dispatched it.

    Blocking; the caller runs it off the loop. The unit is found by the crew
    log's own search over the slot's units, the child's dispatch position comes
    from the ``subagents`` fold, and the entries come from ``read_page`` over a
    window that starts there, because nothing about a child precedes its
    dispatch. A store that cannot answer raises (``UnitSearchFailed`` or the
    fold's own error); that is the caller's ``unavailable``, not an absence.
    """
    from kiro_crew.crew_log.projection import fold_session
    from kiro_crew.crew_log.read import read_page
    from kiro_crew.crew_log.resolve import unit_holding_child

    unit = unit_holding_child(slot_key, agent_id)
    if not unit:
        return CrewRead()
    folded = fold_session(unit, names=("subagents",)).projection("subagents").value
    row = (folded.get("by_id") or {}).get(agent_id)
    seq = row.get("seq_spawned") if isinstance(row, Mapping) else None
    start = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else 1
    page = read_page(unit, start, start + CREW_SCAN_MAX - 1)
    rows = tuple(
        entry
        for entry in page.get("entries") or ()
        if isinstance(entry, Mapping)
        and str(entry.get("type") or "").startswith("subagent/")
        and isinstance(entry.get("data"), Mapping)
        and entry["data"].get("agent_id") == agent_id
    )
    return CrewRead(rows=rows, found=True, complete=page.get("next_from") is None)


def _crew_stream(
    record: Any, sources: Sources, scrub: Callable[[Any], Any]
) -> tuple[list[_Item], StreamReport]:
    if str(getattr(record, "kind", "")) != taskq_model.KIND_SUBAGENT:
        return [], StreamReport(STREAM_CREW_LOG, STATUS_ABSENT, reason=REASON_NOT_APPLICABLE)
    task_id = str(record.id)
    reader = sources.crew_reader or read_child_entries
    try:
        found = reader(sources.slot_of(str(getattr(record, "session_key", "") or "")), task_id)
    except Exception:
        logger.warning("task timeline: crew log read failed for %s", task_id, exc_info=True)
        return [], StreamReport(STREAM_CREW_LOG, STATUS_UNAVAILABLE, reason=REASON_READ_FAILED)
    if not found.found:
        return [], StreamReport(STREAM_CREW_LOG, STATUS_ABSENT, reason=REASON_NO_RECORD)
    items: list[_Item] = []
    dropped = 0
    for row in found.rows:
        millis = row.get("time")
        is_number = isinstance(millis, (int, float)) and not isinstance(millis, bool)
        ts = _seconds(millis, per_second=1000.0) if is_number else None
        if ts is None:
            dropped += 1
            continue
        data = _mapping(scrub, row.get("data"))
        items.append(
            _Item(
                ts=ts,
                stream=STREAM_CREW_LOG,
                order=_order(row.get("seq")),
                label=_text(scrub, row.get("type")),
                detail=_describe(data, scrub, skip=frozenset({"agent_id"})),
            )
        )
    status, reason = STATUS_OK if items else STATUS_ABSENT, "" if items else REASON_NO_RECORD
    if len(items) > CREW_ENTRIES_MAX:
        items = items[-CREW_ENTRIES_MAX:]
        status, reason = STATUS_TRUNCATED, REASON_KEPT_LIMIT
    elif not found.complete:
        status, reason = STATUS_TRUNCATED, REASON_SCAN_LIMIT
    return items, StreamReport(STREAM_CREW_LOG, status, len(items), reason, dropped)


def _iso_seconds(text: Any) -> float | None:
    """An ISO 8601 stamp as epoch seconds; a naive one is read as UTC."""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _workflow_stream(
    task_id: str, sources: Sources, scrub: Callable[[Any], Any]
) -> tuple[list[_Item], StreamReport]:
    owner = owner_of(task_id)
    if owner is None or owner[0] != OWNER_WORKFLOW:
        return [], StreamReport(STREAM_WORKFLOW, STATUS_ABSENT, reason=REASON_NOT_APPLICABLE)
    if sources.workflow_events is None:
        return [], StreamReport(STREAM_WORKFLOW, STATUS_UNAVAILABLE, reason=REASON_NO_SOURCE)
    try:
        events = sources.workflow_events(owner[1])
    except Exception:
        logger.warning("task timeline: workflow read failed for %s", task_id, exc_info=True)
        return [], StreamReport(STREAM_WORKFLOW, STATUS_UNAVAILABLE, reason=REASON_READ_FAILED)
    if events is None:
        return [], StreamReport(STREAM_WORKFLOW, STATUS_ABSENT, reason=REASON_NO_RECORD)
    items: list[_Item] = []
    dropped = 0
    for event in events:
        parsed = _iso_seconds(event.get("ts"))
        ts = None if parsed is None else _seconds(parsed)
        if ts is None:
            dropped += 1
            continue
        data = _mapping(scrub, event.get("data"))
        items.append(
            _Item(
                ts=ts,
                stream=STREAM_WORKFLOW,
                order=_order(event.get("seq")),
                label=_text(scrub, event.get("type")),
                detail=_describe(data, scrub),
            )
        )
    status, reason = STATUS_OK if items else STATUS_ABSENT, "" if items else REASON_NO_RECORD
    if len(items) > WORKFLOW_EVENTS_MAX:
        items = items[-WORKFLOW_EVENTS_MAX:]
        status, reason = STATUS_TRUNCATED, REASON_KEPT_LIMIT
    return items, StreamReport(STREAM_WORKFLOW, status, len(items), reason, dropped)


def events_from_registry(registry: Any) -> Callable[[str], list[dict[str, Any]] | None]:
    """A ``Sources.workflow_events`` reader over a workflow run registry.

    The registry's ``get`` answers a run handle (live or restored from the
    persisted run JSON) whose ``events`` are the same objects the run detail
    serves. An event that cannot be turned into its JSON form is skipped.
    """

    def _events(run_id: str) -> list[dict[str, Any]] | None:
        handle = registry.get(run_id)
        if handle is None:
            return None
        out: list[dict[str, Any]] = []
        for event in list(getattr(handle, "events", ()) or ()):
            try:
                out.append(event.to_json())
            except Exception:
                logger.debug("task timeline: skipped an unreadable workflow event", exc_info=True)
        return out

    return _events


# ── the view ─────────────────────────────────────────────────────────────────


def _merge(*streams: list[_Item]) -> list[_Item]:
    """Every item in time order; ties fall to the stream order, then the stream's own order."""
    merged = [item for stream in streams for item in stream]
    merged.sort(key=lambda item: (item.ts, _RANK[item.stream], item.order))
    return merged


def render_timeline(
    task_id: str,
    *,
    store: TaskStore,
    scrub: Callable[[Any], Any],
    events_limit: int,
    sources: Sources,
) -> Timeline | None:
    """The merged timeline of *task_id*, or ``None`` when the store has no such row.

    Blocking (SQLite and file reads); the caller runs it off the loop.
    *events_limit* is the tail of ``task_events`` read, the same bound the task
    detail route serves. A stream that fails is reported on the result and does
    not fail the others; the store read itself is the one error that
    propagates, because without the row there is no timeline to qualify.
    """
    record = store.get(task_id)
    if record is None:
        return None
    # One row past the bound tells a tail that stopped at the bound from a task
    # whose history is exactly the bound long: only the former is truncated.
    probe = list(store.events(task_id, limit=events_limit + 1))
    truncated = len(probe) > events_limit
    events = probe[-events_limit:] if truncated else probe
    queue_items, queue_dropped = _task_event_items(events, scrub)
    queue_status, queue_reason = (
        (STATUS_OK, "") if queue_items else (STATUS_ABSENT, REASON_NO_RECORD)
    )
    if truncated:
        queue_status, queue_reason = STATUS_TRUNCATED, REASON_TAIL_LIMIT
    reports = [
        StreamReport(
            STREAM_TASK_EVENTS, queue_status, len(queue_items), queue_reason, queue_dropped
        )
    ]
    crew_items, crew_report = _crew_stream(record, sources, scrub)
    workflow_items, workflow_report = _workflow_stream(task_id, sources, scrub)
    reports += [crew_report, workflow_report]
    merged = _merge(queue_items, crew_items, workflow_items)

    lines = [
        f"# Timeline: {task_id}",
        "",
        f"- kind: {getattr(record, 'kind', '')}, state: {getattr(record, 'state', '')}, "
        f"attempts: {getattr(record, 'attempts', 0)}, generation: {getattr(record, 'generation', 0)}",
        "",
    ]
    if merged:
        for item in merged:
            tail = f": {item.detail}" if item.detail else ""
            lines.append(f"- {_stamp(item.ts)} [{item.stream}] {item.label}{tail}")
    else:
        lines.append("No entries are recorded for this task.")
    lines += ["", "## Streams", ""]
    for report in reports:
        note = f" ({report.reason})" if report.reason else ""
        lost = f", {report.dropped} without a readable time" if report.dropped else ""
        lines.append(f"- {report.stream}: {report.status}{note}, {report.count} entries{lost}")
    markdown = "\n".join(str(scrub(line)) for line in lines) + "\n"
    return Timeline(task_id=task_id, markdown=markdown, entries=len(merged), streams=tuple(reports))
