"""Integration tests for project-bound LLM cron turns in the gateway.

How a job bound to a project directory resolves its agent at fire time: skips
when the folder or the agent is gone, member-bound jobs and member-shadow
refusals, which crew and memory store a project agent runs under, session
reuse across a definition change, and the skip messages a bound job records.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import (
    _make_gw_for_llm,
    _make_gw_for_llm_with_update_job,
    _make_llm_job,
    _make_script_job,
    _recording_cron_provider_factory,
    _resolved_binding,
    _run_llm_callback,
    _run_llm_callback_unresolved_default,
    _set_alias_pinned_model_binding,
    _set_member_template_alias_binding,
    _set_project_resolved_ea_dev_binding,
)

from kiro_crew.cron import CronSchedule


class TestCronBindingMetadataEviction:
    @pytest.mark.asyncio
    async def test_removed_job_id_reuse_cold_starts_single_agent_under_the_new_binding(
        self, tmp_path
    ):
        """Removal eviction cannot let a same-name import reuse the old live session."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        created: list[tuple[object, str | None, str | None]] = []
        manager = SessionManager(
            KiroCrewConfig(),
            provider_factory=_recording_cron_provider_factory(created),
        )
        gateway = _make_gw_for_llm()
        gateway.sessions = manager
        job_id = "sameid01"
        key = f"cron:{job_id}"
        old_dir = tmp_path / "old-project"
        new_dir = tmp_path / "new-project"
        old_dir.mkdir()
        new_dir.mkdir()

        try:
            # Same agent name, different folder: the retained-session agent guard
            # (which defers on an agent mismatch) does not apply, so the binding
            # check is all that stands between the import and the old runtime.
            old_provider, _, _ = await manager.get_or_create(
                key,
                agent="new-agent",
                cwd=str(old_dir),
            )
            manager.release(key)
            gateway._cron_bindings()[key] = (
                str(old_dir),
                "new-agent",
                None,
                "project",
            )
            gateway._evict_removed_cron_session_bindings(frozenset({job_id}))
            assert key not in gateway._cron_bindings()

            real_get_or_create = manager.get_or_create
            job = _make_llm_job(
                id=job_id,
                name="same imported name",
                project_path=str(new_dir),
                agent_id="new-agent",
            )
            resolved = MagicMock(
                requested_resolved=True,
                kiro_agent="new-agent",
                model="",
                memory_store_name="",
                execution_context=None,
                resolved_alias="",
                resolved_source="project",
            )
            with (
                patch("kiro_crew.slack.gateway.resolve_agent_bindings", return_value=resolved),
                patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
                patch("kiro_crew.slack.gateway.publish_turn_identity", AsyncMock()),
            ):
                result, stream = await _run_llm_callback(
                    gateway,
                    job,
                    get_or_create_side_effect=real_get_or_create,
                )

            assert result == "Agent response here"
            assert stream.await_args.args[0] is not old_provider
            assert created[1][1:] == ("new-agent", str(new_dir))
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_pruned_sequence_key_reuse_cold_starts_under_the_new_binding(self, tmp_path):
        """A sequence key reintroduced after pruning cannot inherit its old provider."""
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        created: list[tuple[object, str | None, str | None]] = []
        manager = SessionManager(
            KiroCrewConfig(),
            provider_factory=_recording_cron_provider_factory(created),
        )
        gateway = _make_gw_for_llm()
        gateway.sessions = manager
        job_id = "sequence"
        key = f"cron:{job_id}:planner"
        old_dir = tmp_path / "old-sequence-project"
        new_dir = tmp_path / "new-sequence-project"
        old_dir.mkdir()
        new_dir.mkdir()

        try:
            old_provider, _, _ = await manager.get_or_create(
                key,
                agent="old-planner",
                cwd=str(old_dir),
            )
            manager.release(key)
            gateway._cron_bindings()[key] = (
                str(old_dir),
                "old-planner",
                None,
                "project",
            )
            await gateway._prune_cron_session_bindings(job_id, set())
            assert key not in gateway._cron_bindings()

            real_get_or_create = manager.get_or_create
            job = _make_llm_job(
                id=job_id,
                project_path=str(new_dir),
                agent_sequence=["planner", "worker"],
            )

            def resolved(_cfg, agent: str, *_args, **_kwargs):
                return MagicMock(
                    requested_resolved=True,
                    kiro_agent=f"new-{agent}",
                    model="",
                    memory_store_name="",
                    execution_context=None,
                    resolved_alias="",
                    resolved_source="project",
                )

            with (
                patch("kiro_crew.slack.gateway.resolve_agent_bindings", side_effect=resolved),
                patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
                patch("kiro_crew.slack.gateway.publish_turn_identity", AsyncMock()),
            ):
                result, stream = await _run_llm_callback(
                    gateway,
                    job,
                    get_or_create_side_effect=real_get_or_create,
                )

            assert result == "Agent response here"
            assert stream.await_args_list[0].args[0] is not old_provider
            assert created[1][1:] == ("new-planner", str(new_dir))
        finally:
            await manager.close_all()


class TestProjectPathMissingSkipsRun:
    """A job's project directory existing at save time but gone by fire time
    must SKIP the run entirely (no agent invoked, no session acquired) and
    record it as a normal failed run — not silently fall back to a global
    agent, which ran the wrong agent with no visible sign beyond a list-page
    badge (the original, since-reverted behavior).
    """

    @pytest.mark.asyncio
    async def test_single_agent_job_skips_and_marks_error(self, tmp_path):
        gw = _make_gw_for_llm_with_update_job()
        vanished = str(tmp_path / "does-not-exist")
        job = _make_llm_job(project_path=vanished, agent_id="ea-dev")

        result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "no longer exists" in job.last_error
        # The reason is persisted and read past the owner boundary; the folder
        # itself is owner-only and stays in the log line.
        assert vanished not in job.last_error
        assert job.run_never_started is True
        # No agent turn ran: neither the session acquire nor the prompt
        # stream was ever reached.
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_folder_runs_normally(self, tmp_path):
        # Control: a REAL directory must not trip the skip at all — the
        # normal single-agent turn still runs and returns its result.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev")

        with _resolved_binding() as mock_resolve:
            _set_project_resolved_ea_dev_binding(mock_resolve)
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result == "Agent response here"
        assert job.last_status != "error"
        assert job.run_never_started is False
        gw.sessions.get_or_create.assert_called()

    @pytest.mark.asyncio
    async def test_sequential_job_skips_before_any_sequence_member_runs(self, tmp_path):
        gw = _make_gw_for_llm_with_update_job()
        vanished = str(tmp_path / "does-not-exist")
        job = _make_llm_job(project_path=vanished, agent_sequence=["agent-a", "agent-b"])

        result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "no longer exists" in job.last_error
        assert vanished not in job.last_error
        assert job.run_never_started is True
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()


class TestUnboundSequenceJobIsNotFireTimeValidated:
    """The sequence path's fire-time agent check has the single-agent path's scope.

    The single-agent path validates an agent name at fire time only for a
    PROJECT-BOUND job (a folder is the one place a project definition can shadow
    the name) plus the narrow unbound ``delete_after_run`` one-shot (a vanished
    explicit agent must not consume scheduled work that never ran). An unbound
    recurring job dispatches from its captured execution without being refused,
    which is what lets a sequence member that is not a ``config.agents`` alias --
    an app-registered agent, a crew name the host never aliased -- keep firing.
    The sequence guard is scoped the same way, so the two paths cannot disagree
    about which jobs a stale roster is allowed to stop.
    """

    @pytest.mark.asyncio
    async def test_an_unbound_recurring_sequence_fires_on_an_unresolved_name(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(agent_sequence=["ghost-a", "ghost-b"])
        assert not job.project_path and not job.member_id and not job.delete_after_run

        result, _stream_mock = await _run_llm_callback_unresolved_default(gw, job)

        assert job.run_never_started is False, (
            "an unbound recurring sequence job was refused at fire time for a member "
            "name that is not a config alias -- the shape the single-agent path exempts"
        )
        assert job.last_status != "error"
        assert result == "Agent response here"
        gw.sessions.get_or_create.assert_called()
        assert gw.sessions.get_or_create.call_args_list[0].kwargs["agent"] == "ghost-a"

    @pytest.mark.asyncio
    async def test_an_unbound_one_shot_sequence_is_still_validated(self, tmp_path):
        # Control: the one-shot exception applies to the sequence path as it does
        # to the single-agent path -- a vanished member must not consume the
        # scheduled work, so the fire is the named never-started skip.
        from kiro_crew.cron import CronService

        gw = _make_gw_for_llm()
        job = _make_llm_job(
            agent_sequence=["ghost-a", "ghost-b"],
            delete_after_run=True,
            schedule=CronSchedule(kind="at", at_ts=9999999999.0),
        )
        svc = CronService(base_dir=tmp_path)
        svc._jobs = [job]
        svc._save()

        result, _stream_mock = await _run_llm_callback_unresolved_default(gw, job)

        assert result is None
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert "ghost-a" in job.last_error
        gw.sessions.get_or_create.assert_not_called()
        svc._merge_job_result(job)
        assert any(
            j.id == job.id for j in svc.list_jobs(include_disabled=True)
        ), "the skipped one-shot was consumed"


class TestUnresolvedProjectAgentSkipsRun:
    """A job whose ``agent_id``/sequence member names a specific agent that
    ``resolve_agent_bindings`` cannot find inside ``project_path`` (the
    folder exists, but the agent's own JSON does not, or never did) must
    SKIP the run — not silently execute under the default agent's tools and
    permissions with no visible sign anything was substituted. Distinct from
    ``TestProjectPathMissingSkipsRun``, which covers the folder itself being
    gone; this covers the folder existing but not declaring the requested
    agent.
    """

    @pytest.mark.asyncio
    async def test_single_agent_job_skips_and_marks_error(self, tmp_path):
        gw = _make_gw_for_llm_with_update_job()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ghost-agent")

        with _resolved_binding() as mock_resolve:
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.kiro_agent = "default-agent"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "ghost-agent" in job.last_error
        assert job.run_never_started is True
        # No agent turn ran: neither the session acquire nor the prompt
        # stream was ever reached — the resolved default binding must never
        # be handed to a session.
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_sequential_job_skips_before_any_sequence_member_runs(self, tmp_path):
        gw = _make_gw_for_llm_with_update_job()
        job = _make_llm_job(project_path=str(tmp_path), agent_sequence=["ghost-a", "ghost-b"])

        with _resolved_binding() as mock_resolve:
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.kiro_agent = "default-agent"
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "ghost-a" in job.last_error
        assert job.run_never_started is True
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sequence", [False, True], ids=["single-agent", "sequence"])
    async def test_skip_log_redacts_job_and_agent_secrets(self, tmp_path, caplog, sequence):
        """Both unresolved-agent log sites scrub the job name and reason."""
        credential_name = "AKIAIOSFODNN7EXAMPLE"
        exfil_agent = (
            "https://collect.attacker.example/?token="
            + "aB3" * 70
            + "&path=/home/alice/.aws/credentials"
        )
        gw = _make_gw_for_llm()
        kwargs = {
            "name": credential_name,
            "project_path": str(tmp_path),
            "agent_sequence" if sequence else "agent_id": (
                [exfil_agent, "second-agent"] if sequence else exfil_agent
            ),
        }
        job = _make_llm_job(**kwargs)

        with (
            _resolved_binding() as mock_resolve,
            caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"),
        ):
            mock_resolve.return_value.requested_resolved = False
            mock_resolve.return_value.kiro_agent = "default-agent"
            mock_resolve.return_value.resolved_source = "default"
            result, stream = await _run_llm_callback(gw, job)

        assert result is None
        stream.assert_not_awaited()
        assert exfil_agent in job.last_error, "the test did not reach the named skip branch"
        emitted = "\n".join(caplog.messages)
        assert exfil_agent not in emitted, (
            f"the {'sequence' if sequence else 'single-agent'} unresolved-agent log "
            "leaked the exfiltration-shaped agent name"
        )
        assert credential_name not in emitted, (
            f"the {'sequence' if sequence else 'single-agent'} unresolved-agent log "
            "leaked the credential-shaped job name"
        )

    @pytest.mark.asyncio
    async def test_a_job_naming_no_agent_is_probed_as_the_default_not_as_a_name(self):
        """A job that selected NO agent must be probed as the default.

        ``cron_agent`` is the execution's ``template_id``, and that falls back to
        the literal ``"kirocrew"`` when the job named nothing. Handing that
        fallback to the resolver asks "is 'kirocrew' a configured alias?", which is
        False on any host that does not happen to define one -- so the guard below
        refused every job left on "Leave default for the primary agent", replacing
        the silent substitution this feature exists to stop with a silent refusal.
        ``requested_resolved`` is True for a ``None`` name by construction, which is
        the invariant the save-time check states as "an empty agent means the
        default, which always resolves"; this pins that the fire path asks the same
        question. No ``project_path`` is set, so the guard's own folder condition is
        not what carries the test.
        """
        gw = _make_gw_for_llm_with_update_job()
        job = _make_llm_job()
        assert not job.agent_id and not job.member_id

        def _fake_resolve(_cfg, agent_name, _project, **_kw):
            # Mirrors the loader's own rule, `(not agent_name) or alias_hit or
            # passthrough`, with no alias configured -- so a NAME reads as
            # unresolved and None reads as resolved. A flat `requested_resolved =
            # False` stub would refuse the run no matter what the fire path asked,
            # and a flat True would pass even if it asked the wrong question.
            bindings = MagicMock()
            bindings.requested_resolved = agent_name is None
            bindings.kiro_agent = "default-agent"
            bindings.resolved_source = "global"
            return bindings

        with patch("kiro_crew.slack.gateway.resolve_agent_bindings", _fake_resolve) as _:
            result, _stream_mock = await _run_llm_callback(gw, job)

        # The run is not refused: the default always resolves, and it only does so
        # here because the fire path asked about the default rather than about the
        # synthesized "kirocrew" template id.
        assert result is not None
        assert job.last_status != "error"
        _stream_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolved_project_agent_still_runs_normally(self, tmp_path):
        # Control: requested_resolved=True (the agent WAS found in the
        # project) must not trip the skip — the normal single-agent turn
        # still runs and returns its result.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev")

        with _resolved_binding() as mock_resolve:
            _set_project_resolved_ea_dev_binding(mock_resolve)
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result == "Agent response here"
        assert job.last_status != "error"
        assert job.run_never_started is False
        gw.sessions.get_or_create.assert_called()

    @pytest.mark.asyncio
    async def test_sequential_run_never_starts_if_a_later_member_is_unresolvable(self, tmp_path):
        # Resolving every sequence member in one pre-pass, before any session
        # is acquired, means an unresolvable later member is caught before the
        # FIRST member ever runs -- resolving inside the per-agent execution
        # loop instead would let an early member run a REAL turn before a
        # later member's unresolvable agent aborts the whole run and marks it
        # run_never_started=True, mislabeling a run that already executed.
        # The pre-pass keeps run_never_started honest.
        gw = _make_gw_for_llm_with_update_job()
        job = _make_llm_job(
            project_path=str(tmp_path), agent_sequence=["resolves-fine", "ghost-agent"]
        )

        def _resolve_side_effect(_cfg, agent, _project_path, **_kwargs):
            b = MagicMock()
            b.requested_resolved = agent != "ghost-agent"
            b.kiro_agent = agent
            b.model = ""
            b.memory_store_name = ""
            b.resolved_source = "project"
            return b

        with (
            patch(
                "kiro_crew.slack.gateway.resolve_agent_bindings",
                side_effect=_resolve_side_effect,
            ),
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            result, _stream_mock = await _run_llm_callback(gw, job)

        assert result is None
        assert job.last_status == "error"
        assert "ghost-agent" in job.last_error
        assert job.run_never_started is True
        # The whole point of the fix: NO session was ever acquired, even for
        # the first (resolvable) member -- proving the abort happened in the
        # pre-pass, before any turn ran.
        gw.sessions.get_or_create.assert_not_called()
        _stream_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolved_alias_model_is_not_dropped(self, tmp_path):
        # Substituting job.agent_id with _bindings.kiro_agent (the raw
        # kiro-cli agent name) so the resolved agent actually dispatches must
        # not silently drop that alias's OWN configured model tier
        # (ResolvedBindings.model) -- get_or_create must not see only the bare
        # kiro_agent name and fall through to whatever THAT agent defaults to.
        # job.model (a job-level pin) is empty here, so the alias's model must
        # be the one that reaches get_or_create.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev", model="")

        with _resolved_binding() as mock_resolve:
            _set_alias_pinned_model_binding(mock_resolve)
            await _run_llm_callback(gw, job)

        gw.sessions.get_or_create.assert_called()
        _, kwargs = gw.sessions.get_or_create.call_args
        assert kwargs.get("model") == "alias-pinned-model"
        assert kwargs.get("agent") == "raw-kiro-agent-name"

    @pytest.mark.asyncio
    async def test_job_level_model_pin_still_outranks_the_alias_model(self, tmp_path):
        # A job-level job.model pin outranks the resolved alias's own model --
        # confirming the fix's precedence (job.model or alias_model) rather
        # than accidentally swapping which one wins.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev", model="job-pinned-model")

        with _resolved_binding() as mock_resolve:
            _set_alias_pinned_model_binding(mock_resolve)
            await _run_llm_callback(gw, job)

        _, kwargs = gw.sessions.get_or_create.call_args
        assert kwargs.get("model") == "job-pinned-model"

    @pytest.mark.asyncio
    async def test_an_unavailable_alias_model_is_named_in_the_downgrade_notice(self, tmp_path):
        # With no job-level pin, the model that failed is the alias's own, so
        # the notice must name it rather than the job's empty `model`.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="ea-dev", model="")
        calls: list[dict] = []

        async def _get_or_create(*_args, **kwargs):
            calls.append(kwargs)
            if kwargs.get("model"):
                raise RuntimeError(f"model {kwargs['model']} is unavailable")
            return MagicMock(), True, False

        with _resolved_binding() as mock_resolve:
            _set_alias_pinned_model_binding(mock_resolve)
            result, _stream_mock = await _run_llm_callback(
                gw, job, get_or_create_side_effect=_get_or_create
            )

        assert [c.get("model") for c in calls] == ["alias-pinned-model", None]
        assert result.startswith("⚠️ Model 'alias-pinned-model' unavailable; ran with default.")


class TestMemberShadowRefusalAudit:
    @staticmethod
    def _shadowed_bindings():
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED

        bindings = MagicMock()
        bindings.requested_resolved = False
        bindings.kiro_agent = "member-template"
        bindings.resolved_source = RESOLVED_SOURCE_MEMBER_SHADOWED
        return bindings

    @classmethod
    @contextlib.contextmanager
    def _patched_shadow_refusal(cls):
        with (
            patch(
                "kiro_crew.slack.gateway.resolve_agent_bindings",
                return_value=cls._shadowed_bindings(),
            ),
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch(
                "kiro_crew.cron.resolve_cron_memory",
                return_value=("dev-private", "member-template"),
            ),
        ):
            yield

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sequence", [False, True], ids=["single-agent", "sequence"])
    async def test_refusal_emits_sel_denial(self, tmp_path, sequence):
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED

        gw = _make_gw_for_llm()
        kwargs = {
            "project_path": str(tmp_path),
            "member_id": "dev",
            "agent_sequence" if sequence else "agent_id": (
                ["member-template", "second-member"] if sequence else "member-template"
            ),
        }
        job = _make_llm_job(**kwargs)
        sel_factory = MagicMock()

        with self._patched_shadow_refusal():
            result, stream = await _run_llm_callback(gw, job, sel_factory=sel_factory)

        assert result is None
        assert job.run_never_started is True
        gw.sessions.get_or_create.assert_not_called()
        stream.assert_not_awaited()
        calls = sel_factory.return_value.log_api_access.call_args_list
        assert len(calls) == 1, "member-shadow refusal emitted no SEL denial"
        assert calls[0].kwargs == {
            "caller": f"cron:{job.id}",
            "operation": "cron.fire.project_bound_job",
            "outcome": "denied",
            "source": "cron",
            "resources": job.id,
            "error": RESOLVED_SOURCE_MEMBER_SHADOWED,
        }

    @pytest.mark.asyncio
    async def test_sel_failure_preserves_the_single_agent_refusal(self, tmp_path):
        gw = _make_gw_for_llm()
        job = _make_llm_job(
            project_path=str(tmp_path),
            member_id="dev",
            agent_id="member-template",
        )
        sel_factory = MagicMock()
        sel_factory.return_value.log_api_access.side_effect = RuntimeError("SEL unavailable")

        with self._patched_shadow_refusal():
            result, stream = await _run_llm_callback(gw, job, sel_factory=sel_factory)

        assert (
            sel_factory.return_value.log_api_access.call_count == 1
        ), "the member-shadow refusal never attempted its best-effort SEL denial"
        assert result is None, "an SEL failure escaped the clean refusal"
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert "This job is bound to a Crew Member" in job.last_error
        gw.sessions.get_or_create.assert_not_called()
        stream.assert_not_awaited()


class TestMemberBoundJobWithAnOperatingFolder:
    """A member-bound job may be given a project directory, and the two concerns
    stay separate: the folder supplies the run's CWD, while the member keeps its
    own template, lineage and memory.

    Reachable in the product: the folder field renders in the crew editor's
    schedule section, which is the host that sets ``member_id``.
    Project precedence must NOT apply here: substituting a same-named project
    definition would re-base the member onto a different parent template and trip
    the ``parent_identity_changed`` guard, stopping the job from starting at all.
    """

    @pytest.mark.asyncio
    async def test_a_member_bound_job_opts_out_of_project_precedence(self, tmp_path):
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="member-template", member_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch("kiro_crew.cron.resolve_cron_memory", return_value=("", "member-template")),
        ):
            _set_member_template_alias_binding(mock_resolve)
            await _run_llm_callback(gw, job)

        # The member keeps its lineage: precedence is explicitly disabled.
        _, resolve_kwargs = mock_resolve.call_args
        assert resolve_kwargs.get("allow_project_override") is False

    @pytest.mark.asyncio
    async def test_the_folder_still_supplies_the_cwd(self, tmp_path):
        # The other half of the split rule: opting out of project precedence must
        # not also discard the folder, which is what the operator actually asked
        # for by setting it.
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="member-template", member_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch("kiro_crew.cron.resolve_cron_memory", return_value=("", "member-template")),
        ):
            _set_member_template_alias_binding(mock_resolve)
            await _run_llm_callback(gw, job)

        _, kwargs = gw.sessions.get_or_create.call_args
        assert kwargs.get("cwd") == str(tmp_path)

    @pytest.mark.asyncio
    async def test_the_members_own_name_wins_over_the_resolved_one(self, tmp_path):
        # Map job.member_id back to its config.agents NAME: the resolver is
        # called with the member's kiro_agent, so a same-named project agent
        # can make resolved_alias fall back to a DIFFERENT member and apply
        # that member's crew-scoped effort/watchdog settings and memory namespace.
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg = KiroCrewConfig(
            agents={
                "Development Member": KiroCrewAgentConfig(
                    kiro_agent="member-template", member_id="dev"
                )
            }
        )
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="member-template", member_id="dev")

        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch("kiro_crew.cron.resolve_cron_memory", return_value=("", "member-template")),
        ):
            _set_member_template_alias_binding(mock_resolve)
            await _run_llm_callback(gw, job)

        assert gw._cron_session_binding[f"cron:{job.id}"][2] == "Development Member"


PRIVATE_RESULT = "summary drawn from the crew's private memory"


class TestAProjectFileNamedLikeTheDispatchedTemplate:
    """A bound checkout cannot borrow the job's private store by shipping a file
    named after the TEMPLATE the fire dispatches.

    The resolver's own collision check judges the requested NAME (and, under the
    member opt-out, that name's template). Neither name is the one at risk here:
    a job whose identity comes from its captured carrier dispatches
    ``execution_context.template_id`` (``resolve_agent_bindings``'s ``kiro_agent``
    is read straight off the carrier), and a job that names no agent at all is
    probed as the DEFAULT -- ``agent_name`` is ``None``, so the resolver's probe
    is not even reached. Either way the dispatched template can be a name the
    resolver never compared, the fire's cwd is the bound folder, and the backend
    resolves that template project-first -- so the project's own file answers the
    turn while the carrier still names the crew's private silo. A project's
    ``.kiro/agents`` file is writable by anyone who can land a branch in that
    checkout, which is the same "land a branch, read a crew's private memory"
    hazard ``_project_dispatch_execution`` already states for the alias-name
    collision, reached through the template name instead.
    """

    @staticmethod
    def _cfg():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.config.loader import (
            KiroCrewAgentConfig,
            MemoryStoreConfig,
            WorkspaceConfig,
        )

        return KiroCrewConfig(
            agents={
                "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
                # The alias is spelled differently from the template it points
                # at, and the template is what a fire actually dispatches.
                "dev": KiroCrewAgentConfig(kiro_agent="dev-template", memory_store="dev-private"),
            },
            default_agent="default",
            workspaces={"default": WorkspaceConfig(dir="/tmp/ws")},
            default_workspace="default",
            memory_stores={"default": MemoryStoreConfig(), "dev-private": MemoryStoreConfig()},
            default_memory_store="default",
        )

    @staticmethod
    def _project(tmp_path, declared: str):
        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{declared}.json").write_text(f'{{"name": "{declared}"}}', encoding="utf-8")
        return str(tmp_path)

    @staticmethod
    def _carrier():
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        # The shape a cron created from a chat session on alias "dev" persists:
        # the crew's private store, and the crew's TEMPLATE as the dispatched id.
        return ExecutionContext(
            member_id=None,
            store=MemoryStoreRef("dev-private"),
            selection_kind="member",
            template_id="dev-template",
            selection_name="dev",
        )

    async def _dispatched_execution(self, gw, job, monkeypatch):
        from kiro_crew import execution_context
        from kiro_crew.agent_discovery import clear_project_agent_cache

        published: list = []
        original = execution_context.bind_session_execution

        def _bind(key, execution, **kw):
            published.append(execution)
            return original(key, execution, **kw)

        monkeypatch.setattr(execution_context, "bind_session_execution", _bind)
        clear_project_agent_cache()
        with patch("kiro_crew.slack.gateway.KiroCrewConfig.load", return_value=self._cfg()):
            await _run_llm_callback(gw, job)
        assert published, "the fire never bound a session execution"
        return published[0]

    async def _built_prompt(self, job, monkeypatch):
        """The prompt text and ``compressed_history`` the fire's first turn built."""
        from kiro_crew.agent_discovery import clear_project_agent_cache

        clear_project_agent_cache()
        embeds: list = []
        with patch("kiro_crew.slack.gateway.KiroCrewConfig.load", return_value=self._cfg()):
            await _run_llm_callback(
                _make_gw_for_llm(), job, real_session_context=True, embed_calls=embeds
            )
        call = embeds[0].await_args_list[0]
        return call.args[1], call.kwargs.get("compressed_history")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sequence", [False, True], ids=["single-agent", "sequence"])
    async def test_the_displacing_agent_is_not_handed_the_private_stores_output(
        self, tmp_path, monkeypatch, sequence
    ):
        # The job's previous result and its transcript were composed under the
        # crew's private store. The project's file now answers on Global memory,
        # so neither may reach it through the prompt either.
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            execution_context=self._carrier().to_record(),
            last_result=PRIVATE_RESULT,
            **({"agent_sequence": ["dev", "default"]} if sequence else {}),
        )
        prompt, history = await self._built_prompt(job, monkeypatch)

        assert PRIVATE_RESULT not in prompt
        assert history == "", "the private store's transcript was replayed to the project agent"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sequence", [False, True], ids=["single-agent", "sequence"])
    async def test_a_job_that_keeps_its_store_keeps_its_context(
        self, tmp_path, monkeypatch, sequence
    ):
        # Control: no collision, so the job runs on its own store and keeps the
        # carried result and the normal replay.
        from kiro_crew.memory_stores import _named_store_dir

        # A step that keeps the private store opens it, so it has to exist.
        _named_store_dir("dev-private").mkdir(parents=True, exist_ok=True)
        job = _make_llm_job(
            project_path=self._project(tmp_path, "unrelated"),
            execution_context=self._carrier().to_record(),
            last_result=PRIVATE_RESULT,
            **({"agent_sequence": ["dev", "default"]} if sequence else {}),
        )
        prompt, history = await self._built_prompt(job, monkeypatch)

        assert PRIVATE_RESULT in prompt
        assert history is None

    @pytest.mark.asyncio
    async def test_the_projects_file_does_not_inherit_the_private_store(
        self, tmp_path, monkeypatch
    ):
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "default", (
            "the bound checkout ships a file named after the dispatched template, "
            "so its own definition answers the turn -- under the crew's private "
            "memory store"
        )
        assert execution.member_id is None
        # Still dispatched: the project file is what runs, so re-pointing the
        # STORE must not also make the fire skip.
        assert execution.template_id == "dev-template"

    @pytest.mark.asyncio
    async def test_a_checkout_declaring_another_name_keeps_the_private_store(
        self, tmp_path, monkeypatch
    ):
        # Control: the re-point is driven by the COLLISION, not by binding a
        # folder. A checkout that declares some other agent cannot reach the
        # dispatched template, so the job keeps its own silo.
        job = _make_llm_job(
            project_path=self._project(tmp_path, "unrelated"),
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "dev-private"
        assert execution.template_id == "dev-template"

    @pytest.mark.asyncio
    async def test_a_sequence_member_does_not_inherit_the_private_store(
        self, tmp_path, monkeypatch
    ):
        # The sequence path resolves each member WITHOUT the carrier, so its
        # dispatched template comes from the alias record instead -- and the
        # job-level carrier (which can name a private store) is what a member
        # falls through to. Same collision, second path.
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            agent_sequence=["dev"],
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "default"
        assert execution.template_id == "dev-template"

    @pytest.mark.asyncio
    async def test_the_collision_check_reads_no_project_filesystem_on_the_loop(
        self, tmp_path, monkeypatch
    ):
        # The check runs on the event loop, so it must consult the WARMED name
        # cache and nothing else -- the same rule _project_declares_agent states
        # for its own on-loop read, and for the same reason: a scan here is an
        # unbounded stall on chat, WebSocket and heartbeat processing. Widening
        # this into a fresh scan would make every project-bound fire pay for a
        # checkout walk.
        import kiro_crew.agent_discovery as ad

        loop_thread = threading.get_ident()
        scanned: list[int] = []
        real_scan = ad.project_agent_names

        def _scan(*args, **kwargs):
            scanned.append(threading.get_ident())
            return real_scan(*args, **kwargs)

        monkeypatch.setattr(ad, "project_agent_names", _scan)
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            execution_context=self._carrier().to_record(),
        )
        execution = await self._dispatched_execution(_make_gw_for_llm(), job, monkeypatch)

        assert execution.store.store_id == "default"
        assert scanned, "nothing scanned the checkout at all -- the warm is the hit's source"
        assert all(thread != loop_thread for thread in scanned), (
            "the dispatched-template collision check scanned the project ON the "
            "event loop; it must read the cache the warm populated"
        )

    @pytest.mark.asyncio
    async def test_a_cold_name_cache_drops_to_global_memory(self, tmp_path, monkeypatch):
        # The warm runs for this same path before the read, so a MISS means it
        # could not answer -- and the backend still resolves the project file
        # first, so keeping the captured store would hand a project definition
        # sharing the alias's name that crew's private memory. Unknown is treated
        # as shadowed: Global memory is a lost capability, the captured store
        # handed to a project file is an exposure. The read stays cache-only
        # because this runs on the event loop.
        job = _make_llm_job(
            project_path=self._project(tmp_path, "dev-template"),
            execution_context=self._carrier().to_record(),
        )
        gw = _make_gw_for_llm()
        with patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()):
            execution = await self._dispatched_execution(gw, job, monkeypatch)

        assert execution.store.store_id != "dev-private"


class TestSessionReuseNoticesWhichDefinitionWon:
    """The session-reuse key carries the resolved SOURCE, so project-agent
    precedence holds in steady state rather than only at cold start.

    A live persistent session is reused regardless of the cwd/agent passed to
    ``get_or_create``. Dropping a ``<project>/.kiro/agents/<name>.json`` beside a
    same-named alias changes WHICH definition answers while the cwd and the
    requested name both stay put -- and where the crew's template is spelled like
    the crew on a host whose ``default_agent`` is that crew, the resolved agent and
    alias are identical too. Without the source tag a job firing every few minutes
    would keep running the losing definition until an idle eviction.
    """

    @staticmethod
    def _binding_mock(source: str):
        b = MagicMock()
        b.requested_resolved = True
        # Deliberately IDENTICAL across both fires -- only the source differs.
        b.kiro_agent = "dev"
        b.model = ""
        b.memory_store_name = ""
        b.execution_context = None
        b.resolved_alias = "dev"
        b.resolved_source = source
        return b

    @staticmethod
    def _gw_with_pending_subagents():
        gw = _make_gw_for_llm()
        gw.subagent_mgr = MagicMock()
        gw.subagent_mgr.has_pending_work_for = MagicMock(
            side_effect=AssertionError("sync pending probe called")
        )
        gw.subagent_mgr.has_pending_work_for_async = AsyncMock(return_value=True)
        return gw

    @pytest.mark.asyncio
    async def test_a_source_change_alone_defers_the_fire(self, tmp_path):
        # The deferral is the unambiguous observable: a binding mismatch with
        # subagents pending returns without running the turn, and NOTHING else in
        # this path produces that. (A plain reset is not a usable signal here --
        # the run's own finally block releases and resets every fire.)
        gw = self._gw_with_pending_subagents()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="dev")

        with _resolved_binding() as mock_resolve:
            mock_resolve.return_value = self._binding_mock("alias")
            await _run_llm_callback(gw, job)
            assert gw._cron_session_binding[f"cron:{job.id}"][3] == "alias"

            # A project file now shadows the alias. cwd, requested name, resolved
            # agent and resolved crew alias are all unchanged.
            mock_resolve.return_value = self._binding_mock("project")
            result, stream_mock = await _run_llm_callback(gw, job)

        assert result is None, "the fire should have been deferred"
        stream_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unchanged_source_does_not_defer(self, tmp_path):
        # The control: identical bindings across fires must NOT defer, or every
        # persistent job with a live subagent would stall forever.
        gw = self._gw_with_pending_subagents()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="dev")

        with _resolved_binding() as mock_resolve:
            mock_resolve.return_value = self._binding_mock("alias")
            await _run_llm_callback(gw, job)
            mock_resolve.return_value = self._binding_mock("alias")
            result, stream_mock = await _run_llm_callback(gw, job)

        assert result is not None
        stream_mock.assert_awaited()


class TestTheSkipMessageNamesTheRealReason:
    """The ``last_error`` an unresolved agent leaves on a skipped fire.

    This string is the ONLY signal the operator gets for these skips: they
    spend no auto-pause strike by design, so nothing else escalates and the
    text itself has to carry the remedy. Three shapes, and the third is why
    the wording is branched at all -- a member-bound job shadowed by its bound
    directory refuses BECAUSE the agent is present there, so the unbranched
    "not found in project directory" would state the opposite of the fact and
    send the operator to create a file that already exists.
    """

    def test_an_unbound_job_says_the_name_is_gone(self):
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="")
        msg = _agent_unresolved_message(job, "ghost")
        assert "Agent 'ghost' is no longer configured" in msg
        # Every branch names a NEXT STEP, not just a diagnosis (UX Review).
        assert "Pick another agent" in msg, "the unbound skip named no remedy"

    def test_a_bound_job_names_the_directory_to_fix(self):
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="/repo")
        msg = _agent_unresolved_message(job, "ghost")
        assert "not found in this job's project directory" in msg
        # The directory to CREATE the file in, not merely the one it is missing
        # from: an operator should not have to know the .kiro/agents/ convention
        # to act on this. The CONVENTION is named; the folder itself is not (see
        # the disclosure test below) -- the owner reads the folder off the job.
        assert ".kiro/agents/" in msg, "the skip did not say where to add the agent"
        assert "/repo" not in msg

    def test_the_member_shadow_refusal_does_not_claim_the_agent_is_missing(self):
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="/repo")
        msg = _agent_unresolved_message(job, "default", RESOLVED_SOURCE_MEMBER_SHADOWED)

        assert "not found" not in msg, (
            "the shadow refusal reported the agent as missing from the very "
            "directory that declares it -- the operator would go add a file "
            "that is already there, when the fix is to rename it or unbind "
            "the member"
        )
        assert "project directory" in msg, "the refusal did not say WHERE the shadowing file is"
        assert "/repo" not in msg
        assert "Crew Member" in msg, "the refusal did not say WHAT the project file collides with"
        # The remedies, not just the diagnosis: this text is the whole signal.
        assert "Rename" in msg and "unbind" in msg, (
            "the refusal named no remedy, leaving the operator with a skipped "
            "job and no stated way to clear it"
        )

    def test_the_member_shadow_refusal_leads_with_the_plain_fact(self):
        """UX Review: the state, in plain words, before any remedy or mechanism.

        Internal vocabulary in the opening clause -- shadowing, a member's
        private memory -- spends the one moment the operator most needs plain
        words, on a state rare enough that they are reading it for the first
        time. This string is their only signal, so it states WHAT IS TRUE first
        (the job is bound to a Crew Member, and the directory defines an agent
        of the same name), then the consequence, then the two remedies.
        """
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED
        from kiro_crew.slack.gateway import _agent_unresolved_message

        job = _make_script_job(project_path="/repo")
        msg = _agent_unresolved_message(job, "default", RESOLVED_SOURCE_MEMBER_SHADOWED)

        assert msg.startswith("This job is bound to a Crew Member"), (
            "the refusal did not open on the plain fact; the operator reads "
            f"mechanism before state: {msg!r}"
        )
        assert "the same name" in msg, "the refusal never plainly said the names collide"
        # The flagged vocabulary, gone: neither term survives anywhere in the
        # sentence, not merely moved later in it.
        for jargon in ("shadow", "private memory"):
            assert jargon not in msg, f"internal vocabulary {jargon!r} survived in {msg!r}"
        # The remedies keep their own clause and stay LAST -- a remedy read
        # before the state it repairs is a remedy for nothing.
        assert msg.index("Rename the project's agent") > msg.index("the same name")

    @pytest.mark.asyncio
    async def test_no_persisted_skip_reason_names_the_bound_folder(self, tmp_path):
        """``last_error`` is read past the owner boundary; the folder is not.

        ``GET /api/crons`` serializes ``last_error`` for every dashboard token and
        withholds ``project_path`` from a non-owner; the run-history routes
        serialize the same string as a run's ``summary``/``error`` with no owner
        gate at all, and ``cron_list`` shows it in chat. So the skip REASON is
        readable by a non-owner by design (it is the only diagnosis a skip
        leaves), and the folder must therefore not ride inside it -- neither the
        path itself nor its ``<path>/.kiro/agents/`` layout. Every persisted skip
        builder is covered here: the two ``_agent_unresolved_message`` shapes for
        a bound job and the vanished-directory skip. The path stays in the log
        line, which is where an operator-only diagnosis belongs.
        """
        from kiro_crew.config.loader import RESOLVED_SOURCE_MEMBER_SHADOWED
        from kiro_crew.slack.gateway import (
            _agent_unresolved_message,
            _project_directory_vanished,
        )

        folder = str(tmp_path / "private-repo")
        job = _make_script_job(project_path=folder)
        for msg in (
            _agent_unresolved_message(job, "ghost"),
            _agent_unresolved_message(job, "ghost", RESOLVED_SOURCE_MEMBER_SHADOWED),
        ):
            assert folder not in msg, msg
            assert "private-repo" not in msg, msg
        assert await _project_directory_vanished(job) is True
        assert job.last_status == "error"
        assert folder not in job.last_error, job.last_error
        assert "private-repo" not in job.last_error, job.last_error
        assert "no longer exists" in job.last_error

    @pytest.mark.asyncio
    async def test_vanished_directory_log_line_does_not_carry_the_raw_folder(
        self, tmp_path, caplog
    ):
        """The vanished-directory skip's log line must not carry the raw path.

        The dashboard log subscription gate (``dashboard/ws.py``'s
        ``subscribe_logs`` handler) admits any ``_is_dashboard_user`` socket --
        that flag is set for every authenticated dashboard token holder,
        including a non-owner ``!dashboard`` Slack subject (``app == ""`` sets
        ``is_dashboard_user = not _app = True``), not only the configured
        ``owner_id``. ``is_owner_dashboard_request`` is a distinct, stricter
        predicate this gate never calls. So the log stream is a
        dashboard-*user* surface, not an owner-only one, and a folder path
        logged there reaches the same non-owner the cron owner gate exists to
        keep it from. The path stays useful to the operator as the folder's
        last path segment (``private-repo``) is still present; the fix is to
        strip it via :func:`~kiro_crew.security.redaction.redact_local_paths`
        the same way every other subprocess/OS-error surface that reaches a
        browser does, not to drop the diagnosis outright.
        """
        import logging

        from kiro_crew.slack.gateway import _project_directory_vanished

        folder = str(tmp_path / "private-repo")
        credential_name = "AKIAIOSFODNN7EXAMPLE"
        job = _make_script_job(project_path=folder, name=credential_name)

        with caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
            assert await _project_directory_vanished(job) is True

        full_text = "\n".join(caplog.messages)
        assert (
            credential_name not in full_text
        ), "the vanished-directory log leaked the credential-shaped job name"
        assert folder not in full_text, (
            "the vanished-directory log line carried the raw project folder "
            "path, which a non-owner dashboard-token holder (a `!dashboard` "
            "Slack subject) can read via the WS log subscription -- that gate "
            "checks only `_is_dashboard_user`, never `is_owner_dashboard_request`"
        )


class TestAProjectTemplateCarriesNoCrew:
    """A project-bound job whose name resolved to a project file or a materialized
    template dispatches that template, not a crew member. ``resolved_alias`` is
    then only the default alias the bindings fell back to, and passing it on as
    ``crew_agent`` would run the default member's capability preparation in the
    job's folder, which refuses with ``project_identity_changed`` whenever that
    member is enrolled for another workspace -- on every fire, until auto-pause.
    """

    @staticmethod
    def _bind(mock_resolve, source):
        mock_resolve.return_value.requested_resolved = True
        mock_resolve.return_value.kiro_agent = "repo-agent"
        mock_resolve.return_value.model = ""
        mock_resolve.return_value.resolved_alias = "default-member"
        mock_resolve.return_value.resolved_source = source
        mock_resolve.return_value.memory_store_name = ""
        mock_resolve.return_value.execution_context = None

    async def _fire(self, tmp_path, source, **job_kwargs):
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), **job_kwargs)
        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
        ):
            self._bind(mock_resolve, source)
            await _run_llm_callback(gw, job)
        return gw, job

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["materialized", "project"])
    async def test_a_single_agent_fire_binds_no_crew(self, tmp_path, source):
        gw, job = await self._fire(tmp_path, source, agent_id="repo-agent")

        kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        # The explicit no-crew value: None would let resolve_crew_identity's
        # namespace fallback pick a crew back up.
        assert kwargs["crew_agent"] == ""
        assert gw._cron_session_binding[f"cron:{job.id}"][2] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["materialized", "project"])
    async def test_a_sequence_step_binds_no_crew(self, tmp_path, source):
        gw, _ = await self._fire(tmp_path, source, agent_id="", agent_sequence=["repo-agent"])

        crews = [c.kwargs["crew_agent"] for c in gw.sessions.get_or_create.call_args_list]
        assert crews and all(crew == "" for crew in crews)

    @pytest.mark.asyncio
    async def test_an_alias_hit_keeps_its_crew(self, tmp_path):
        gw, _ = await self._fire(tmp_path, "alias", agent_id="default-member")

        kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert kwargs["crew_agent"] == "default-member"


class TestCronProjectCrewAlias:
    """The crew rule itself, independent of a fire."""

    def test_project_and_template_answers_carry_no_crew(self):
        from kiro_crew.slack.gateway import _cron_project_crew_alias

        cfg = MagicMock(agents={})
        assert _cron_project_crew_alias(cfg, "", "default-member", "materialized") == ""
        assert _cron_project_crew_alias(cfg, "", "default-member", "project") == ""

    def test_an_alias_hit_and_a_nameless_default_keep_their_alias(self):
        from kiro_crew.slack.gateway import _cron_project_crew_alias

        cfg = MagicMock(agents={})
        assert _cron_project_crew_alias(cfg, "", "captain", "alias") == "captain"
        assert _cron_project_crew_alias(cfg, "", "default-member", "") == "default-member"

    def test_a_member_bound_job_keeps_its_member_whatever_the_source(self):
        from kiro_crew.slack.gateway import _cron_project_crew_alias

        cfg = MagicMock(agents={"Development Member": MagicMock()})
        assert (
            _cron_project_crew_alias(cfg, "Development Member", "someone-else", "materialized")
            == "Development Member"
        )


class TestCronProjectAliasTemplate:
    """A project-bound alias hit dispatches the alias's provider template.

    A template-kind carrier records the job's ``agent_id`` as ``template_id``, so
    for an alias the resolver hands back the ALIAS name, which kiro-cli rejects as
    an unavailable mode before the prompt runs. The unbound and sequence paths
    already dispatch the alias's ``kiro_agent``.
    """

    @staticmethod
    def _bindings(source="alias", kind="template", kiro_agent="in-3d", alias="in-3d"):
        return MagicMock(
            resolved_source=source,
            resolved_alias=alias,
            kiro_agent=kiro_agent,
            execution_context=MagicMock(selection_kind=kind),
        )

    def test_an_alias_hit_on_a_template_carrier_dispatches_the_alias_template(self):
        from kiro_crew.slack.gateway import _cron_project_alias_template

        cfg = MagicMock(agents={"in-3d": MagicMock(kiro_agent="kiro-dev")})
        with patch("kiro_crew.slack.gateway.dispatch_kiro_agent", side_effect=lambda n: n):
            assert _cron_project_alias_template(cfg, self._bindings()) == "kiro-dev"

    def test_the_template_goes_through_the_declared_name_mapping(self):
        from kiro_crew.slack.gateway import _cron_project_alias_template

        cfg = MagicMock(agents={"in-3d": MagicMock(kiro_agent="KiroPkg-captain")})
        with patch(
            "kiro_crew.slack.gateway.dispatch_kiro_agent",
            side_effect=lambda n: "captain" if n == "KiroPkg-captain" else n,
        ):
            assert _cron_project_alias_template(cfg, self._bindings()) == "captain"

    @pytest.mark.parametrize(
        "bindings",
        [
            # A member carrier is already mapped by the resolver.
            {"kind": "member", "kiro_agent": "kiro-dev"},
            # A project file or materialized template is dispatched as resolved.
            {"source": "project", "kiro_agent": "repo-agent", "alias": "default-member"},
            {"source": "materialized", "kiro_agent": "app-agent", "alias": "default-member"},
            # A carrier that already names a real template keeps it.
            {"kiro_agent": "kirocrew-lite"},
        ],
    )
    def test_every_other_answer_keeps_the_resolver_template(self, bindings):
        from kiro_crew.slack.gateway import _cron_project_alias_template

        cfg = MagicMock(agents={"in-3d": MagicMock(kiro_agent="kiro-dev")})
        b = self._bindings(**bindings)
        assert _cron_project_alias_template(cfg, b) == b.kiro_agent

    def test_no_carrier_keeps_the_resolver_template(self):
        from kiro_crew.slack.gateway import _cron_project_alias_template

        cfg = MagicMock(agents={"in-3d": MagicMock(kiro_agent="kiro-dev")})
        b = self._bindings(kiro_agent="kiro-dev")
        b.execution_context = None
        assert _cron_project_alias_template(cfg, b) == "kiro-dev"

    @pytest.mark.asyncio
    async def test_a_bound_fire_dispatches_the_template_and_keeps_the_alias_crew(self, tmp_path):
        gw = _make_gw_for_llm()
        job = _make_llm_job(project_path=str(tmp_path), agent_id="in-3d")
        cfg = MagicMock(agents={"in-3d": MagicMock(kiro_agent="kiro-dev")})
        with (
            patch("kiro_crew.slack.gateway.resolve_agent_bindings") as mock_resolve,
            patch("kiro_crew.slack.gateway.warm_project_agent_names", AsyncMock()),
            patch("kiro_crew.slack.gateway.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.gateway.dispatch_kiro_agent", side_effect=lambda n: n),
            patch(
                "kiro_crew.slack.gateway._cron_crew_alias",
                side_effect=lambda _c, _m, alias: alias,
            ),
        ):
            r = mock_resolve.return_value
            r.requested_resolved = True
            r.kiro_agent = "in-3d"
            r.model = ""
            r.resolved_alias = "in-3d"
            r.resolved_source = "alias"
            r.memory_store_name = ""
            r.execution_context = None
            with patch(
                "kiro_crew.slack.gateway._cron_project_alias_template",
                wraps=lambda c, b: "kiro-dev",
            ) as helper:
                await _run_llm_callback(gw, job)
        assert helper.called
        kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert kwargs["agent"] == "kiro-dev"
        assert kwargs["crew_agent"] == "in-3d"
