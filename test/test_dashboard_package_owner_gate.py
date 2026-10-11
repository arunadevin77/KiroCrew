"""A dashboard package is written by the crewmate it is bound to, and by nobody else.

The split this protects is the one that makes a free-HTML page safe to run at all: the
owner composes the page and a person approves it, workers only `work_report` and the fold
carries their reports to it. A worker that could write the package could write the page --
and the page runs script inside the frame that holds the owner's own fold values.

The refusal is a SENTENCE because the party that reads it is an agent that can act on it:
whose dashboard this is, and what to do instead. A code would be rendered to a worker as
`owner_only` with nothing to do next.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.artifact_store import dashboard_package as dp


@pytest.fixture()
def roster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slugs for two crews, resolved the way the roster resolves them."""
    import kiro_crew.members as members_mod

    slugs = {"Atlas": "atlas", "Nyx": "nyx"}

    def member_slug(name: str, config: Any = None) -> str:
        if name not in slugs:
            raise members_mod.MemberSlugError(name)
        return slugs[name]

    monkeypatch.setattr(members_mod, "member_slug", member_slug)


class TestACrewmateBinding:
    def test_the_owner_may_write_its_own_package(self, roster: None) -> None:
        assert dp.binding_refusal("crewmate:atlas", crew="Atlas") == ""

    def test_a_worker_is_refused_and_told_what_to_do(self, roster: None) -> None:
        said = dp.binding_refusal("crewmate:atlas", crew="Nyx")
        assert "atlas" in said
        assert "Nyx" in said
        assert "work_report" in said, "the refusal does not say what the worker should do"

    def test_a_caller_with_no_crew_is_refused(self, roster: None) -> None:
        """A gate whose default is "no identity, no objection" is not a gate."""
        assert dp.binding_refusal("crewmate:atlas") != ""

    def test_a_caller_whose_name_has_no_slug_is_refused(self, roster: None) -> None:
        """An unslugifiable caller cannot be shown to own anything, which is the same
        answer as a mismatch rather than a crash on the write path."""
        assert dp.binding_refusal("crewmate:atlas", crew="Ghost") != ""

    def test_the_slug_is_compared_not_the_display_name(self, roster: None) -> None:
        """Slugification is lossy, so two configured names can reach one slug. Comparing
        names would let each pass for the other's page; comparing slugs is what the
        binding actually stores."""
        assert dp.binding_refusal("crewmate:atlas", crew="atlas") != ""


class TestASessionBinding:
    def test_the_bound_session_may_write(self) -> None:
        assert dp.binding_refusal("session:chat-7-17910", slot="chat-7-17910") == ""

    def test_another_session_is_refused(self) -> None:
        said = dp.binding_refusal("session:chat-7-17910", slot="chat-9-17911")
        assert "chat-7-17910" in said
        assert "work_report" in said

    def test_a_caller_with_no_session_is_refused(self) -> None:
        assert dp.binding_refusal("session:chat-7-17910") != ""

    def test_a_crew_identity_does_not_satisfy_a_session_binding(self, roster: None) -> None:
        """The two scopes name different things. A crewmate that happened to pass here
        would be writing a page bound to a slot it does not own."""
        assert dp.binding_refusal("session:chat-7-17910", crew="Atlas") != ""


class TestAMalformedBinding:
    def test_a_binding_that_is_not_one_is_refused_with_its_own_reason(self) -> None:
        """Refused as a malformed binding rather than as a wrong owner: an author that
        wrote the grammar wrong needs the grammar, not a sentence about ownership."""
        for bad in ("atlas", "", None, 7, "crewmate:", "session:dashboard:chat-7"):
            said = dp.binding_refusal(bad, crew="Atlas", slot="chat-7-17910")
            assert said != ""
            assert "work_report" not in said, f"{bad!r} was answered as an ownership refusal"


# --------------------------------------------------------------------------- #
# Two ways the gate was skippable. Both were reported by review on
# e1f2ce06cb and both are authorization bypasses, so each gets a test that
# fails without the fix rather than a comment saying it is handled.
# --------------------------------------------------------------------------- #


class _Req:
    """The three things the gate reads off a request, and nothing else.

    ``get`` answers ``None`` for every key: an MCP caller carries no app claim, which
    is what distinguishes it from the app-token requests in
    :class:`TestAnAppTokenCannotWriteAPackage`.
    """

    def __init__(self, secret: bool = True) -> None:
        self.headers = {"X-Internal-Secret": "s"} if secret else {}

    def get(self, key: str, default: Any = None) -> Any:
        return default


@pytest.fixture()
def worker(monkeypatch: pytest.MonkeyPatch, roster: None) -> None:
    """A caller that is the crew ``Nyx`` -- not ``atlas``, whose pages these are."""
    from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

    monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Nyx", "chat-9-17911"))


def _package_text(bound_to: str = "crewmate:atlas", kind: str = '"dashboard"') -> str:
    """A valid package as submitted BYTES, so a test can spell ``kind`` its own way."""
    return (
        "{"
        f'"kind":{kind},'
        f'"bound_to":"{bound_to}",'
        '"model":{"types":{"goal":{"type":"text",'
        '"source":{"fold":"work","path":"conductor.goal"}}}},'
        # A page that BINDS NOTHING, drawing from `subscribe` instead. The binding
        # attribute is itself spelled `data-dashboard-field`, so a page that used one
        # would put the word in the bytes and the old substring check would have caught
        # this payload by accident -- which is not the payload an attacker sends.
        '"view":{"html":"<canvas id=t></canvas>"},'
        '"theme":{"tokens":{}}'
        "}"
    )


class TestTheKindCannotBeHiddenFromTheGate:
    """The gate classified the content by looking for the word ``dashboard`` in the
    submitted bytes. JSON can spell any character as an escape, so the word need never
    appear -- and the store's own ``json.loads`` decodes it to a package regardless.

    Reported independently by two review lanes, which is the signal that it is reachable
    rather than theoretical: the worker needs no special access, only a text editor.
    """

    def test_a_package_whose_kind_is_escaped_is_still_gated(self, worker: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        # No literal "dashboard" anywhere in these bytes.
        text = _package_text(kind='"dash\\u0062oard"')
        assert "dashboard" not in text
        said = mdf.package_write_refusal(_Req(), text)
        assert said, "a worker can write another crewmate's page by escaping one letter"
        assert "atlas" in said

    def test_every_letter_escaped_is_still_gated(self, worker: None) -> None:
        """The fix cannot be a longer list of spellings to look for."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        escaped = "".join(f"\\u{ord(c):04x}" for c in "dashboard")
        assert mdf.package_write_refusal(_Req(), _package_text(kind=f'"{escaped}"'))

    def test_a_prose_document_is_still_not_gated(self, worker: None) -> None:
        """The cheap pre-filter has to stay cheap AND unescapable: a JSON document
        begins with '{' and no escape hides that, so prose never reaches a parse."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        for text in ("# just a note", "dashboard is a word I used in prose", "", None):
            assert mdf.package_write_refusal(_Req(), text) == ""

    def test_json_that_is_not_a_package_is_not_gated(self, worker: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        assert mdf.package_write_refusal(_Req(), '{"kind":"note","body":"hi"}') == ""


class TestTheStoredBindingIsAuthorizedToo:
    """The gate read only the binding being WRITTEN, which is a claim the caller wrote.

    So a worker submitted a package bound to ITSELF, passed, and overwrote the slug
    holding another crewmate's page -- rebinding that crewmate's dashboard to the worker
    in one write. The binding that decides whether a slug may be written is the one the
    slug holds now.
    """

    def test_a_worker_cannot_overwrite_another_crewmates_slug_by_rebinding_it(
        self, worker: None
    ) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(
            _Req(),
            # Bound to the worker itself: authorizing this alone always passes.
            _package_text(bound_to="crewmate:nyx"),
            # What the slug holds now.
            _package_text(bound_to="crewmate:atlas"),
        )
        assert said, "a worker can take over another crewmate's dashboard by rebinding it"
        assert "atlas" in said

    def test_a_worker_cannot_blank_another_crewmates_page_with_prose(self, worker: None) -> None:
        """The submitted content is not a package at all, so classifying only the
        submission answers "not mine to judge" and the store overwrites the page."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(_Req(), "# gone", _package_text("crewmate:atlas"))
        assert said
        assert "atlas" in said

    def test_the_owner_may_still_rewrite_its_own_page(self, worker: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatched = mdf.package_write_refusal(
            _Req(), _package_text("crewmate:nyx"), _package_text("crewmate:nyx")
        )
        assert monkeypatched == ""

    def test_a_browser_caller_is_not_gated_here(self) -> None:
        """No internal secret: the person at the dashboard, already owner-gated by the
        artifact routes. Asserted so the fix above cannot quietly start refusing them."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        assert (
            mdf.package_write_refusal(
                _Req(secret=False), _package_text(), _package_text("crewmate:atlas")
            )
            == ""
        )


class TestAnAppTokenCannotWriteAPackage:
    """An app token carries no internal secret, so the person's exemption must not be
    reachable by one.

    Every condition is ordinary rather than exotic, which is why this is a test and not
    a note: app-token requests carry `request["app"]`, the artifact routes' own
    `_non_owner_refusal` returns early for exactly that claim, and shipped apps already
    declare `/api/artifacts` grants and write through that route. So nothing between the
    token and the store asked whose page it was.
    """

    class _AppReq:
        """An app-token request: an app claim, and no internal secret."""

        def __init__(self, app: str = "code-review-sage") -> None:
            self.headers: dict[str, str] = {}
            self._store = {"app": app}

        def get(self, key: str, default: Any = None) -> Any:
            return self._store.get(key, default)

    def test_an_app_is_refused_even_for_a_package_bound_to_nobody_it_knows(
        self, roster: None
    ) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(self._AppReq(), _package_text("crewmate:atlas"))
        assert said, "an app token can overwrite a crewmate's dashboard package"
        assert "app" in said.lower()

    def test_an_app_cannot_rebind_a_stored_package_to_itself(self, roster: None) -> None:
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        said = mdf.package_write_refusal(
            self._AppReq(),
            _package_text("crewmate:nyx"),
            _package_text("crewmate:atlas"),
        )
        assert said

    def test_an_app_writing_a_prose_document_is_untouched(self, roster: None) -> None:
        """The gate is asked on every artifact write. An app saving a note is its own
        business, and refusing that would break every app that uses the library."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        assert mdf.package_write_refusal(self._AppReq(), "# a note") == ""

    def test_the_person_at_the_dashboard_is_still_exempt(self, roster: None) -> None:
        """No secret AND no app claim: the browser session the artifact routes have
        already owner-gated. The app check above is what makes that a positive
        identification rather than "whoever sent no secret"."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        class _Person:
            headers: dict[str, str] = {}

            def get(self, key: str, default: Any = None) -> Any:
                return default

        assert mdf.package_write_refusal(_Person(), _package_text("crewmate:atlas")) == ""


class TestTheWriteLandsOnTheSNAPSHOTTheGateJudged:
    """The gate reads the stored binding, and the write happens later.

    Two moments, so a rebind arriving between them is the hazard: an authorization made
    against the binding the slug held at the read, committed by a store whose default is
    last-write-wins, puts the caller's page on the crewmate who just took the slug --
    and nothing puts the owner's binding back. An agent holding several PATCHes open
    chooses that timing itself.

    What closes it is carrying the token of the read the gate judged into the write, so
    the store refuses a write based on a snapshot that has moved on.
    """

    def test_the_store_refuses_the_write_when_the_record_moved_under_it(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE RACE ITSELF, against the real store and with no threads: the rebind is
        simply performed between the read and the write, which is the interleaving the
        agent is choosing."""
        from kiro_crew import artifacts as art_mod
        from kiro_crew.artifacts import ArtifactConflictError, ArtifactStore
        from kiro_crew.dashboard.handlers.artifacts import _stored_snapshot_for

        store = ArtifactStore(root=tmp_path / "artifacts")
        made = store.create(name="page", content=_package_text("crewmate:atlas"), kind="dashboard")
        monkeypatch.setattr(art_mod, "_default_store", store)

        # 1. What the gate reads and judges.
        content, token, snapshot = _stored_snapshot_for(made.slug)
        assert snapshot == "found"
        assert content is not None and token, "the gate has no snapshot to authorize"
        assert "atlas" in content

        # 2. The owner rebinds the slug to another crewmate while that write is open.
        store.update(made.slug, content=_package_text("crewmate:nyx"))

        # 3. The write the gate authorized now lands -- and must not.
        with pytest.raises(ArtifactConflictError):
            store.update(made.slug, content=_package_text("crewmate:atlas"), expected_token=token)
        assert "nyx" in (store.get(made.slug).content or ""), "the rebind was overwritten"

    @pytest.mark.asyncio
    async def test_the_update_route_writes_under_the_token_it_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the route is what must carry it. A caller that sends no
        ``expected_token`` -- every agent, since the field is optional -- gets the one
        the gate's own read returned."""
        wrote = await _update_through_the_route(
            monkeypatch, stored=_package_text("crewmate:atlas"), submitted=_package_text()
        )
        assert wrote["expected_token"] == "token-of-the-read", (
            "the route wrote without the token of the snapshot it authorized, so a "
            "rebind landing in between is overwritten"
        )

    @pytest.mark.asyncio
    async def test_a_callers_own_token_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller that sends one is re-basing on a read of its own. A stale token
        fails the store's check anyway, so keeping it cannot widen the write."""
        wrote = await _update_through_the_route(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text(),
            expected_token="token-the-caller-read",
        )
        assert wrote["expected_token"] == "token-the-caller-read"

    @pytest.mark.asyncio
    async def test_an_ordinary_document_keeps_last_write_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asked on EVERY artifact write. Forcing the check on a prose document would
        turn the editor's autosave into a conflict the person never caused."""
        wrote = await _update_through_the_route(
            monkeypatch, stored="# yesterday's note", submitted="# today's note"
        )
        assert wrote["expected_token"] is None


class TestTheDedupPathReadsTheRecordItLandsOn:
    """A save carrying a ``source_path`` lands on whatever artifact already points at
    that path, and that record is found by SCANNING META -- so it carries no content.

    Handing that to the gate is the empty-snapshot hole wearing another hat: with no
    stored binding to check, the gate judges only the binding the caller submitted,
    which the caller wrote. A member who copies another member's source path and
    re-saves it through this route would take their page.
    """

    @pytest.mark.asyncio
    async def test_a_worker_re_saving_another_crewmates_source_path_is_refused(
        self, worker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:nyx"),
        )
        assert status == 403, "a worker took another crewmate's page through the dedup path"
        assert not reached, "the write reached the store anyway"

    @pytest.mark.asyncio
    async def test_the_owner_may_still_re_save_its_own(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate has to read the record, not refuse everything that reaches it."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Atlas", "chat-1-1790"))
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:atlas"),
        )
        assert reached, "the owner was refused its own page"
        assert status == 0

    @pytest.mark.asyncio
    async def test_a_snapshot_that_is_gone_by_the_read_refuses_the_write(
        self, worker: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The source-path lookup found a record and the read by slug found none, which
        is the window between them: the target deleted, and its slug free to be
        recreated as somebody else's dashboard before this write lands.

        There is no snapshot to authorize and no token to carry, so the write is
        refused rather than committed under the caller's own claim.
        """
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored="",
            submitted=_package_text("crewmate:nyx"),
            reader=_raises(_not_found()),
        )
        assert status == 409, "a package write went out with no snapshot behind it"
        assert not reached, "the write reached the store with no token"

    @pytest.mark.asyncio
    async def test_a_tokenless_snapshot_refuses_the_write_too(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record that answers without a token is the same hole wearing a hat: the
        guard would be dropped silently and the write would be last-write-wins."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Atlas", "chat-1-1790"))
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:atlas"),
            reader=lambda slug, **_k: SimpleNamespace(
                slug=slug,
                kind="dashboard",
                content=_package_text("crewmate:atlas"),
                content_token="",
            ),
        )
        assert status == 409
        assert not reached

    @pytest.mark.asyncio
    async def test_an_ordinary_document_is_not_held_to_that(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The requirement belongs to this gate, not to the artifact library: a note
        de-duped onto its own path is saved the way it is on base."""
        status, reached = await _save_onto_a_deduped_record(
            monkeypatch,
            stored="# yesterday",
            submitted="# today",
            reader=lambda slug, **_k: SimpleNamespace(
                slug=slug, kind="html", content="# yesterday", content_token=""
            ),
        )
        assert reached, "an ordinary de-duped save stopped reaching the store"
        assert status == 0

    @pytest.mark.asyncio
    async def test_the_write_goes_out_under_the_token_of_that_read(
        self, roster: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same reason as the update route: the gate judged a snapshot and this write
        lands later, so a rebind in between must meet the store's refusal."""
        from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

        monkeypatch.setattr(mdf, "writing_caller", lambda request: ("Atlas", "chat-1-1790"))
        wrote: dict[str, Any] = {}
        await _save_onto_a_deduped_record(
            monkeypatch,
            stored=_package_text("crewmate:atlas"),
            submitted=_package_text("crewmate:atlas"),
            captured=wrote,
        )
        assert wrote.get("expected_token") == "token-of-the-read"


async def _save_onto_a_deduped_record(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored: str,
    submitted: str,
    captured: dict[str, Any] | None = None,
    reader: Any = None,
) -> tuple[int, bool]:
    """Drive ``api_artifacts_create`` down the ``source_path`` dedup branch.

    The store answers the source-path lookup with a META-ONLY record, the way the real
    one does, and serves content only when asked by slug -- which is the distinction
    under test.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    seen: dict[str, Any] = captured if captured is not None else {}
    reached: list[bool] = []

    def _update(_slug: str, **kwargs: Any) -> Any:
        reached.append(True)
        seen.update(kwargs)
        raise _Wrote

    store = SimpleNamespace(
        find_by_source_path=lambda path: SimpleNamespace(
            slug="page", kind="dashboard", content=None, source_path=path
        ),
        get=reader
        or (
            lambda slug, **_k: SimpleNamespace(
                slug=slug, kind="dashboard", content=stored, content_token="token-of-the-read"
            )
        ),
        update=_update,
        list=lambda kind=None, **_k: [SimpleNamespace(slug="page", kind="dashboard")],
    )
    monkeypatch.setattr(handler, "get_default_store", lambda: store)
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)
    monkeypatch.setattr(handler, "_validate_inbound_webapp_metadata", lambda _b: "")

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)

    # THE PROMOTE VERDICT, which is what decides whether a path is dedup-able at all
    # and canonicalizes it. Stubbed to "a linked path, these bytes" so this test is
    # about the gate rather than about path classification, which owns its own tests:
    # a path under the system temp dir is classified disposable and never reaches the
    # dedup branch, so a test that did not stub this would silently measure nothing.
    async def _verdict(_request: Any, _raw: str) -> Any:
        return "/repo/page.json", "/repo", False, ""

    async def _promoted(_path: str, _root: str) -> Any:
        return submitted, ""

    monkeypatch.setattr(handler, "_promote_verdict", _verdict)
    monkeypatch.setattr(handler, "_authoritative_promote_content", _promoted)

    class _Request:
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-9-17911"}
        match_info: dict[str, str] = {}
        query: dict[str, str] = {}
        app = {"state": SimpleNamespace()}

        async def read(self) -> bytes:
            return json.dumps(
                {"name": "page", "content": submitted, "source_path": "/tmp/copied.json"}
            ).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        response = await handler.api_artifacts_create(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return 0, True
    return int(response.status), bool(reached)


class TestAReadThatFailedAuthorizesNothing:
    """A snapshot read has three outcomes, and two of them are not "nothing stored".

    Collapsing them is a privilege-class hole: with the stored content read as empty,
    the gate has only the binding the CALLER submitted -- a claim the caller wrote --
    and there is no token to carry into the write. So both guards are skipped at once,
    and the write lands. Two ways in: a transient read failure on an existing package,
    and an owner creating the slug between the read and the write.
    """

    @pytest.mark.asyncio
    async def test_a_read_that_raised_refuses_the_write_to_a_gated_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The submitted content is PROSE, so only the stored record says the gate owns
        this slug -- and it says so from the listing, which is metadata and readable
        when the record's own bytes are not."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted="# gone",
            listed=["page"],
        )
        assert status == 503, "an unreadable dashboard record was written over"
        assert not reached, "the write went to the store anyway"

    @pytest.mark.asyncio
    async def test_a_read_that_raised_refuses_a_submitted_package_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other way the gate owns the write: what is being submitted IS a package,
        whatever the slug holds now."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted=_package_text("crewmate:nyx"),
            listed=[],
        )
        assert status == 503
        assert not reached

    @pytest.mark.asyncio
    async def test_an_unreadable_record_of_another_kind_behaves_as_it_does_on_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE OTHER SIDE of the same question. This gate exists for dashboard packages,
        and a prose document whose bytes cannot be read is the store's own problem to
        report -- refusing it here would charge every other kind for this rule."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted="# today's note",
            listed=[],
        )
        assert reached, "an unreadable note stopped reaching the store"
        assert status == 0

    @pytest.mark.asyncio
    async def test_a_listing_that_also_failed_refuses_the_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: nothing could say whose record this is, and a crewmate's
        dashboard is not written over on the strength of a failed lookup."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted="# gone",
            listed=OSError("the library is unreadable"),
        )
        assert status == 503
        assert not reached

    @pytest.mark.asyncio
    async def test_a_read_that_raised_does_not_reach_the_gate_with_empty_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate is never ASKED about a snapshot nobody read -- an answer of `""`
        from it would be indistinguishable from a legitimate pass."""
        asked: list[Any] = []
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(OSError("the artifact is unreadable")),
            submitted=_package_text("crewmate:nyx"),
            gate=lambda *a: asked.append(a) or "",  # type: ignore[func-returns-value]
        )
        assert status == 503
        assert asked == [], "the gate was asked to judge a snapshot that was never read"
        assert not reached

    @pytest.mark.asyncio
    async def test_a_package_write_to_an_empty_slug_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is stored, so there is no binding to authorize against and no token
        to carry: the race where the owner creates this slug mid-write is the one case
        the store's own 404 does not already cover."""
        status, reached = await _update_against_a_store(
            monkeypatch,
            reader=_raises(_not_found()),
            submitted=_package_text("crewmate:nyx"),
        )
        assert status == 409
        assert not reached, "the write raced the creation of the slug it was judged on"

    @pytest.mark.asyncio
    async def test_an_ordinary_write_to_an_empty_slug_still_reaches_the_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent slug is the store's own 404 to answer, and it says which slug.
        Only a package write is turned away earlier than that."""
        status, reached = await _update_against_a_store(
            monkeypatch, reader=_raises(_not_found()), submitted="# just a note"
        )
        assert reached, "an ordinary write stopped reaching the store"
        assert status == 0


def _not_found() -> Exception:
    from kiro_crew.artifacts import ArtifactNotFoundError

    return ArtifactNotFoundError("no artifact 'page'")


def _raises(exc: Exception) -> Any:
    def _get(_slug: str, **_kwargs: Any) -> Any:
        # ``**_kwargs`` because the real ``get`` takes ``assign_token``: a fake with a
        # narrower signature would raise TypeError and be read as an unreadable record,
        # which is a different case from the one each test means.
        raise exc

    return _get


async def _update_against_a_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reader: Any,
    submitted: str,
    gate: Any = None,
    listed: Any = (),
) -> tuple[int, bool]:
    """``(status, whether the store was written)`` for one ``api_artifact_update``.

    ``status`` is ``0`` when the write reached the store, because the harness stops it
    there rather than building an artifact complete enough to serialize.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    reached: list[bool] = []

    def _update(_slug: str, **_kwargs: Any) -> Any:
        reached.append(True)
        raise _Wrote

    def _list(kind: str | None = None, **_k: Any) -> list[Any]:
        # The LISTING, which is metadata: it answers the kind when the record's own
        # bytes do not. An exception here is the case where nothing can say.
        if isinstance(listed, Exception):
            raise listed
        return [SimpleNamespace(slug=slug, kind="dashboard") for slug in listed]

    monkeypatch.setattr(
        handler,
        "get_default_store",
        lambda: SimpleNamespace(get=reader, update=_update, list=_list),
    )
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)
    if gate is not None:
        monkeypatch.setattr(handler, "_package_write_refusal", gate)

    class _Request:
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-9-17911"}
        match_info = {"slug": "page"}
        app = {"state": SimpleNamespace()}

        async def read(self) -> bytes:
            return json.dumps({"content": submitted}).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        response = await handler.api_artifact_update(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return 0, True
    return int(response.status), bool(reached)


class _Wrote(Exception):
    """The fake store reached the write. Carried as a raise so the harness measures the
    call rather than a stand-in return value."""


async def _update_through_the_route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored: str,
    submitted: str,
    expected_token: str | None = None,
) -> dict[str, Any]:
    """Drive ``api_artifact_update`` over a store that records the write it is handed.

    The owner gate and the restricted-session check are the artifact route's own and
    have their own tests; what this harness isolates is which token reaches the store.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import artifacts as handler

    captured: dict[str, Any] = {}

    def _update(slug: str, **kwargs: Any) -> Any:
        # STOPS HERE, because what reaches the store IS the measurement: returning a
        # stand-in artifact would mean keeping a second fake true -- the response
        # serializer reads a dozen fields off it -- for a value no assertion looks at.
        captured.update(kwargs)
        raise _Wrote

    store = SimpleNamespace(
        get=lambda slug, **_k: SimpleNamespace(
            slug=slug, content=stored, content_token="token-of-the-read", kind="dashboard"
        ),
        update=_update,
    )
    # PATCHED ON THE HANDLER, which imports the accessor at module scope: patching
    # `kiro_crew.artifacts.get_default_store` leaves this module's own name bound to the
    # real store, and the route answers 404 off the developer's own library.
    monkeypatch.setattr(handler, "get_default_store", lambda: store)
    monkeypatch.setattr(handler, "_is_restricted_session", lambda *_a: False)

    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_non_owner_refusal", _allowed)
    # The gate's own answer is not what this measures: it has its own tests above, and
    # a refusal here would stop the write before the token could be read.
    monkeypatch.setattr(handler, "_package_write_refusal", lambda *_a: "")

    body: dict[str, Any] = {"content": submitted}
    if expected_token is not None:
        body["expected_token"] = expected_token

    class _Request:
        headers = {"X-Internal-Secret": "s", "X-Session-Key": "chat-1-1790"}
        match_info = {"slug": "page"}
        app = {"state": SimpleNamespace()}

        async def read(self) -> bytes:
            return json.dumps(body).encode()

        def get(self, key: str, default: Any = None) -> Any:
            return default

    try:
        await handler.api_artifact_update(_Request())  # type: ignore[arg-type]
    except _Wrote:
        return captured
    raise AssertionError("the route never reached the store")
