from __future__ import annotations

import pytest
from test_chat_session_not_found_recovery import _assistant, _client, _drain, _errors, _state, _wire

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.transport_errors import _raise_acp_error
from kiro_crew.llm_helpers import (
    CREDENTIAL_RELOAD_GIVE_UP_TEXT,
    CREDENTIAL_RELOAD_RETRY_NOTICE,
    acp_error_is_session_not_found,
    acp_error_needs_fresh_backend,
)

_CREDENTIAL_MESSAGE = (
    "Internal error: API Error: Could not load AWS credentials · Could not load credentials "
    "from any providers. Check or refresh your AWS credentials and try again."
)


def _raised(frame: dict, backend: str = "claude") -> AcpError:
    with pytest.raises(AcpError) as info:
        _raise_acp_error(frame, backend=backend)
    return info.value


def credential_error() -> AcpError:
    return _raised(
        {
            "code": -32603,
            "message": _CREDENTIAL_MESSAGE,
            "data": {"errorKind": "cloud_credential_error"},
        }
    )


@pytest.mark.parametrize("backend", ["claude", ""])
def test_the_structured_cloud_credential_error_kind_is_tagged(backend):
    exc = _raised(
        {
            "code": -32603,
            "message": _CREDENTIAL_MESSAGE,
            "data": {"errorKind": "cloud_credential_error"},
        },
        backend=backend,
    )

    assert exc.credential_reload is True
    assert exc.transient is False
    assert exc.auth_required is False


@pytest.mark.parametrize(
    "frame",
    [
        {"code": -32603, "message": _CREDENTIAL_MESSAGE, "data": {"errorKind": "unknown"}},
        {"code": -32603, "message": _CREDENTIAL_MESSAGE},
        {
            "code": -32603,
            "message": "Internal error",
            "data": "{'errorKind': 'cloud_credential_error'}",
        },
        {"code": -32603, "message": "errorKind cloud_credential_error", "data": {}},
    ],
    ids=["other-kind", "no-data", "kind-only-in-text", "kind-only-in-message"],
)
def test_only_the_structured_error_kind_is_tagged(frame):
    assert _raised(frame).credential_reload is False


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (credential_error(), True),
        (AcpError(f"Could not reconnect the agent's session. {credential_error()}"), True),
        (AcpError("Session not found: 3f2a"), True),
        (AcpError("Internal error: API Error: Internal server error"), False),
        (RuntimeError("cloud_credential_error"), False),
    ],
    ids=[
        "cloud-credential",
        "re-wrapped-cloud-credential",
        "session-not-found",
        "other-acp-error",
        "not-an-acp-error",
    ],
)
def test_a_fresh_backend_is_needed_for_a_lost_session_or_stale_credentials(exc, expected):
    assert acp_error_needs_fresh_backend(exc) is expected


def test_a_credential_error_is_not_reported_as_a_lost_session():
    assert acp_error_is_session_not_found(credential_error()) is False


@pytest.mark.asyncio
async def test_dashboard_chat_recycles_the_backend_once_and_lands(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    calls: list[str] = []

    async def _stream(msg):
        calls.append(msg)
        if len(calls) == 1:
            raise credential_error()
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="answered on a fresh backend")
        yield LLMEvent(kind=EVENT_COMPLETE)

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_stream))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    await _run_chat(state, slot, "check the PR")
    await _drain(state)

    assert calls == ["check the PR", "check the PR"]
    state.sessions.reset.assert_awaited_once()
    state.sessions.discard_conversation.assert_not_awaited()
    assert CREDENTIAL_RELOAD_RETRY_NOTICE in _errors(slot)
    assert any("answered on a fresh backend" in t for t in _assistant(slot))


@pytest.mark.asyncio
async def test_dashboard_chat_stops_after_a_second_credential_failure(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat import _run_chat

    calls: list[str] = []

    async def _fail(msg):
        calls.append(msg)
        raise credential_error()
        yield  # pragma: no cover

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_fail))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    await _run_chat(state, slot, "check the PR")
    await _drain(state)

    assert len(calls) == 2
    errors = _errors(slot)
    assert errors.count(CREDENTIAL_RELOAD_RETRY_NOTICE) == 1
    assert errors[-1] == f"❌ {CREDENTIAL_RELOAD_GIVE_UP_TEXT}"
    assert slot._queue == []
