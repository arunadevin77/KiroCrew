"""A declined settings-seed share surfaces a notice in the session transcript.

When ``_write_claude_local_settings`` cannot write its seed — because a sibling
session governs settings.local.json with different settings (model or permissions
differ) — the session runs without Crew's MCP tools and the user has no
explanation.  These tests pin two things:

1. ``AcpClient._log_declined_share`` sets ``_settings_seed_declined = True``
   so the flag is always True exactly when and only when the seed was declined.

2. ``chat_runner._notice_seed_declined`` emits one ``notice`` row when that flag
   is set, and emits nothing when it is not.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_acp_seed_provenance import (  # noqa: F401 -- autouse fixture
    _OWNER,
    isolated_records,
)

from kiro_crew import model_registry as mr
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.dashboard import chat_runner
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

# Two distinct advertised model lists the tests toggle between.
_MODEL_A = "global.anthropic.claude-opus-5[1m]"
_MODEL_B = "global.anthropic.claude-opus-4-8[1m]"
_SERVED_A = [_MODEL_A]
_SERVED_B = [_MODEL_A, _MODEL_B]


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the kiro-cli version so the spec permissions gate does not shell out."""
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _client(tmp_path: Path, **kw: object) -> AcpClient:
    return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)


# ── part 1: flag is set when the seed is declined ────────────────────────────


class TestSettingsSeedDeclinedFlag:
    """``_settings_seed_declined`` is False on a fresh client and True after a decline."""

    def test_fresh_client_has_flag_false(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        assert client._settings_seed_declined is False

    def test_flag_stays_false_when_seed_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        client = _client(tmp_path)
        client._write_claude_local_settings()
        assert client._settings_seed_declined is False

    def test_flag_stays_false_when_sibling_shares_identical_seed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        owner = _client(tmp_path)
        owner._write_claude_local_settings()

        sibling = _client(tmp_path)
        sibling._write_claude_local_settings()

        assert sibling._settings_seed_declined is False

    def test_flag_is_true_when_sibling_seed_is_declined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Owner seeds with model A only; sibling needs model B (not in the list).
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        owner = _client(tmp_path, model=_MODEL_A)
        owner._write_claude_local_settings()

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_B})
        sibling = _client(tmp_path, model=_MODEL_B)
        sibling._write_claude_local_settings()

        # The share was declined because _MODEL_B is not in the owner's
        # availableModels list (only _MODEL_A is there).
        assert sibling._settings_seed_declined is True

    def test_flag_is_true_when_permissions_differ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED_A})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()

        sibling = _client(tmp_path, permission_mode="acceptEdits")
        sibling._write_claude_local_settings()

        assert sibling._settings_seed_declined is True


# ── part 2: notice row is emitted iff the flag is set ────────────────────────


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect every notice text appended via ``append_and_surface``."""
    seen: list[str] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **kw: seen.append(text),
    )
    return seen


def _run_notice(client: object) -> None:
    """Drive ``_notice_seed_declined`` synchronously."""
    asyncio.run(chat_runner._notice_seed_declined(SimpleNamespace(), SimpleNamespace(), client))


class TestNoticeEmitted:
    """``_notice_seed_declined`` emits a notice row iff the flag is set."""

    def test_no_notice_when_flag_is_false(self, notices: list[str]) -> None:
        client = SimpleNamespace(settings_seed_declined=False)
        _run_notice(client)
        assert notices == []

    def test_no_notice_when_attribute_is_absent(self, notices: list[str]) -> None:
        """Best-effort: a client with no such attribute emits nothing."""
        _run_notice(SimpleNamespace())
        assert notices == []

    def test_notice_emitted_when_flag_is_true(self, notices: list[str]) -> None:
        client = SimpleNamespace(settings_seed_declined=True)
        _run_notice(client)
        assert len(notices) == 1

    def test_notice_mentions_mcp_tools(self, notices: list[str]) -> None:
        client = SimpleNamespace(settings_seed_declined=True)
        _run_notice(client)
        assert "MCP tools" in notices[0]

    def test_notice_names_the_cause(self, notices: list[str]) -> None:
        """The notice must say why tools are missing, not just that they are."""
        client = SimpleNamespace(settings_seed_declined=True)
        _run_notice(client)
        # Either the word "sibling" or "settings" must appear so the user has
        # a clue about the source of the conflict.
        assert "sibling" in notices[0] or "settings" in notices[0]

    def test_notice_names_an_action(self, notices: list[str]) -> None:
        """The notice must say what to do."""
        client = SimpleNamespace(settings_seed_declined=True)
        _run_notice(client)
        # A notice without any actionable verb is not useful.
        assert any(verb in notices[0].lower() for verb in ("start", "close", "open", "restart"))

    def test_notice_is_emitted_only_once_per_call(self, notices: list[str]) -> None:
        """The function is not idempotent (its guard is the session-start is_new
        flag in chat_runner); calling it twice emits twice.  The session-start
        path is responsible for calling it exactly once.  This test confirms the
        function itself does not swallow calls."""
        client = SimpleNamespace(settings_seed_declined=True)
        _run_notice(client)
        _run_notice(client)
        assert len(notices) == 2
