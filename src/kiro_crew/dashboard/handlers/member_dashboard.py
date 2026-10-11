"""HTTP for one crewmate's dynamic dashboard: the instance, the registry, share, snapshots.

``GET /api/members/{slug}/dashboard`` is the one route the Dashboard tab's frame reads,
and its body is the shape CONTRACT-v3 fixes between the two:
``{instance_version, template: {id, version}, html, manifest, state}``.

**``?member=`` is required**, exactly as the briefing and rules reads require it, and
for the reason those give: slugification is lossy, so two crew names can reach one slug.
A dashboard instance is ONE directory per slug, so for a colliding slug the instance
belongs to neither crewmate. The exact name must derive this
slug, exist in config, and be the only name that derives it.

**The read is owner-gated.** The fields this body carries are work-ledger and crew-log
data, and ``work_ledger_board`` answers a non-owner ``owner_only`` for the same values,
so arriving as rendered html does not make them a wider audience's.

**App tokens are denied outright.** An app token scoped to ``/api/members`` reaches
this by PREFIX, and a crewmate's dashboard is inside exactly what that isolation
withholds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Final, Mapping

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import member_dashboard_fields
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members import (
    _deny_app_caller,
    _member_names_for_slug,
    _member_thread_slot,
)
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.members import MemberSlugError
from kiro_crew.platform.context import redact_via_context

logger = logging.getLogger(__name__)

__all__ = ["register_member_dashboard_routes"]


def _bad(code: str, message: str, status: int = 400) -> web.Response:
    """One refusal shape for the whole module: a code a client branches on, and a sentence.

    Both halves, always. A client cannot branch on prose, and a person cannot act on a
    code -- a surface given only one of the two either hard-codes English or shows the
    user ``instance_refused``.
    """
    return web.json_response({"error": message, "code": code}, status=status)


def _name_unsafe(name: str) -> bool:
    """``True`` when *name* is a string the egress redactor would rewrite.

    Asked of an IDENTIFIER rather than of prose, which is why it answers a boolean and
    not a safe copy. Every other string on this surface is scrubbed by replacing it --
    ``_page_safe`` for a value, ``_template_text_safe`` for markup -- because nothing
    downstream joins on its bytes. A slug is joined on: the body names the package the
    tab mounts, the frame names the package a patch belongs to, and the refetch names
    the package it wants. Rewriting it would make those three disagree, so the only
    answers available are "serve it raw" and "do not serve it".

    ``redact_via_context`` for the reason ``_page_safe`` uses it: it is the canonical
    egress shim, so a host with a loaded companion applies that companion's patterns
    too, and the question here is exactly "would this leave the machine rewritten".
    """
    return bool(name) and redact_via_context(name) != name


async def _resolve(request: web.Request) -> tuple[str, str] | web.Response:
    """``(slug, member)`` for this request, or the refusal to return instead.

    The four questions the sibling member routes ask, in their order: is the slug a
    slug, is the member name one that can reach a model, does that name derive THIS
    slug and exist, and is it the only name that does.
    """
    denied = await _deny_app_caller(request, "members.dashboard")
    if denied is not None:
        return denied
    slug = request.match_info.get("slug", "")
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return _bad("invalid_member_slug", "invalid member slug")
    member = request.query.get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        return _bad("missing_member", "member query parameter required")
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # The ROSTER's own spelling, before either check. The roster is keyed by the
    # display name somebody typed, so `Atlas` is the key and a tab opened on `atlas`
    # is the same crewmate asking for its own page -- answered, before this, with
    # "no crew member for this slug" while its data sat under the canonical key.
    # Resolved ONCE and carried, because the slug check and the roster check have to
    # agree about which name they are asking about.
    member = members_mod.canonical_member_key(member, cfg)
    try:
        if members_mod.member_slug(member, cfg) != slug:
            return _bad("member_slug_mismatch", "member does not match slug")
    except MemberSlugError:
        return _bad("member_slug_mismatch", "member does not match slug")
    if member not in cfg.agents:
        return _bad("member_not_found", "no crew member for this slug", status=404)
    if _member_names_for_slug(cfg, slug) != [member]:
        return _bad(
            "dashboard_slug_ambiguous",
            "multiple crews share this slug; their dashboards would be ambiguous",
            status=409,
        )
    return slug, member


def _dashboard_slot(member: str, slug: str) -> str:
    """The DM slot this crewmate's dashboard reads its fold values from.

    A V2 member's DM log lives on ``member_slot_key(slug, store)``, so the bare
    ``member_slot_key(slug)`` names a different, empty slot: the dashboard then reads no
    values and renders every field unresolved while the crewmate's thread is right there.
    :func:`_member_thread_slot` is the same derivation ``api_member_thread`` uses to
    CREATE the thread, so it names the slot the session actually runs under.

    Falls back to the V1 key when the store resolution refuses (``UnknownMemoryStore``) --
    an unknown member, or a V2 record that is missing or degraded. A degraded store record
    must read as "no values yet" rather than as a dashboard that cannot be served at all.
    """
    try:
        cfg = KiroCrewConfig.load()
        slot, _store = _member_thread_slot(cfg, member, slug)
    except Exception:
        logger.debug("dashboard: falling back to the V1 slot for %r", slug, exc_info=True)
        return members_mod.member_slot_key(slug)
    return slot


def _write_session(slug: str, member: str) -> str:
    """The session a dashboard change's history entry belongs to: the crewmate's DM log.

    Derived, not looked up, and empty when there is none. The instance record is a file
    and is already committed by the time this is used, so a crewmate whose DM thread has
    never run gets a working dashboard with no history row rather than a refused change.

    Resolved by the slot the member's DM thread runs under, then by the newest session
    unit on it: a slot owns one session id at a time, and the newest is the live one.

    "Newest" is taken from the DURABLE succession chain the store wrote, not from the
    header clock. A unit's ``createdAt`` is stamped once and never rewritten, so a clock
    that steps backward between two units of one slot -- an NTP correction, a VM resume
    -- lists the retired unit last, and this would then attribute every later change to
    a session that is already over.
    """
    try:
        # Imported here, not at module scope, for the reason every other store call in
        # this handler is: the crew log is an optional subsystem and the boot path must
        # not load it.
        from kiro_crew.crew_log.projection import units_in_succession

        slot = _dashboard_slot(member, slug)
        units = units_in_succession(slot)
        return units[-1] if units else ""
    except Exception:
        logger.debug("dashboard: no DM session for %r", member, exc_info=True)
        return ""


async def _owner_only(request: web.Request, operation: str) -> web.Response | None:
    return await require_owner_dashboard_request(request, operation)


async def _run(fn: Callable[[], Any]) -> Any:
    """Run a store call off the event loop. Every call here is file IO."""
    return await asyncio.to_thread(fn)


def _refusal(exc: Exception) -> web.Response:
    """Turn a store refusal into an answer. Separate codes, because they differ for a client.

    ``*_refused`` is the caller's input and a retry of the same request will be refused
    again; ``*_failed`` is this gateway's state and a retry may work. Collapsing them
    would make a client either retry forever or give up on a transient fault.
    """
    if isinstance(exc, instance.InstanceRefused):
        return _bad("dashboard_refused", str(exc), status=409)
    if isinstance(exc, catalog.UnknownTemplate):
        return _bad("template_not_found", str(exc), status=404)
    logger.warning("dashboard store call failed", exc_info=exc)
    return _bad("dashboard_failed", "the dashboard store could not serve this", status=500)


# --------------------------------------------------------------------------
# the instance
# --------------------------------------------------------------------------


async def api_member_dashboard(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/dashboard?member=<name> — the crewmate's own dashboard.

    The body is CONTRACT-v3's fixed shape. ``state`` is one of ``empty``, ``live``,
    ``stale`` and ``error``, DERIVED at read time rather than stored: a stored flag
    would be a claim about the registry made when the instance was last written, and a
    template shipping a new version makes every copy of it stale without touching one
    instance file.

    A crewmate that never adopted a template answers 200 with ``state: "empty"``, never
    404: having no dashboard yet is the ordinary first state of every crewmate, and the
    frame's empty state IS that answer. A 404 here would make "nothing adopted" and "no
    such member" one reading for the tab.

    For that case the body also carries the DEFAULT template, rendered. An empty frame
    answers none of the questions a person opened the tab with, so what they see is the
    default page; ``state`` stays ``empty`` and ``instance_version`` stays 0, because
    nothing was adopted and nothing was written -- the snapshot route still refuses on
    that state, and the frame draws whatever ``rendered_html`` carries. ``template``
    names the default rather than staying blank, so a reader of this body can tell which
    page the html belongs to.

    OWNER-ONLY. The fields this body carries are work-ledger and crew-log data, and
    ``work_ledger_board`` answers a non-owner ``owner_only`` for the same values, so
    arriving as rendered html does not make them a wider audience's.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    # OWNER-ONLY, like every other route that serves this crewmate's fold values.
    # The page's fields ARE work-ledger and crew-log data -- task titles, summaries,
    # PR links -- and `work_ledger_board` answers the same caller `owner_only` for
    # exactly those. Serving them here because they arrive as rendered html rather
    # than as JSON would make the gate a property of the response format.
    #
    # Refused rather than rendered with an empty read: an empty read has no resolved
    # fold, which IS the stale condition, so the page would tell a non-owner its
    # numbers are older than the record when the truth is that they were withheld.
    owner_denied = await _owner_only(request, "members.dashboard")
    if owner_denied is not None:
        return owner_denied
    slug, _member = resolved
    # The reader's UI language, as the browser resolved it. Checked against the
    # shipped catalogs in `dashboard_frame.page_locale`; anything else is English.
    locale = request.query.get("locale", "")
    package_slug = request.query.get("dashboard", "")
    if not package_slug:
        # RESOLVED HERE, which is what makes the package page reachable at all: no
        # caller knows a package's slug, so without this the route, the push and the
        # refetch were unreachable code. Named by the crewmate's own binding, off the
        # event loop because it lists the artifact library.
        #
        # STOPGAP: the package line's own `resolve_bound_slug` answers this as part
        # of its API; when that ships, this call becomes that one.
        package_slug = await _run(lambda: member_dashboard_fields.bound_package_slug(_member))
    if _name_unsafe(package_slug):
        # A SLUG IS A NAME TOO, and it travels further than the others: the body carries
        # it twice, every pushed frame carries it, and the refetch answers with it. An
        # artifact slug is `[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?`, which forbids `_` and
        # upper case and so cannot be a `ghp_` or an AWS key -- but it CAN be a Slack
        # bot token (`xoxb-<digits>-<chars>`) or a GitLab PAT (`glpat-<chars>`), and
        # both are shapes the redactor rewrites.
        #
        # HERE, where the slug ENTERS, rather than at each of the three places it
        # leaves: this is the one point both sources pass through, and refusing before
        # `_serve_package` means no package is read and no page is armed -- which also
        # settles the refetch, since it answers from an armed page or 409s.
        #
        # The two sources get different answers because they are different requests.
        if request.query.get("dashboard", ""):
            # THE CALLER NAMED IT, so the caller is told. A code rather than a
            # sentence about secrets, and the slug is not echoed back.
            logger.warning("dashboard: %r named a package slug that would be redacted", slug)
            return _bad(
                "dashboard_slug_refused",
                "that dashboard package cannot be served: its slug would be rewritten "
                "on its way to a page, and the page and its pushes join on that slug",
                status=409,
            )
        # NOBODY ASKED FOR IT: this slug came from the crewmate's own binding. Treated
        # as an unresolved binding, which is the template page every crewmate has on
        # this branch -- so a credential pasted into an artifact's NAME costs its owner
        # the package page and never the tab. Logged, so the rename is findable.
        logger.warning(
            "dashboard: %r's bound package has a slug that would be redacted; serving "
            "the template page instead",
            slug,
        )
        package_slug = ""
    if package_slug:
        # A PAGE ITS OWNER WROTE, served from the same route and behind the same owner
        # check as a template instance. The body keeps the shape the tab already reads
        # -- `rendered_html` is what it mounts -- so a package page needs no second
        # route and no second frame contract, which are the two places this surface
        # cannot afford to get the gate wrong twice.
        return await _serve_package(request, slug, _member, package_slug, locale)
    if request.query.get("preview") == "1":
        # The STAGED page, which no version records. Served from the same route and
        # behind the same owner check: a staged page carries this crewmate's fold
        # values exactly as the live one does, so a route of its own would be a second
        # place to get that gate right. `instance.preview_url` builds this link.
        staged = await _run(lambda: _preview_record(slug))
        if staged is None:
            return web.json_response(
                {"error": "nothing is staged to preview", "code": "no_preview"}, status=404
            )
        body = _safe_body(staged.wire())
        # The CURRENT version, not a new one, because staging wrote none. A reader that
        # saw this number move would believe the page had been installed.
        body["preview"] = True
        rendered = await _run(lambda: _render(slug, _member, staged, locale))
        if rendered is not None:
            body["rendered_html"] = rendered
        return web.json_response(body)
    try:
        record = await _run(lambda: instance.read(slug))
    except Exception as exc:
        return _refusal(exc)
    renderable = True
    if record.state == instance.STATE_EMPTY:
        fallback = await _run(lambda: instance.default_instance(slug))
        if fallback is not None:
            record = fallback
        else:
            # NOTHING TO COMPOSE, so nothing is attempted. An empty record carries
            # `manifest={}`, which `parse_manifest` refuses, so `_render` would log a
            # warning with a traceback for a crewmate that has simply adopted
            # nothing -- on every poll and every projection frame, which is the
            # ordinary state of a build whose registry ships no default. The frame's
            # own empty state IS the answer here, and it needs no html.
            renderable = False
    body = _safe_body(record.wire())
    # STALE belongs here with LIVE and EMPTY: `_state_of` calls a stale copy complete
    # and still renderable -- what it cannot do is be compared against or refreshed
    # from its source, which is what the frame's stale band says. A copy served
    # without composing carries no data island, no bootstrap, no band and no ready
    # beacon, so it shows no values at all. Only ERROR is excluded, because that is
    # the one state in which the copy does not parse.
    if renderable and record.state in (
        instance.STATE_LIVE,
        instance.STATE_EMPTY,
        instance.STATE_STALE,
    ):
        rendered = await _run(lambda: _render(slug, _member, record, locale))
        if rendered is not None:
            body["rendered_html"] = rendered
    return web.json_response(body)


#: Any key whose VALUE is a worker's session key, dropped from a value on its way to a
#: page. Named for the whole repository's rule rather than for one fold: the conductor
#: ledger's is that no reader but the conductor sees a session key, and
#: ``work_ledger_board._MASKED_ITEM_FIELDS`` masks exactly this on the Crew page.
_MASKED_VALUE_KEYS = frozenset({"worker_session_key"})

#: Event kinds whose ``text`` IS a session key, so masking the item field alone leaves
#: the key on the page inside the event log. The line is kept for its ``kind`` and
#: ``ts``, which a timeline needs and which the key is not required to express.
_KEY_BEARING_EVENT_KINDS = frozenset({"bind"})


def _page_safe(value: Any) -> Any:
    """*value* made safe to put on a page: session keys removed, strings redacted.

    ONE traversal doing both, because both are the same question asked of the same
    bytes -- what must not reach a browser -- and two passes are two places for the
    rule to drift.

    Every string here is AGENT-AUTHORED and nothing between the write and this read
    inspects it. A fold value is whatever a conductor put in the crew log: a
    `session_ledger_record(goal=...)` carrying a pasted private key is rendered by any
    template that binds that fold. Dropping the one field known to be a secret says
    nothing about prose that happens to contain one.

    A mapping's KEYS are agent-authored on the same terms as its values -- an artifact
    name is a free string -- so keys are redacted too, and a key that redacts onto one
    already present is suffixed rather than dropped, so two distinct rows do not
    collapse into one.

    `redact_via_context` rather than a named pair of redactors: it is the canonical
    egress shim, so a host with a loaded companion applies that companion's patterns
    too, and it is fail-closed on a composition error. `work_ledger_board._redact_deep`
    applies the same rule to the same data for the Crew page; this is that rule at the
    dashboard's own chokepoint.

    RECURSIVE and shape-agnostic on purpose. This page is served by
    ``GET /api/members/{slug}/dashboard``, which has no owner check, and a template
    declares its own fold paths -- so which fold reaches a page, and how deep the key
    sits in it, is a decision the TEMPLATE makes. A mask written against one fold's
    item shape covers the template that exists today and not the one adopted tomorrow,
    which is how the same key reached a page twice already: once on a task row and
    once on a board id.

    Applied to the resolved values rather than inside a fold, because the fold is also
    read by the conductor itself, which is the one reader allowed to see the key.
    """
    if isinstance(value, str):
        return redact_via_context(value)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        kind = value.get("kind")
        for key, item in value.items():
            if key in _MASKED_VALUE_KEYS:
                continue
            if key == "text" and isinstance(kind, str) and kind in _KEY_BEARING_EVENT_KINDS:
                out[key] = ""
                continue
            safe_key = redact_via_context(key) if isinstance(key, str) else key
            if safe_key in out:
                suffix = 2
                while f"{safe_key} ({suffix})" in out:
                    suffix += 1
                safe_key = f"{safe_key} ({suffix})"
            out[safe_key] = _page_safe(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_page_safe(item) for item in value]
    return value


def _safe_body(body: dict[str, Any]) -> dict[str, Any]:
    """One wire body with its page halves scrubbed, in place.

    ``html`` and ``manifest`` are the two keys carrying the stored template, and the
    body carries both RAW beside the composed page. Scrubbing only the composed one
    would leave the same credential one key away in the same response.
    """
    if isinstance(body.get("html"), str):
        body["html"] = _template_text_safe(body["html"])
    if "manifest" in body:
        body["manifest"] = _manifest_text_safe(body["manifest"])
    return body


def _template_text_safe(text: str) -> str:
    """One page's own text, scrubbed the way a fold value is.

    The SAME redactors ``_page_safe`` applies to a resolved value, applied to the
    markup and prose around it. Only a template that shipped with the product can
    become a page -- which is a claim about the repository, not about the bytes on
    THIS disk. A template directory somebody hand-edited, or an instance record
    written under an older gateway, reaches this handler as a page the parity check
    passed, and a parity check says nothing about a credential pasted into a heading.

    This is the one step that produces both the raw body and the composed document,
    so it is the only place the page and the values in it can be covered by one pass.
    ``_page_safe`` covers the resolved values and never the page around them.
    """
    if not text:
        return text
    return redact_via_context(text)


def _manifest_text_safe(manifest: Any) -> Any:
    """A manifest's own strings, scrubbed. Keys are left alone.

    Values only, and recursively, because a manifest's KEYS are field names the
    loader has already matched against ``^[a-z][a-z0-9_]{0,63}$`` -- a grammar no
    credential survives -- while ``title`` and ``description`` are free prose.
    """
    if isinstance(manifest, str):
        return redact_via_context(manifest)
    if isinstance(manifest, Mapping):
        return {key: _manifest_text_safe(value) for key, value in manifest.items()}
    if isinstance(manifest, (list, tuple)):
        return [_manifest_text_safe(item) for item in manifest]
    return manifest


def _preview_record(slug: str) -> Any | None:
    """The staged page as a readable record, or ``None`` when nothing is staged.

    Built as an ``Instance`` so that ONE renderer serves both: a preview a person looks
    at must be filled with the same fold values, masked by the same pass and composed
    by the same builder as the page it would replace, or they have been shown something
    other than what applying it would give them.

    ``instance_version`` is the CURRENT record's, because staging wrote no version.
    ``state`` is ``live``, which is what a staged page is in the only sense the frame
    reads that field for: it parses, its bindings match its manifest -- both checked at
    staging -- so it renders. The body carries ``preview: true`` beside it so no reader
    has to infer which of the two it is holding.
    """
    preview = instance.staged_preview(slug)
    if preview is None:
        return None
    try:
        current_version = instance.read(slug).instance_version
    except Exception:
        logger.warning("dashboard: could not read %r's version to preview against", slug)
        current_version = 0
    return instance.Instance(
        slug=slug,
        instance_version=current_version,
        template_id=preview.template_id,
        template_version=preview.template_version,
        html=preview.html,
        manifest=preview.manifest,
        state=instance.STATE_LIVE,
        state_reason="staged for preview; no version written",
        updated_ms=preview.staged_ms,
    )


def _trusted_page(slug: str, record: Any) -> str | None:
    """The markup this gateway will EXECUTE for *record*, or ``None`` to refuse.

    Read from the catalog by the record's ``template_id``, so the bytes come from a
    directory in this repository and never from the crewmate's own writable
    ``instance.json``. Every other half of the render still comes from the record --
    the manifest decides which fields are read and which are the agent's -- and that
    is fine: a manifest can only name fields and fold paths, so the worst a tampered
    one does is draw a cell nothing fills. The HTML is the half that RUNS.

    ``None`` for all four refusals, because the tab draws one unavailable state and
    there is nothing a reader can do differently between them:

    * the record names no template, which is the empty state and not an attack;
    * the manifest's ``source`` is not renderable, kept as the cheap check so a
      record that never went through adopt is turned away before a registry scan;
    * the catalog does not serve that id -- a template removed from the product, or
      an id only ever written by something editing the file;
    * the catalog's own copy will not load, which is a broken checkout rather than
      this crewmate's problem.

    The refusal is LOGGED with the slug and the id, because an id the catalog does
    not serve is the signature of a tampered record and an operator is the only one
    who can go and look.
    """
    from kiro_crew.dashboard_templates import catalog
    from kiro_crew.dashboard_templates import instance as instance_store

    template_id = str(getattr(record, "template_id", "") or "")
    if not template_id:
        logger.warning("dashboard: %r's record names no template, so there is none to run", slug)
        return None
    source = str((record.manifest or {}).get("source") or "")
    if source not in instance_store.RENDERABLE_SOURCES:
        logger.warning("dashboard: refusing to render %r's %r template", slug, source)
        return None
    try:
        entry = catalog.load_one(template_id)
    except catalog.UnknownTemplate:
        logger.warning(
            "dashboard: %r's record names template %r, which this gateway does not "
            "serve; refusing to run the page stored beside it",
            slug,
            template_id,
        )
        return None
    except Exception:
        logger.warning("dashboard: template %r could not be loaded", template_id, exc_info=True)
        return None
    # The CATALOG's own claim about itself, which is the one that counts: the scan
    # already checked this directory's `source` against where the directory actually
    # is, so an entry reaching here is one the repository vouches for.
    if entry.manifest.source not in instance_store.RENDERABLE_SOURCES:
        logger.warning(
            "dashboard: catalog template %r declares %r and will not be run",
            template_id,
            entry.manifest.source,
        )
        return None
    return entry.html


def read_fields(slug: str, member: str, manifest: Any) -> Any:
    """One read of *manifest*'s field values for this crewmate, as the page gets them.

    The page's own read, shared with ``dashboard_fields`` so the values an agent is
    shown are the values the reader sees. File IO; raises on a failed read.
    """
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard_feed import DashboardFeed

    slot = _dashboard_slot(member, slug)
    feed = DashboardFeed(slot, _write_session(slug, member))
    try:
        feed.subscribe(manifest)
        agentic: Any = {}
        if any(spec.agentic for spec in manifest.fields.values()):
            agentic = projection.read_slot_projection(slot, "agentic").value
        return feed.read(manifest, agentic if isinstance(agentic, dict) else {})
    finally:
        feed.unsubscribe()


def _render(slug: str, member: str, record: Any, locale: str = "") -> str | None:
    """The live page with its values filled in: the frame's half of contract v3 part 5.

    Fold values come through :class:`~kiro_crew.dashboard_feed.DashboardFeed`, which
    subscribes with a baseline -- the bus hands the CURRENT fold value through the
    projection read path, so nothing refolds a log here. Agentic values come from the
    slot's ``agentic`` fold. The result is :func:`dashboard_frame.compose_body`, the one
    builder that also composes a refill, so the page's ``window.kirocrew`` is the same
    shape on first paint and after.

    DEMO SCOPE: the feed is opened and closed per request. The contract wants one
    long-lived feed per open dashboard pushing refills over the WS exporter; until
    that lands, a re-read (focus, the tab's own refetch) is how the page moves.
    ``None`` on any failure, so the raw page still renders under the frame's own
    stale band rather than the tab erroring.
    """
    try:
        from kiro_crew import dashboard_frame
        from kiro_crew.dashboard_templates.manifest import parse_manifest

        manifest = parse_manifest(dict(record.manifest))
        # THE EXECUTABLE PAGE COMES FROM THE CATALOG, NEVER FROM THE RECORD.
        #
        # `record.html` and `record.manifest` both come out of
        # ``members/<slug>/dashboard/instance.json``, which is WRITABLE. Gating the
        # render on `manifest.source` asked that file to vouch for itself: anything
        # that could append a `<script>` to the stored page could leave the
        # `builtin` label in place beside it, and this step is the one that hands
        # the crewmate's task titles and summaries to whatever runs. The label is
        # not evidence, so it cannot be the gate.
        #
        # What IS trusted is the repository. The record's ``template_id`` names a
        # directory in it, so the id is read from the record and the BYTES are read
        # from the catalog. A stored page may stay on disk -- a rollback reads it,
        # and it is what the crewmate copied -- but nothing executes it.
        page = _trusted_page(slug, record)
        if page is None:
            return None
        read = read_fields(slug, member, manifest)
        payload = dashboard_frame.read_payload(
            # MASKED AND REDACTED on the way out, at the one step that hands fold
            # values to a page's own script. A snapshot is taken from what the page
            # holds, so it inherits this rather than needing its own pass.
            {name: _page_safe(value) for name, value in read.fields.items()},
            agentic=[name for name, spec in manifest.fields.items() if spec.agentic],
            seq=read.seq,
            stale=read.stale,
            missing=read.missing,
            # Masked like the values beside them: a stamp is not a secret, but this is
            # the one chokepoint and a field added here later would otherwise skip it.
            written_at={name: str(_page_safe(at)) for name, at in read.written_at.items()},
            locale=locale,
        )
        # The catalog's page, redacted like the values that go into it. The scrub is
        # kept because this is the one step producing both the raw body and the
        # composed document, and it guards a hand-edited checkout rather than a page
        # an attacker chose: the bytes here came out of the repository.
        return dashboard_frame.compose_body(_template_text_safe(page), payload)
    except Exception:
        logger.warning("dashboard: could not fill %r's page", slug, exc_info=True)
        return None


def register_member_dashboard_routes(app: web.Application) -> None:
    """Register the dynamic dashboard's read route and the gap refetch beside it.

    The refetch path is a strict suffix of the read path, so aiohttp's own resolver
    separates them by METHOD and there is no ordering question. ``server.py`` duplicates the
    path through its deferred binder rather than calling this function, because calling
    it would import this module at boot and the boot-path rule forbids that for an
    optional subsystem; ``test_member_dashboard_routes`` pins the two spellings against
    each other.
    """
    app.router.add_get("/api/members/{slug}/dashboard", api_member_dashboard)
    app.router.add_post("/api/members/{slug}/dashboard/refetch", api_member_dashboard_refetch)


#: One armed page per (crewmate, package), on the app so a reload replaces rather than
#: stacks. Keyed by BOTH because one crewmate can be shown through two packages at once
#: and each needs its own subscriptions and its own version counter.
_LIVE_PAGES = "member_dashboard_live_pages"

#: How many pages may be armed at once. Each one holds a bus subscription per fold its
#: model reads, and nothing disarms a page when its tab closes -- the browser cannot
#: say so -- so an operator who opens every crewmate's dashboard over a long-running
#: gateway leaves every one of those subscriptions behind for its life.
#:
#: The least recently armed page is disarmed when the cap is passed. That costs its tab
#: its pushes until its next read arms it again, and its next read is a refetch of the
#: whole value set -- so the failure mode is a page that stops moving, never one that
#: presents a stale value as the current one.
_MAX_LIVE_PAGES: Final[int] = 32


def _live_pages(request: web.Request) -> dict[tuple[str, str], Any]:
    pages: dict[tuple[str, str], Any] | None = request.app.get(_LIVE_PAGES)
    if pages is None:
        pages = {}
        request.app[_LIVE_PAGES] = pages
    return pages


def _push_frame(state: Any, slug: str, package_slug: str, frame: dict[str, Any]) -> None:
    """Send one field frame to the owner's sockets. Called on the FOLD WORKER thread.

    ``broadcast_ws_owners`` -- the same hub and the same owner-socket set every other
    fold-driven frame goes out on, so the owner gate, the backpressure and the
    reconnect are not re-implemented for a second channel.
    """
    try:
        # THE PACKAGE IS NAMED on every frame. The socket is per connection, so a
        # frame reaches every tab the owner has open -- and one crewmate can be shown
        # through two different packages at once (two tabs, or a template page beside a
        # package page). Without the id each tab applies the other's patches, and its
        # refetch answers with the other package's read.
        state.broadcast_ws_owners(
            member_dashboard_fields.FIELD_FRAME,
            {"slug": slug, "dashboard": package_slug, "read": frame},
        )
    except Exception:  # pragma: no cover - a failed send must not cost the folder
        logger.debug("dashboard: a field frame for %s could not be broadcast", slug)


async def _serve_package(
    request: web.Request, slug: str, member: str, package_slug: str, locale: str
) -> web.Response:
    """Serve the ``kind="dashboard"`` artifact *package_slug* as this crewmate's page.

    THE BINDING IS CHECKED, not looked up. The tab names the package it is opening and
    this asks whether that package is THIS crewmate's -- which is the question that
    matters, and the one a scan of the artifact library would answer less directly. A
    package bound elsewhere is refused rather than rendered, because a page carries the
    binding's own fold values and rendering it here would show one crewmate's work
    under another's name.

    Arming the live page is part of serving it: the first paint and every later patch
    come from one :class:`~kiro_crew.dashboard.handlers.member_dashboard_fields.LivePage`,
    so the version the body carries is the one the first patch is checked against.
    """
    from kiro_crew.artifact_store.dashboard_package import (
        DASHBOARD_KIND,
        binding_refusal,
        parse_package,
        subscribed_fields,
    )
    from kiro_crew.artifacts import ArtifactError, get_default_store

    try:
        loaded = await _run(lambda: get_default_store().get(package_slug))
    except ArtifactError:
        return _bad("dashboard_not_found", "no dashboard package at that slug", status=404)
    except OSError:
        logger.warning("dashboard: package %r could not be read", package_slug)
        return _bad("dashboard_failed", "the dashboard package could not be read", status=500)
    # THE RECORD'S OWN KIND, checked before anything is parsed. Content alone is not
    # enough: a LINKED artifact's bytes come from a file on disk, so its content can be
    # changed without passing the artifact write gate at all -- and parsing whatever is
    # there would arm a page nobody authorized. Only a stored `kind="dashboard"`
    # artifact that owns its own bytes reaches the parse below.
    if str(getattr(loaded, "kind", "")) != DASHBOARD_KIND:
        return _bad(
            "dashboard_not_a_package",
            "that artifact is not a dashboard package",
            status=409,
        )
    if str(getattr(loaded, "source_path", "") or "") and not bool(
        getattr(loaded, "source_copy_only", False)
    ):
        # A live pointer: the artifact serves a file's current bytes, which nothing in
        # the artifact API gates. Refused rather than rendered, because the page RUNS.
        return _bad(
            "dashboard_live_source",
            "a dashboard package cannot be served from a linked file: its bytes would "
            "bypass the write gate that decides whose page this is",
            status=409,
        )
    # THE RESIDUAL, named where it bites. These are the record's OWN bytes, and the
    # store rotates a random `content_token` per write rather than hashing them, so a
    # write made straight into the stored file -- around the artifact API and so around
    # the owner gate -- is not detectable at this line. Nor can a check here close it: a
    # writer reaching that file is the same OS user as this process, so any digest or key
    # this handler compared against sits in a file that writer can rewrite too, and it
    # could edit this module instead. What bounds the damage is the frame rather than the
    # gate -- the page runs with no network on an opaque origin and is sent only the
    # fields the model declares, so tampered markup can mislead the owner reading it and
    # reach nothing beyond that tab. The gate's own job is the write that arrives over
    # the API, which is the one every agent and app actually has.
    try:
        package = await _run(lambda: parse_package(loaded.content or ""))
    except Exception as exc:
        return _bad("dashboard_refused", f"the dashboard package does not load: {exc}", status=409)
    # OFF THE LOOP: `binding_refusal` resolves the caller's slug through the roster,
    # which loads the config from disk.
    refused = await _run(lambda: binding_refusal(package.get("bound_to"), crew=member))
    if refused:
        return _bad("dashboard_not_bound", refused, status=403)
    state = request.app["state"]
    version = int(getattr(loaded, "version", 0) or 0)
    pages = _live_pages(request)
    # KEYED BY (crewmate, package), because one crewmate can have two packages open and
    # each needs its own subscriptions and its own version counter.
    key = (slug, package_slug)
    previous = pages.pop(key, None)
    if previous is not None:
        # DISARMED FIRST. A reload or a layout change would otherwise leave the old
        # page's subscriptions on the bus, pushing frames computed under a layout
        # nothing is rendering.
        await _run(previous.disarm)
    # BOTH RESOLVERS ARE FILE IO and neither may run here: `_dashboard_slot` loads the
    # config and `_write_session` walks the slot's unit succession, reading each unit's
    # opening entry. On the event loop that blocks every other session's chat and the
    # heartbeat for as long as the walk takes.
    slot, session_unit = await _run(
        lambda: (_dashboard_slot(member, slug), _write_session(slug, member))
    )
    page = member_dashboard_fields.LivePage(
        slug,
        slot,
        package,
        lambda frame: _push_frame(state, slug, package_slug, frame),
        session_unit=session_unit,
        layout_version=version,
        # The reader's own UI language, so a patch describes the same read the first
        # paint did. Without it every frame carries the default and a non-English
        # reader is handed an English page on every push.
        locale=locale,
        # THE SAME masking and redaction pass the template path applies, handed in
        # rather than re-implemented: this is the one place a package's values are
        # produced, so a value that did not cross it cannot reach a browser.
        redact=_page_safe,
    )
    read = await _run(page.arm)
    # RE-CHECKED AFTER ARMING, because arming awaits: two GETs for one crewmate (two
    # tabs, or both tabs refetching after a reconnect) each pop before either assigns,
    # so without this the first armed page is replaced without ever being disarmed --
    # and it stays subscribed to the bus for the life of the gateway, pushing frames
    # from its own version counter that the open page can only read as gaps.
    raced = pages.get(key)
    if raced is not None and raced is not page:
        await _run(raced.disarm)
    pages[key] = page
    # THE CAP, applied after the assignment so the page this request serves is never
    # the one evicted. A dict iterates in insertion order and every serve pops its key
    # before re-inserting it, so the first key is the least recently ARMED one -- the
    # order the eviction wants, with no second structure to keep in step with it.
    while len(pages) > _MAX_LIVE_PAGES:
        oldest = next(iter(pages))
        if oldest == key:
            break
        stale = pages.pop(oldest)
        await _run(stale.disarm)
    # SCRUBBED BEFORE COMPOSING, with the same pass the `html` field below gets. The
    # composed document is the half that RUNS, so scrubbing only the raw field left a
    # credential pasted into the page's own markup in the response one key away.
    safe_page = dict(package)
    safe_view = dict(package.get("view") or {})
    safe_view["html"] = _template_text_safe(safe_view.get("html") or "")
    safe_page["view"] = safe_view
    try:
        html = member_dashboard_fields.render_package_page(safe_page, read)
    except Exception as exc:
        await _run(page.disarm)
        pages.pop(key, None)
        return _bad("dashboard_refused", f"the page cannot be framed: {exc}", status=409)
    return web.json_response(
        {
            "instance_version": version,
            "template": {"id": package_slug, "version": version},
            # NAMED for the tab: it carries this back on a refetch and drops any frame
            # that belongs to a different package.
            "dashboard": package_slug,
            # The package's own page, kept beside the composed one exactly as the
            # template path keeps its stored copy, and scrubbed by the same pass.
            "html": safe_view["html"],
            "manifest": _manifest_text_safe(package.get("model") or {}),
            "state": instance.STATE_LIVE,
            "rendered_html": html,
            # THE ALLOWLIST, named in the body so a reader of one response can see
            # exactly which fields this page can ever be sent.
            "fields": sorted(subscribed_fields(package)),
            "locale": read.get("locale", locale) if isinstance(read, dict) else locale,
        }
    )


async def api_member_dashboard_refetch(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/refetch -- the whole read, after a version gap.

    The host's half of the frame's own gap rule: a page that cannot place a patch in
    order asks for a full read rather than applying one out of order, and this answers
    it. The tree-keyed folds are re-read here too, which is the other half of why a
    refetch exists -- nothing can push a tree, so this is the only moment its value
    moves.

    Owner-gated like the read it belongs to. A page nothing has armed answers 409: the
    remedy is to re-read the dashboard, which arms one.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner_denied = await _owner_only(request, "members.dashboard")
    if owner_denied is not None:
        return owner_denied
    slug, _member = resolved
    # The PACKAGE is named, for the same reason a frame names it: a crewmate can have
    # two open, and answering the wrong one's read would replace a page's values with
    # another page's wholesale.
    package_slug = request.query.get("dashboard", "")
    if not package_slug:
        return _bad("missing_dashboard", "name the dashboard package to refetch")
    page = _live_pages(request).get((slug, package_slug))
    if page is None:
        return _bad("no_live_page", "no dashboard page is armed for this package", status=409)
    return web.json_response({"dashboard": package_slug, "read": await _run(page.refetch)})
