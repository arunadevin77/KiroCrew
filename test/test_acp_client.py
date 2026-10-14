"""Tests for ACP client."""

import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _stop_stderr_drain  # noqa: F401 - re-exported
from acp_client_helpers import _POSIX_EXEC_PATHS_ONLY
from spawn_test_helpers import strip_spawn_shim

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import (
    _DRAIN_DURATION,
    _DRAIN_IDLE_EXIT,
    AcpClient,
    AcpError,
    _format_acp_error,
    _is_model_substitution_advisory,
    _substitute_model_from_advisory,
)
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, JSONRPC_METHOD_NOT_FOUND, AcpPromptStats
from kiro_crew.agent_sdk import host_auth


def _scripted_process(lines, *, returncode=None):
    """Build a mock subprocess whose stdout.readline yields *lines* in order.

    Each entry of *lines* is a dict (serialized to a JSON-RPC line) or a raw
    bytes value. After the list is exhausted, readline blocks-then-returns an
    empty timeout-friendly value so _read_message's wait_for sees nothing new.
    """
    queue = deque(lines)

    async def _readline():
        if queue:
            item = queue.popleft()
            if isinstance(item, (bytes, bytearray)):
                return bytes(item)
            return (json.dumps(item) + "\n").encode()
        # Nothing left — emulate a quiet stream (no more frames this read).
        await asyncio.sleep(0)
        return b""

    proc = MagicMock()
    stdout = AsyncMock()
    stdout.readline = AsyncMock(side_effect=_readline)
    proc.stdout = stdout
    proc.returncode = returncode
    # stdin used by _send_error / _send_response.
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin
    return proc


class TestWaitForResponseDeferral:
    """F1: _wait_for_response must not spin on inbound server requests or
    foreign-id responses, must not drop them, and must re-inject them."""

    @pytest.mark.asyncio
    async def test_inbound_permission_request_deferred_not_spun(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        # Awaiting response for req_id=7. A server->client permission request
        # arrives first carrying id=7 (colliding namespace), THEN the real
        # response for req 7 arrives.
        perm = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "options": [], "toolCall": {"title": "x"}},
        }
        resp = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        client._process = _scripted_process([perm, resp])

        result = await asyncio.wait_for(client._wait_for_response(7, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        # Permission request must be re-injected (not dropped, not spun).
        assert len(client._buffer) == 1
        buffered = client._buffer.popleft()
        assert buffered.method == "session/request_permission"
        assert buffered.id == 7

    @pytest.mark.asyncio
    async def test_foreign_id_response_preserved(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        foreign = {"jsonrpc": "2.0", "id": 99, "result": {"stale": True}}
        resp = {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        client._process = _scripted_process([foreign, resp])

        result = await asyncio.wait_for(client._wait_for_response(3, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        assert len(client._buffer) == 1
        assert client._buffer.popleft().id == 99

    @pytest.mark.asyncio
    async def test_notification_goes_to_mcp_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"ok": True}}
        client._process = _scripted_process([notif, resp])

        result = await asyncio.wait_for(client._wait_for_response(5, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        # Notification buffered for drain, NOT re-injected into _buffer.
        assert len(client._buffer) == 0
        assert len(client._mcp_notifications) == 1
        assert client._mcp_notifications[0].method == "session/update"

    @pytest.mark.asyncio
    async def test_deferred_reinjected_in_order_on_timeout(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        first = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {},
        }
        second = {"jsonrpc": "2.0", "id": 88, "result": {"other": True}}
        # No matching response ever arrives -> timeout. Deferred frames must
        # still be re-injected in arrival order.
        client._process = _scripted_process([first, second])

        with pytest.raises(AcpError):
            await asyncio.wait_for(client._wait_for_response(7, timeout=1.0), timeout=10.0)
        assert len(client._buffer) == 2
        m0 = client._buffer.popleft()
        m1 = client._buffer.popleft()
        assert m0.method == "session/request_permission"
        assert m1.id == 88

    def test_session_timeout_progress_names_missing_failed_and_oauth_servers(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            ),
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_init_failure",
                params={
                    "serverName": "broken",
                    "error": "aws_secret_access_key=supersecret connection failed",
                },
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"}),
        ]

        progress = client._mcp_timeout_progress(
            [{"name": "ready"}, {"name": "broken"}, {"name": "silent"}]
        )

        assert "2/3 session-injected MCP server(s) reported" in progress
        assert "no report from silent" in progress
        assert "failed: broken" in progress
        assert "supersecret" not in progress
        assert "awaiting authorization: oauth" in progress

    def test_session_timeout_progress_empty_roster_keeps_reports(self, tmp_path):
        """An empty ``mcpServers`` roster must not drop the staged reports.

        Mirrors ``AcpRuntime._mcp_init_progress``: with nothing to attribute
        against, the count is reported as ``roster unknown`` instead of the
        timeout reading as a bare budget.
        """
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            ),
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_init_failure",
                params={"serverName": "broken", "error": "connection failed"},
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"}),
        ]

        for expected in ([], None):
            progress = client._mcp_timeout_progress(expected)
            assert "2 MCP server report(s), roster unknown" in progress
            assert "session-injected" not in progress
            assert "failed: broken (connection failed)" in progress
            assert "awaiting authorization: oauth" in progress

    def test_session_timeout_progress_ignores_prior_attempt_frames(self, tmp_path):
        """A timed-out ``session/load``'s frames are not this attempt's progress.

        ``_drain_notifications`` clears the buffer only at the end of
        ``_initialize_session``, so the load -> ``session/new`` fallback still
        holds the previous attempt's ``server_initialized`` frames. The progress
        summary must honour the per-attempt floor ``_begin_session_report`` sets,
        or it asserts the stall is not in those servers on stale evidence.
        """
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        roster = [{"name": "ready"}]
        # Attempt 1 (session/load): the server reported and another asked for
        # authorization, then the load timed out.
        client._begin_session_report(roster)
        client._mcp_notifications.append(
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            )
        )
        client._mcp_notifications.append(
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"})
        )
        # Attempt 2 (session/new fallback): nothing has reported yet.
        client._begin_session_report(roster)

        progress = client._mcp_timeout_progress(roster)

        assert "0/1 session-injected MCP server(s) reported" in progress
        assert "no report from ready" in progress
        assert "not in those servers" not in progress
        # The authorization the failed attempt asked for is still owed by the
        # user, so it is not floored (same rule as _drain_notifications).
        assert "awaiting authorization: oauth" in progress

        # A frame that arrives during attempt 2 is counted.
        client._mcp_notifications.append(
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            )
        )
        progress = client._mcp_timeout_progress(roster)
        assert "1/1 session-injected MCP server(s) reported" in progress


class TestWaitForResponseActivityDeadline:
    """Low-A: a steady stream of notifications keeps _wait_for_response alive
    past the base timeout until the real response arrives."""

    @pytest.mark.asyncio
    async def test_streaming_notifications_extend_deadline(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)

        # Virtual clock so the test is fast and deterministic. Each readline
        # advances time by 0.05s; base timeout is 0.1s. Without activity-based
        # extension the call would die after ~2 reads; with it, the stream of
        # notifications keeps pushing the deadline out until the response lands.
        clock = {"t": 1000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        frames = [notif] * 20 + [{"jsonrpc": "2.0", "id": 4, "result": {"loaded": True}}]
        queue = deque(frames)

        async def _readline():
            if queue:
                clock["t"] += 0.05  # each frame advances the virtual clock
                return (json.dumps(queue.popleft()) + "\n").encode()
            return b""

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        result = await asyncio.wait_for(client._wait_for_response(4, timeout=0.1), timeout=10.0)
        assert result == {"loaded": True}
        # All 20 notifications were drained while the deadline kept extending.
        assert len(client._mcp_notifications) == 20

    @pytest.mark.asyncio
    async def test_hard_cap_eventually_times_out(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)
        clock = {"t": 5000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}

        async def _readline():
            # Endless notifications, large time jumps — never a matching
            # response. The absolute hard cap must eventually fire.
            clock["t"] += 30.0
            return (json.dumps(notif) + "\n").encode()

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        with pytest.raises(AcpError):
            await asyncio.wait_for(client._wait_for_response(1, timeout=0.1), timeout=10.0)


class TestProcessMessageUnknownServerRequest:
    """F5: unknown server->client requests are classified for a -32601 reply,
    not silently skipped."""

    def test_unknown_server_request_classified(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=12, method="fs/read_text_file", params={"path": "/x"})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_terminal_create_classified(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=3, method="terminal/create", params={})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_notification_still_skipped(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        # Unknown notification (method, NO id) is not a request -> skip.
        msg = JsonRpcMessage(method="some/unknown_notification", params={})
        assert client._process_message(msg, req_id=1) == "skip"

    def test_known_permission_request_not_unknown(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=2, method="session/request_permission", params={})
        assert client._process_message(msg, req_id=1) == "permission"

    @pytest.mark.asyncio
    async def test_reject_sends_method_not_found_error(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=42, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)

        client._process.stdin.write.assert_called_once()
        written = client._process.stdin.write.call_args[0][0].decode()
        payload = json.loads(written)
        assert payload["id"] == 42
        assert payload["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        assert "terminal/create" in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_reject_noop_when_no_id(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=None, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)
        client._process.stdin.write.assert_not_called()


class TestFormatAcpError:
    """Tests for _format_acp_error — Bedrock-aware error rewriting.

    Covers the bug filed at task 86089e43: ACP backend errors used
    to be surfaced as the raw JSON-RPC dict (`Prompt error: {'code': -32603,
    ...}`), which dead-ends users when the picker can't expose a valid
    alternative. The helper rewrites known Bedrock failures into actionable
    text while preserving the request_id for support correlation, and scrubs
    embedded credentials / exfiltration URLs as defense-in-depth.
    """

    def test_non_dict_falls_back(self):
        assert _format_acp_error(None) == "Prompt error: None"
        assert _format_acp_error("boom") == "Prompt error: boom"

    def test_unknown_dict_preserves_raw(self):
        """An unrecognised shape must lose no information.

        The fallback now leads with the provider's own text rather than a repr
        of the JSON-RPC dict, but both fields still have to survive: the
        provider detail AND the non-boilerplate JSON-RPC message, which for
        codes other than -32603 is the only summary the error carries.
        """
        err = {"code": -32000, "message": "Something else", "data": "weird"}
        out = _format_acp_error(err)
        assert "Something else" in out
        assert "weird" in out
        # No dict repr: punctuation soup is not a user-facing error message.
        assert "Prompt error: {" not in out

    def test_model_not_available_rewrite(self):
        # With NO advertised list the caller cannot know whether this is an
        # entitlement or a capacity failure, so it keeps the capacity wording.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: The model 'opus' "
                "is not available. Please use '/model' to select a different "
                "model and try again. (request_id: 3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e)"
            ),
        }
        out = _format_acp_error(err)
        assert "unavailable on the backend" in out
        assert "'opus'" in out
        assert "model picker" in out
        # Must NOT point at ~/.claude/settings.json — KiroCrew never reads it;
        # the model lives in config.json / the agent spec.
        assert "settings.json" not in out
        assert "config.json" in out
        # Nor echo the provider's own "use '/model'" advice: that is a kiro-cli
        # TUI command, inert in the dashboard, Slack, and cron.
        assert "/model" not in out
        # Request id is preserved for support correlation.
        assert "3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e" in out
        # Should not leak the raw dict prefix when we have a real rewrite.
        assert "Prompt error: {" not in out

    def test_unentitled_model_names_what_the_account_can_use(self):
        """The free-tier case: say it is an access problem and list the options.

        Upstream sends the SAME string for entitlement and capacity failures,
        so the advertised list is the only thing that distinguishes them.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'opus-4.8-1m' is not available. (request_id: abc-123)",
        }
        out = _format_acp_error(err, ["claude-sonnet-4-6", "claude-haiku-4-5"])
        assert "does not have access" in out
        assert "'opus-4.8-1m'" in out
        # The actionable part: what they CAN pick.
        assert "claude-sonnet-4-6" in out
        assert "claude-haiku-4-5" in out
        # Must NOT blame capacity or tell them to wait — no retry will help.
        assert "capacity" not in out
        assert "wait a minute" not in out
        assert "abc-123" in out

    def test_advertised_model_still_reads_as_capacity(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The model IS on offer, so a rejection really is a transient blip and
        # must keep the capacity wording (and stay retryable).
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        out = _format_acp_error(err, ["opus", "sonnet"])
        assert "unavailable on the backend" in out
        assert "does not have access" not in out
        assert _is_transient_raw_error(err, ["opus", "sonnet"]) is True

    def test_unentitled_model_is_terminal_not_retried(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The retry verdict must move with the wording -- retrying a model the
        # account was never offered just reproduces the same rejection.
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        assert _is_transient_raw_error(err, ["sonnet", "haiku"]) is False
        # Unknown entitlement -> unchanged (transient), never a false accusation.
        assert _is_transient_raw_error(err, None) is True
        assert _is_transient_raw_error(err, []) is True

    def test_unentitled_match_is_case_insensitive(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # An entitled-but-differently-cased model must not be called unentitled.
        err = {"code": -32603, "message": "x", "data": "The model 'Opus' is not available."}
        assert _is_transient_raw_error(err, ["opus"]) is True
        assert "does not have access" not in _format_acp_error(err, ["opus"])

    def test_long_available_list_is_capped(self):
        err = {"code": -32603, "message": "x", "data": "The model 'nope' is not available."}
        out = _format_acp_error(err, [f"model-{i}" for i in range(12)])
        assert "+4 more" in out
        assert "model-7" in out
        assert "model-8" not in out

    def test_throttling_exception_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ThrottlingException: Too many requests "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "wait" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    def test_too_many_requests_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "TooManyRequestsException: rate limited"}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_access_denied_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDeniedException: not authorized to invoke",
        }
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()
        assert "aws sso login" in out.lower()

    def test_expired_token_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "ExpiredToken: signature expired"}
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()

    def test_missing_request_id_omits_suffix(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'sonnet' is not available.",
        }
        out = _format_acp_error(err)
        assert "request_id" not in out
        assert "'sonnet'" in out

    def test_throttle_keyword_in_message(self):
        # Some backends put the trigger word in `message` rather than `data`.
        err = {"code": -32603, "message": "Rate limit exceeded", "data": ""}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_credentials_in_data_are_redacted(self):
        """AWS access keys embedded in upstream errors must not leak to the UI.

        Recognized error patterns (auth/throttle/model-unavailable) already drop
        the `data` field when constructing the rewritten message, so the secret
        is gone simply by virtue of the rewrite. The redaction layer is the
        defense-in-depth fallback for the unknown-shape path; this test pins
        the absence guarantee on the recognized-pattern path.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDenied: AKIAIOSFODNN7EXAMPLE not authorized",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_credentials_in_unknown_dict_fallback_are_redacted(self):
        """The fallback path echoes raw dict — must still scrub secrets."""
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # The scrubbed placeholder proves redaction ran on this path.
        assert "REDACTED" in out
        # The surrounding provider text still reaches the user.
        assert "leak:" in out

    def test_exfiltration_url_in_unknown_dict_fallback_is_redacted(self):
        """The fallback path echoes raw dict — exfil URLs must also be scrubbed.

        Pairs with `test_credentials_in_unknown_dict_fallback_are_redacted` to
        cover the second redaction layer (`redact_exfiltration_urls`).
        """
        # Use a URL whose query carries a base64-blob — matches _EXFIL_PATTERNS
        # and is what real provider error payloads tend to look like when they
        # echo signed callback URLs.
        leaked_blob = "QUtJQUlPU0ZPRE5ON0VYQU1QTEVTRUNSRVRBQ0NFU1NLRVk" + "A" * 30
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": f"callback to https://attacker.example.com/exfil?token={leaked_blob}",
        }
        out = _format_acp_error(err)
        assert leaked_blob not in out, "leaked credential blob must be redacted"
        assert "REDACTED" in out

    def test_sensitive_content_emits_log_warning(self, caplog):
        """When the redaction layer scrubs anything, a warning MUST be logged.

        Silent scrubbing hides upstream-leak signals from security review;
        the warning lets operators notice that a provider echoed sensitive
        content back. The warning intentionally includes only counts — never
        the redacted values.
        """
        import logging

        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            _format_acp_error(err)

        warnings = [r for r in caplog.records if "sensitive content" in r.getMessage()]
        assert warnings, "expected a redaction warning to be logged"
        assert "AKIAIOSFODNN7EXAMPLE" not in warnings[0].getMessage()

    def test_internal_server_error_rewrite(self):
        """A transient 5xx from the backend must

        classify as a momentary backend error rather than dumping the raw
        -32603 JSON-RPC dict at the user.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "CodewhispererChatResponseStream(ServiceError(ServiceError "
                '{ name: "InternalServerError", ... please try again ... })) '
                "(request_id: 91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f)"
            ),
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "retry in a moment" in out.lower()
        assert "5xx" in out
        # Request id preserved for support correlation.
        assert "91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f" in out
        # Must not dead-end the user with the raw dict.
        assert "Prompt error: {" not in out

    def test_please_try_again_phrasing_rewrite(self):
        """A bare 'please try again' transient hint (no explicit 5xx token)

        still classifies as transient.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Service hiccup, please try again.",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "Prompt error: {" not in out

    def test_dispatch_failure_rewrite(self):
        """DispatchFailure / connection-reset shapes are transient too."""
        err = {
            "code": -32603,
            "message": "x",
            "data": "DispatchFailure: ConnectionReset (request_id: bbbb2222-cccc-3333-dddd-444455556666)",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "bbbb2222-cccc-3333-dddd-444455556666" in out

    def test_transient_5xx_missing_request_id_omits_suffix(self):
        err = {"code": -32603, "message": "Internal error", "data": "InternalServerError occurred"}
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "request_id" not in out

    def test_transient_branch_does_not_leak_credentials(self):
        """Defense-in-depth on the new transient path: an AWS key embedded in

        the upstream `data` must never reach the UI. The rewrite drops the
        `data` field (so the secret is gone by construction), and the function
        still routes the result through redact_credentials/redact_exfiltration_urls
        at the end — the new branch adds no early return that bypasses it.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "InternalServerError: leaked AKIAIOSFODNN7EXAMPLE please try again",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "transient error" in out.lower()

    def test_throttle_precedence_over_transient(self):
        """A throttle that also says 'please try again' stays a throttle —

        throttling precedence must survive the new transient branch.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ThrottlingException: slow down, please try again",
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "transient error" not in out.lower()

    def test_non_transient_minus_32603_falls_through_to_fallback(self):
        """A non-transient -32603 whose only 'internal'-ish signal is the

        canonical JSON-RPC message 'Internal error' must NOT be misclassified
        as transient. The transient branch keys off the provider `data` field
        only (never the generic `message`), so a deterministic failure like a
        ValidationException surfaces its real cause instead.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        # The real cause is shown verbatim — stronger than the old assertion,
        # which only required a dict repr containing it somewhere.
        assert "ValidationException: input contains an unsupported field 'foo'" in out
        # The -32603 boilerplate message is dropped as noise next to that.
        assert "Internal error" not in out

    def test_bare_500_token_is_not_treated_as_5xx(self):
        """A standalone numeric (e.g. a token-limit value) must not match the

        5xx branch — the numeric match is anchored to an HTTP/status context,
        not a bare 500/502/503 token.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "max_tokens 500 exceeds the model limit of 200000",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "max_tokens 500 exceeds the model limit of 200000" in out

    def test_http_500_status_context_still_classifies_transient(self):
        """An HTTP/status-anchored 50x token IS a genuine transient signal."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "upstream returned HTTP 503 (request_id: aaaa1111-bbbb-2222-cccc-333344445555)",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    def test_kiro_generic_generation_failure_formats_as_transient(self):
        """kiro-cli's pre-stream generation failure wrapper gets the friendly
        retry guidance instead of the raw-dict fallback: data carried no
        request_id and no error class, only the generic wrapper string.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Kiro failed to generate a response",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "Prompt error: {" not in out

    def test_generation_failure_phrase_in_message_only_is_not_transient(self):
        """The generation-failure pattern is scoped to `data` — the phrase
        appearing only in the JSON-RPC `message` field must not trigger the
        friendly transient branch (mirrors the model-unavailable scoping)."""
        err = {
            "code": -32603,
            "message": "Kiro failed to generate a response",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "ValidationException: input contains an unsupported field 'foo'" in out

    def test_kiro_process_failure_formats_as_transient_and_keeps_request_id(self):
        """kiro-cli's post-stream sibling wrapper ("The service failed to
        process the request (request_id: ...)") gets the same retry guidance
        as the generation-failure branch, and the request_id survives the
        rewrite so a support thread can quote it.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "The service failed to process the request "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out
        assert "Prompt error: {" not in out
        # The string fallback in llm_helpers classifies both the raw wrapper
        # and the rewrite, so a path that lost the structured flag still retries.
        from kiro_crew.llm_helpers import is_transient_backend_error

        assert is_transient_backend_error(str(err["data"])) is True
        assert is_transient_backend_error(out) is True

    def test_process_failure_phrase_in_message_only_is_not_transient(self):
        """Scoped to `data` like its sibling: the phrase in the JSON-RPC
        `message` alone must not flip a deterministic failure to transient."""
        err = {
            "code": -32603,
            "message": "The service failed to process the request",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "ValidationException: input contains an unsupported field 'foo'" in out

    def test_session_expired_rewrite(self):
        """An expired session gets actionable sign-in guidance rather than the
        misleading transient-5xx retry advice.

        The sign-in half is the failing harness's own declared remedy, so this
        asserts it is rendered verbatim rather than pinning the wording here: a
        second copy of that wording is what let the message name `kiro-cli login`
        to an operator on a harness that signs in some other way.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "DispatchFailure: session expired",
        }
        out = _format_acp_error(err)
        # The signed-out MESSAGE, not the remedy. This arm reaches the text on real
        # evidence (a 401/403, or prose saying the session expired), so it may assert
        # the state; the remedy is the register for a caveat rendered having measured
        # nothing, and it deliberately makes no state claim.
        assert host_auth.signed_out_message("") in out
        assert "kiro-cli login" in out.lower()
        assert "retry" in out.lower() and "will not help" in out.lower()
        # Must NOT show the misleading 5xx message.
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_session_expired_names_the_failing_harness(self):
        """A harness that signs in elsewhere gets ITS message, not kiro-cli's.

        The regression this guards: the arm hardcoded `kiro-cli login`, so an
        expiry on a harness with its own credential file told the operator to run
        a command that could not fix it. The retry verdict is this arm's own
        finding about the error, so it must survive on every backend.

        Reads ``signed_out_message`` and not ``sign_in_remedy``, and the
        difference is the point: this arm HAS evidence -- a 401, or prose saying
        the session expired -- so it may assert the state. The remedy is the
        register for the places that render a standing line having measured
        nothing, and using it here would drop the diagnosis.
        """
        err = {"code": -32603, "message": "Internal error", "data": "HTTP 401"}
        for backend in ("claude", "codex"):
            out = _format_acp_error(err, backend=backend)
            assert host_auth.signed_out_message(backend) in out
            assert "kiro-cli login" not in out.lower(), backend
            assert "will not help" in out.lower(), backend

    def test_session_expired_by_http_status(self):
        """A bare 401/403 is the shape an expired session actually arrives in:
        the rejection carries no explanatory wording, so status alone must
        drive the classification."""
        for status in ("HTTP 401", "HTTP 403", "status code 401", "status 403"):
            err = {"code": -32603, "message": "Internal error", "data": status}
            out = _format_acp_error(err)
            assert "kiro-cli login" in out.lower(), f"No sign-in guidance for: {status!r}"
            assert "transient error" not in out.lower(), f"Misclassified: {status!r}"

    def test_session_expired_401_with_transport_error(self):
        """The reported failure mode: an aborted request leaves a transport
        error alongside the 401, and the 5xx family must not win and tell the
        user to retry."""
        err = {
            "code": -32603,
            "message": "Encountered an error in the response stream",
            "data": "DispatchFailure ConnectionResetError: HTTP 401",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_invalid_bearer_token_rewrite(self):
        """The account-switch rejection: a credential the running child still
        holds is rejected as invalid, with no status code and no expiry wording,
        so it must still reach the sign-in guidance instead of the raw string."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The bearer token included in the request is invalid.",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "retry" in out.lower() and "will not help" in out.lower()
        # Must NOT show the misleading transient-5xx advice.
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_invalid_bearer_token_wins_over_transport_error(self):
        """An aborted request leaves a transport error beside the rejection; the
        credential branch is checked first so the 5xx family cannot win."""
        err = {
            "code": -32603,
            "message": "Encountered an error in the response stream",
            "data": "DispatchFailure ConnectionResetError: the bearer token is invalid",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "transient error" not in out.lower()

    def test_unrelated_invalid_does_not_read_as_credential_failure(self):
        """The fenced gap must not let an unrelated 'invalid' in a combined
        haystack turn a validation fault into a sign-in prompt."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ValidationException: refreshed the bearer token successfully. "
                "Field 'temperature' is invalid"
            ),
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" not in out.lower()

    def test_genuine_5xx_still_transient_with_auth_absent(self):
        """The new auth-status branch must not swallow real 5xx errors."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ServiceUnavailableException: HTTP 503",
        }
        out = _format_acp_error(err)
        assert "transient" in out.lower()
        assert "kiro-cli login" not in out.lower()

    def test_session_expired_variants(self):
        """Various kiro-cli session-expiry error shapes are all classified."""
        variants = [
            "not logged in",
            "session has expired",
            "login expired",
            "authentication required",
            "session timed out",
            "not authenticated",
            "login required",
        ]
        for text in variants:
            err = {"code": -32603, "message": "Internal error", "data": text}
            out = _format_acp_error(err)
            assert "transient error" not in out.lower(), f"Failed for: {text!r}"
            assert "kiro-cli login" in out.lower(), f"No login guidance for: {text!r}"

    def test_session_expired_with_5xx_token_wins(self):
        """Session expiry checked before 5xx: a DispatchFailure wrapping a
        session-expired message must surface as auth, not transient."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "DispatchFailure ConnectionResetError: session expired",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "kiro-cli login" in out.lower()


class TestIsTransientRawError:
    """_is_transient_raw_error classifies retryability from the RAW JSON-RPC
    error so the verdict is independent of the formatted message."""

    def test_internal_server_error_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: ... "
                "CodewhispererChatResponseStream(ServiceError(ServiceError "
                "{ source: InternalServerError(...) please try again ...)))"
            ),
        }
        assert _is_transient_raw_error(err) is True

    def test_status_50x_and_hint_are_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert _is_transient_raw_error({"data": "HTTP 503 Service Unavailable"}) is True
        assert _is_transient_raw_error({"data": "HTTP 504 Gateway Timeout"}) is True
        assert _is_transient_raw_error({"data": "status code 529 overloaded"}) is True
        assert _is_transient_raw_error({"data": "ServiceUnavailableException"}) is True
        assert _is_transient_raw_error({"data": "DispatchFailure ConnectionReset"}) is True
        assert _is_transient_raw_error({"data": "please try again"}) is True

    def test_5xx_token_in_message_field_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # 5xx signal carried in `message` (not `data`) must still be caught:
        # the classifier scans the combined haystack, so this does not fail
        # fast the way a data-only scan would (SHOULD-FIX). Auth is
        # still checked first, so an auth error with a stray 50x stays terminal.
        assert _is_transient_raw_error({"message": "InternalServerError", "data": ""}) is True
        assert _is_transient_raw_error({"message": "HTTP 503", "data": "Internal error"}) is True
        assert (
            _is_transient_raw_error(
                {"message": "please try again", "data": "AccessDeniedException"}
            )
            is False
        )

    def test_throttle_and_model_unavailable_are_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert _is_transient_raw_error({"data": "ThrottlingException: slow down"}) is True
        assert _is_transient_raw_error({"message": "Rate limit exceeded", "data": ""}) is True
        assert _is_transient_raw_error({"data": "The model 'opus' is not available."}) is True

    def test_auth_and_unknown_are_not_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Auth is terminal — a retry can't fix an expired/denied credential.
        assert _is_transient_raw_error({"data": "AccessDeniedException: nope"}) is False
        assert _is_transient_raw_error({"data": "ExpiredTokenException"}) is False
        # Unknown shapes and non-dicts are terminal (fail-fast, not retry).
        assert _is_transient_raw_error({"data": "max_tokens 500 exceeded"}) is False
        # 501 Not Implemented is terminal — only 500/502/503/504 + 529 retry.
        assert _is_transient_raw_error({"data": "HTTP 501 Not Implemented"}) is False
        assert (
            _is_transient_raw_error({"code": -32603, "message": "Internal error", "data": ""})
            is False
        )
        assert _is_transient_raw_error(None) is False
        assert _is_transient_raw_error("boom") is False

    def test_invalid_bearer_token_is_not_transient(self):
        """A rejected credential must be terminal, not fed to the retry ladder.

        The rejection carries no status code and no expiry wording, so without
        its own pattern it reaches the 5xx family — and a co-occurring transport
        error is enough to make it look retryable, spending the whole ladder on
        a credential no retry can revive.
        """
        from kiro_crew.acp.client import _is_transient_raw_error

        assert (
            _is_transient_raw_error(
                {"data": "The bearer token included in the request is invalid."}
            )
            is False
        )
        assert _is_transient_raw_error({"data": "invalid bearer token"}) is False
        # A co-occurring transport error must not flip it to retryable.
        assert (
            _is_transient_raw_error(
                {
                    "message": "Encountered an error in the response stream",
                    "data": "ConnectionResetError: the bearer token is invalid",
                }
            )
            is False
        )
        # An unrelated 'invalid' must stay out of the credential class.
        assert (
            _is_transient_raw_error(
                {"data": "ServiceUnavailableException: HTTP 503, invalid window"}
            )
            is True
        )

    def test_session_expired_is_not_transient(self):
        """Regression test: kiro-cli session expiry must be terminal.

        These error shapes can fall through to the 5xx branch (when they
        also carried DispatchFailure/ConnectionResetError), telling the user to
        retry when re-authentication was required.
        """
        from kiro_crew.acp.client import _is_transient_raw_error

        # Direct session-expired wording from kiro-cli.
        assert _is_transient_raw_error({"data": "session expired"}) is False
        assert _is_transient_raw_error({"data": "session has expired"}) is False
        assert _is_transient_raw_error({"data": "not logged in"}) is False
        assert _is_transient_raw_error({"data": "not authenticated"}) is False
        assert _is_transient_raw_error({"data": "login required"}) is False
        assert _is_transient_raw_error({"data": "authentication required"}) is False
        assert _is_transient_raw_error({"data": "re-authenticate"}) is False
        assert _is_transient_raw_error({"message": "session timed out", "data": ""}) is False
        # Session expiry with a co-occurring 5xx token: the session-expiry
        # branch must win (checked first).
        assert (
            _is_transient_raw_error({"data": "DispatchFailure: session expired", "message": ""})
            is False
        )
        assert (
            _is_transient_raw_error({"data": "ConnectionResetError: not logged in", "message": ""})
            is False
        )
        # A bare 401/403 — the shape an expired session actually arrives in.
        assert _is_transient_raw_error({"data": "HTTP 401"}) is False
        assert _is_transient_raw_error({"data": "HTTP 403"}) is False
        assert _is_transient_raw_error({"data": "status code 401"}) is False
        # 401 alongside the transport error left by the aborted request: the
        # 5xx family must not reclaim it and re-arm the retry ladder.
        assert (
            _is_transient_raw_error(
                {"data": "DispatchFailure ConnectionResetError: HTTP 401", "message": ""}
            )
            is False
        )
        # Real 5xx stays retryable — the auth-status branch must not overreach.
        assert _is_transient_raw_error({"data": "ServiceUnavailableException"}) is True
        assert _is_transient_raw_error({"data": "HTTP 503"}) is True

    def test_kiro_generic_generation_failure_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # kiro-cli's pre-stream generation failure wrapper: no request_id, no
        # error class, none of the 5xx/throttle tokens. Retryable — this exact
        # shape was surfaced to the user with no retry during a model-capacity
        # blip.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Kiro failed to generate a response",
        }
        assert _is_transient_raw_error(err) is True

    def test_generation_failure_scoped_to_data_and_loses_to_auth(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Phrase only in `message` (data carries a deterministic failure):
        # scoped match must not fire — stays terminal.
        assert (
            _is_transient_raw_error(
                {
                    "message": "Kiro failed to generate a response",
                    "data": "ValidationException: unsupported field",
                }
            )
            is False
        )
        # Auth is checked first and stays terminal even when the generation-
        # failure phrase co-occurs in data.
        assert (
            _is_transient_raw_error(
                {"data": "AccessDeniedException: Kiro failed to generate a response"}
            )
            is False
        )

    def test_kiro_process_failure_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error, classify_provider_error

        # kiro-cli's post-stream sibling of the generation-failure wrapper: a
        # request_id is present, no error class, none of the 5xx tokens. This
        # exact shape otherwise ends the turn with a terminal card and no retry.
        data = (
            "The service failed to process the request "
            "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
        )
        err = {"code": -32603, "message": "Internal error", "data": data}
        assert _is_transient_raw_error(err) is True
        verdict = classify_provider_error(data, data=data)
        assert verdict.retryable is True
        assert verdict.matched == "failed to process the request"

    def test_process_failure_scoped_to_data_and_loses_to_auth(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Phrase only in `message`: the scoped match must not fire.
        assert (
            _is_transient_raw_error(
                {
                    "message": "The service failed to process the request",
                    "data": "ValidationException: unsupported field",
                }
            )
            is False
        )
        # Auth is checked first and stays terminal even when the phrase co-occurs.
        assert (
            _is_transient_raw_error(
                {"data": "AccessDeniedException: The service failed to process the request"}
            )
            is False
        )

    def test_prose_spelled_dispatch_failure_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The AWS Rust SDK spells ONE connector fault two ways: the Debug repr
        # is CamelCase (DispatchFailure) and the Display impl is prose
        # ("dispatch failure"). kiro-cli surfaces whichever the failing layer
        # produced. While the pattern was CamelCase-only and case-SENSITIVE the
        # prose form read as an unknown shape and was classified TERMINAL, so
        # every affected turn surfaced a bare error on the FIRST attempt and no
        # retry ladder ever ran.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "An unknown error occurred: dispatch failure"
            ),
        }
        assert _is_transient_raw_error(err) is True
        # The CamelCase spelling keeps classifying identically.
        assert _is_transient_raw_error({"data": "DispatchFailure ConnectionReset"}) is True
        # Same two-spelling split for the sibling connector fault.
        assert _is_transient_raw_error({"data": "connection reset by peer"}) is True

    def test_bare_internal_error_stays_terminal(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # -32603's canonical message is literally "Internal error". The
        # separator tolerance loosens only the gaps BETWEEN words, never the
        # tokens, so this still matches nothing (no "server", no "failure") and
        # keeps falling through to the unknown-shape branch instead of being
        # mis-told to retry a condition that will never succeed.
        assert (
            _is_transient_raw_error(
                {"code": -32603, "message": "Internal error", "data": "Input is too long"}
            )
            is False
        )

    def test_terminal_branches_outrank_a_co_occurring_dispatch_failure(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Every terminal branch is checked BEFORE the 5xx family, so widening
        # the 5xx pattern must not let a connector token rescue an error that
        # can only be reproduced by retrying it.
        for data in (
            "You've reached your monthly usage limit. dispatch failure",
            "dispatch failure: session expired",
            "AccessDeniedException dispatch failure",
            "Improperly formed request dispatch failure",
        ):
            assert _is_transient_raw_error({"data": data, "message": ""}) is False

    def test_prose_dispatch_failure_reaches_the_retry_decider(self):
        import pytest

        from kiro_crew.acp.client import AcpError, _raise_acp_error
        from kiro_crew.llm_helpers import acp_error_is_transient

        # The classifier and the retry decider are two different layers, and
        # the gap between them is what broke: acp_error_is_transient prefers the
        # structured AcpError.transient flag and consults its string markers
        # ONLY when no flag is present. _TRANSIENT_MARKERS already listed the
        # prose spellings, so a transient=False flag made that list unreachable
        # dead code on this path. Assert the end-to-end verdict, not just the
        # classifier, or the next regex edit silently reintroduces this.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "An unknown error occurred: dispatch failure"
            ),
        }
        with pytest.raises(AcpError) as ei:
            _raise_acp_error(err)
        exc = ei.value
        assert exc.transient is True
        assert acp_error_is_transient(exc) is True
        # The raw provider text is replaced by the actionable 5xx wording.
        assert "transient error (HTTP 5xx)" in str(exc)

    def test_raise_acp_error_carries_transient_flag_and_formatted_message(self):
        import pytest

        from kiro_crew.acp.client import AcpError, _raise_acp_error

        transient_err = {
            "code": -32603,
            "message": "Internal error",
            "data": "InternalServerError: please try again (request_id: abc-123)",
        }
        with pytest.raises(AcpError) as ei:
            _raise_acp_error(transient_err)
        exc = ei.value
        assert exc.transient is True
        # Message is the formatted, user-facing string (not the raw dict).
        assert "transient error (HTTP 5xx)" in str(exc)
        assert "abc-123" in str(exc)

        auth_err = {"code": -32603, "message": "Internal error", "data": "AccessDeniedException"}
        with pytest.raises(AcpError) as auth_ei:
            _raise_acp_error(auth_err)
        auth_exc = auth_ei.value
        assert auth_exc.transient is False
        assert "authentication failed" in str(auth_exc).lower()

    def test_context_window_overflow_is_structural_and_non_transient(self):
        import pytest

        import kiro_crew.acp as acp_package
        from kiro_crew.acp import transport_errors
        from kiro_crew.acp.client import AcpError, _raise_acp_error

        error = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "The context window overflowed "
                "(request_id: 3844b25f-d540-4972-9b0b-03ddb5d177c6)"
            ),
        }
        with patch.object(
            transport_errors,
            "_is_transient_raw_error",
            wraps=transport_errors._is_transient_raw_error,
        ) as classify:
            with pytest.raises(AcpError) as raised:
                _raise_acp_error(error)

        exc = raised.value
        classify.assert_called_once_with(error, None)
        assert type(exc) is AcpError
        assert exc.transient is False
        assert exc.structural_terminal is True
        assert exc.context_overflow is True
        assert "Retrying on the same model session will not help" in str(exc)
        assert "3844b25f-d540-4972-9b0b-03ddb5d177c6" in str(exc)
        assert not hasattr(acp_client, "AcpContextOverflow")
        assert not hasattr(acp_package, "AcpContextOverflow")

    def test_context_window_overflow_tag_is_data_field_only(self):
        from kiro_crew.acp.client import AcpError, _raise_acp_error

        error = {
            "code": -32603,
            "message": "The context window overflowed",
            "data": "opaque error detail",
        }
        with pytest.raises(AcpError) as raised:
            _raise_acp_error(error)

        exc = raised.value
        assert type(exc) is AcpError
        assert exc.transient is False
        assert exc.structural_terminal is False
        assert exc.context_overflow is False

    def test_context_window_overflow_wording_is_surface_neutral(self):
        # The formatter cannot tell a first turn from a later one, and
        # ``subagent_manager/run.py`` appends this text after its own
        # post-activity reason, so a startup-only claim would contradict the
        # caller. Both causes and both remedies must be named.
        from kiro_crew.acp.client import AcpError, _raise_acp_error

        error = {
            "code": -32603,
            "message": "Internal error",
            "data": "The context window overflowed (request_id: 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0)",
        }
        with pytest.raises(AcpError) as raised:
            _raise_acp_error(error)

        text = str(raised.value)
        assert "before the turn could run" not in text
        assert "established conversation" in text
        assert "accumulated history" in text
        assert "fresh session" in text
        assert "always-loaded" in text
        assert "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0" in text
        assert raised.value.transient is False
        assert raised.value.structural_terminal is True
        assert raised.value.context_overflow is True

    def test_raise_acp_error_types_thinking_conversation_binding_mismatch(self):
        import pytest

        from kiro_crew.acp.client import (
            AcpConversationBindingMismatch,
            AcpError,
            _raise_acp_error,
        )

        mismatch = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "messages.3.content.0: Invalid `signature` in `thinking` block. "
                "The block is bound to a different conversation. Content before "
                "this block differs from when it was created."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch) as mismatch_error:
            _raise_acp_error(mismatch)
        assert mismatch_error.value.transient is False

        production_missing_predecessor = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "messages.3.content.0: Invalid `signature` in `thinking` block. "
                "The block is bound to a different conversation. "
                "The `thinking` block that preceded this one when it was created "
                "is missing from this request."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch):
            _raise_acp_error(production_missing_predecessor)

        reverse_order = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "The block is bound to a different conversation. "
                "messages.3.content.0: Invalid `signature` in `thinking` block."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch):
            _raise_acp_error(reverse_order)

        # Two unrelated fields cannot be combined into a recovery verdict.
        split_fields = {
            "code": -32603,
            "message": "The block is bound to a different conversation.",
            "data": "messages.3.content.0: Invalid `signature` in `thinking` block.",
        }
        with pytest.raises(AcpError) as split_error:
            _raise_acp_error(split_fields)
        assert not isinstance(split_error.value, AcpConversationBindingMismatch)

        # A signature that is merely invalid may be tampered or undecryptable.
        # The provider does not document conversation replacement as a repair for
        # that case, so it must stay outside the recoverable subtype.
        tampered = {
            "code": -32603,
            "message": "Prompt failed",
            "data": "messages.3.content.0: Invalid `signature` in `thinking` block.",
        }
        with pytest.raises(AcpError) as tampered_error:
            _raise_acp_error(tampered)
        assert not isinstance(tampered_error.value, AcpConversationBindingMismatch)

    def test_binding_mismatch_keeps_the_shared_raw_frame_tags(self):
        """The recoverable subtype is raised through the ordinary tagging path.

        Raising it before the shared tags are computed would drop facts other
        callers read from the same frame (here the usage-limit tag), so the
        subtype must change only the exception TYPE and its retry verdict.
        """
        import pytest

        from kiro_crew.acp.client import AcpConversationBindingMismatch, _raise_acp_error

        frame = {
            "code": -32603,
            "message": "Prompt failed",
            "data": (
                "Invalid `signature` in `thinking` block. The block is bound to a "
                "different conversation. Your monthly usage limit has been reached."
            ),
        }
        with pytest.raises(AcpConversationBindingMismatch) as raised:
            _raise_acp_error(frame)
        assert raised.value.transient is False
        assert getattr(raised.value, "usage_limit", False) is True

    def test_acp_error_default_transient_is_none(self):
        from kiro_crew.acp.client import AcpError

        assert AcpError("plain").transient is None
        assert AcpError("flagged", transient=True).transient is True


class TestAcpClientDrainEarlyExit:
    """_drain_notifications exits early once MCP servers go quiet, bounded by
    the hard cap. Guards the TTFT optimization (idle early-exit) against
    regressing into either a full-cap wait or a premature exit while active."""

    def test_idle_exit_constant_below_duration_constant(self):
        # The idle early-exit only short-circuits the drain if it can fire
        # before the hard cap. If a future edit bumps _DRAIN_IDLE_EXIT to or
        # above _DRAIN_DURATION, the cap always fires first, the idle path
        # becomes dead code, and cold-start TTFT silently regresses to the full
        # cap. The default-bound drain tests pass explicit duration/idle_exit
        # args, so only this assertion guards the module constants themselves.
        assert _DRAIN_IDLE_EXIT < _DRAIN_DURATION, (
            "_DRAIN_IDLE_EXIT must stay strictly below _DRAIN_DURATION "
            "to keep the idle early-exit reachable"
        )

    @pytest.mark.asyncio
    async def test_drain_exits_early_when_quiet(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        # Pre-buffered notification (arrived during _wait_for_response) — exercises
        # the buffered-drain path in addition to the live-read path (CR feedback).
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"name": "buffered-mcp"}
            ),
        ]

        # Two live MCP notifications, then quiet (None) forever.
        msgs = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"name": "builder-mcp"}
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/server_initialized", params={"name": "node"}),
        ]
        calls = {"n": 0}

        async def fake_read(timeout):
            if calls["n"] < len(msgs):
                m = msgs[calls["n"]]
                calls["n"] += 1
                return m
            await asyncio.sleep(timeout)  # honor the poll timeout, return nothing
            return None

        client._read_message = fake_read  # type: ignore[assignment]

        start = time.monotonic()
        # Hard cap 10s; idle window 0.2s for a fast test.
        await client._drain_notifications(duration=10.0, idle_exit=0.2)
        elapsed = time.monotonic() - start

        # Tight window proves it was the IDLE exit that fired (~0.2s after the last
        # live message), not the 10s cap NOR an instant exit from a swallowed
        # exception / always-None read (CR feedback). Floor guards against the
        # idle logic being deleted; ceiling guards against the full-cap wait.
        assert 0.15 < elapsed < 1.0, f"expected ~0.2s idle exit, got {elapsed:.2f}s"
        assert calls["n"] == 2  # both LIVE notifications were drained
        # The buffered notification is consumed + the buffer cleared by the drain.
        assert client._mcp_notifications == []

    @pytest.mark.asyncio
    async def test_drain_respects_hard_cap_when_chatty(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = []

        # A server that never goes quiet — the cap must bound the wait.
        reads = {"n": 0}

        async def fake_read(timeout):
            reads["n"] += 1
            return JsonRpcMessage(method="_kiro.dev/mcp/progress", params={"name": "slow"})

        client._read_message = fake_read  # type: ignore[assignment]

        start = time.monotonic()
        await client._drain_notifications(duration=0.5, idle_exit=5.0)
        elapsed = time.monotonic() - start

        # Idle never triggers (always chatty), so the 0.5s cap bounds it.
        assert 0.4 < elapsed < 2.0, f"hard cap not respected (took {elapsed:.2f}s)"
        # Confirm the drain loop actually iterated (processed reads), not just
        # that the timer expired (CR feedback).
        assert reads["n"] > 0, "drain loop never read a message during the cap window"


class TestResolveKiroBinEnvOverride:
    """_resolve_kiro_bin honors the KIROCREW_KIRO_BIN override for environments
    (e.g. AgentSpaces/DevSpaces) where the toolbox shim is broken."""

    def test_env_override_used_when_valid(self, tmp_path):
        from kiro_crew.acp.client import _resolve_kiro_bin
        from kiro_crew.kiro_prerequisite import TrustedAcpExecutableSnapshot

        fake = tmp_path / "kiro-cli"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        with (
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=str(fake),
            ),
            patch(
                "kiro_crew.kiro_prerequisite.snapshot_trusted_acp_executable",
                return_value=TrustedAcpExecutableSnapshot("/immutable/kiro-cli"),
            ),
        ):
            assert _resolve_kiro_bin() == "/immutable/kiro-cli"

    def test_resolver_returns_installed_path_without_copying(self, tmp_path):
        # The resolver hands back the user's installed binary path itself. It must
        # never substitute a private copy: Kiro CLI 2.15+ exec's a sibling
        # subcommand binary resolved relative to its own path, so a copy into a
        # flat dir breaks every spawn with ENOENT.
        from kiro_crew.acp import client as client_module

        macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        source = macos_dir / "kiro-cli"
        source.write_bytes(b"source")
        source.chmod(0o700)
        (macos_dir / "kiro-cli-chat").write_bytes(b"sibling")

        with patch.object(client_module, "resolve_kiro_cli", return_value=str(source)):
            launch_path = client_module._resolve_kiro_bin()

        assert launch_path == str(source)
        # The sibling the CLI dispatches to is still beside the launch path.
        assert (Path(launch_path).parent / "kiro-cli-chat").exists()

    def test_windows_resolver_accepts_runnable_candidate_anywhere(self, tmp_path):
        from kiro_crew.acp import client as client_module

        # Trust is "it runs": a Windows CLI outside Program Files (winget/scoop,
        # a venv Scripts dir) resolves for ACP launch rather than being rejected.
        candidate = tmp_path / "venv" / "Scripts" / "kiro-cli.exe"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"runnable")
        program_files = tmp_path / "Program Files"

        with (
            patch.object(client_module.platform_compat, "IS_WINDOWS", True),
            patch.object(
                client_module,
                "resolve_kiro_cli",
                return_value=str(candidate),
            ),
            patch.dict(
                "os.environ",
                {"ProgramFiles": str(program_files)},
                clear=True,
            ),
        ):
            assert client_module._resolve_kiro_bin() == os.path.realpath(str(candidate))

    @pytest.mark.asyncio
    async def test_spawn_launches_self_updated_override(self, tmp_path):
        from kiro_crew import sandbox as sandbox_module
        from kiro_crew.acp import client as client_module
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n# original\n")
        fake.chmod(0o755)
        mock_exec = AsyncMock()
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process; tracked for a de-flake pass
        mock_exec.return_value.pid = 12345
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": str(fake)}),
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
            patch.object(client_module, "_get_child_pids", return_value=[]),
            # This test asserts WHICH bytes get launched, not that the spawn is
            # sandboxed (covered by test_sandbox_*.py). A CI runner with
            # kernel.apparmor_restrict_unprivileged_userns=1 genuinely cannot
            # build a namespace sandbox and wrap_argv fail-closes by design, so
            # pin the decision instead of inheriting the host's capability.
            patch.object(sandbox_module, "_allow_unsandboxed_exec", return_value=True),
            patch.object(
                client_module,
                "resolve_kiro_cli",
                return_value=str(fake),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                mock_exec,
            ),
        ):
            # A Kiro self-update legitimately rewrites the binary after gateway
            # start. Trust is "it runs", so the updated bytes still launch — the
            # snapshot just pins whatever bytes are resolved at spawn time.
            KiroPrerequisiteService(home=tmp_path, data_home=tmp_path / "data")
            fake.write_bytes(b"#!/bin/sh\n# self-updated\n")
            fake.chmod(0o755)

            client = AcpClient(work_dir=tmp_path / "workspace")
            await client._spawn()
            mock_exec.assert_awaited()

            await _stop_stderr_drain(client)

    @pytest.mark.asyncio
    async def test_spawn_passes_installed_path_through_exact_wrappers(self, tmp_path):
        from kiro_crew.acp import client as client_module

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        launch_path = str(fake)
        mock_exec = AsyncMock(side_effect=RuntimeError("spawn failed"))
        wrapped: dict[str, object] = {}

        def capture_wrap(argv, mode, **kwargs):
            wrapped.update(argv=list(argv), mode=mode, kwargs=kwargs)
            return ["/usr/bin/sandbox-wrapper", *argv], None

        with (
            patch.object(
                client_module,
                "_resolve_kiro_bin",
                return_value=launch_path,
            ),
            patch.object(client_module, "wrap_argv", side_effect=capture_wrap),
            patch.object(
                client_module, "assert_voice_runtime_outside_agent_workspace"
            ) as voice_guard,
            patch.object(
                client_module,
                "cgroup_scope_argv",
                side_effect=lambda argv: ["/usr/bin/cgroup-wrapper", *argv],
            ),
            patch(
                "asyncio.create_subprocess_exec",
                mock_exec,
            ),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")

            with pytest.raises(RuntimeError, match="spawn failed"):
                await client._spawn()

        launch_argv = wrapped["argv"]
        assert isinstance(launch_argv, list)
        assert launch_argv == [launch_path, "acp", "--agent", client._agent]
        assert wrapped["mode"] == "auto"
        wrap_kwargs = dict(wrapped["kwargs"])
        # The per-session terminal-log window is allocated at spawn time; its
        # path is runtime-owned, so only its presence and shape are pinned here.
        extra_private = wrap_kwargs.pop("extra_private_dirs")
        assert isinstance(extra_private, (list, tuple))
        # The default tier leaves ~/.ssh readable, so the ssh-agent socket is kept.
        assert wrap_kwargs == {
            "strip_python_env": True,
            "is_kiro_cli": True,
            **({} if sys.platform == "win32" else {"forward_ssh_auth_sock": True}),
        }
        voice_guard.assert_called_once_with(client._work_dir)
        spawn_call = mock_exec.await_args
        assert strip_spawn_shim(spawn_call.args) == (
            "/usr/bin/cgroup-wrapper",
            "/usr/bin/sandbox-wrapper",
            launch_path,
            "acp",
            "--agent",
            client._agent,
        )
        # No inherited snapshot descriptor: the installed binary is exec'd in
        # place, so there is nothing to hand down to the wrapper chain.
        #
        # macOS is the exception, and for a different fd:
        # bind_voice_safe_agent_workspace opens the agent workspace as a
        # directory descriptor there so the child enters it by fchdir instead of
        # re-resolving a pathname a same-UID symlink retarget could aim
        # elsewhere. _spawn hands that descriptor to create_subprocess_limited
        # as chdir_fd, which folds exactly that one fd into pass_fds so the
        # spawn shim inherits it. Off darwin the binding returns None, so
        # chdir_fd is None and pass_fds stays absent. The binding is also gated
        # on the harness owning an internal sandbox (ACP_BACKENDS_INTERNAL_SANDBOX,
        # which the default Kiro backend built here belongs to), so a backend
        # outside that set stays on the pathname cwd whatever the platform says.
        if sys.platform == "darwin":
            pass_fds = spawn_call.kwargs["pass_fds"]
            # Exactly the bound workspace descriptor, nothing else: no snapshot
            # fd is inherited here, so the only descriptor handed down is the
            # one the fchdir binding produced. Which int it is, is pinned on
            # every OS by the forced-darwin companion below.
            assert isinstance(pass_fds, tuple)
            assert len(pass_fds) == 1
            assert isinstance(pass_fds[0], int) and pass_fds[0] >= 0
        else:
            assert "pass_fds" not in spawn_call.kwargs

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "the binding opens the workspace as a directory descriptor, which "
            "needs O_DIRECTORY; Windows has no such flag and os.open on a "
            "directory raises, so a forced-darwin binding refuses with "
            "'cannot-verify' before any spawn is attempted"
        ),
    )
    async def test_spawn_passes_exactly_the_bound_workspace_fd_on_darwin(
        self, tmp_path, monkeypatch
    ):
        # The darwin half of the assertion above runs on no CI shard, because no
        # macOS job collects this module. The binding gate is a runtime
        # sys.platform read rather than an import-time constant, so forcing the
        # platform runs the REAL binding on any POSIX host and pins the whole
        # composition: _spawn -> bind_voice_safe_agent_workspace_async ->
        # create_subprocess_limited(chdir_fd=...) -> a pass_fds holding exactly
        # that descriptor. Spy-and-delegate rather than stub, so the fd compared
        # against pass_fds is the one the real binding opened.
        from kiro_crew.acp import client as client_module

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        launch_path = str(fake)
        mock_exec = AsyncMock(side_effect=RuntimeError("spawn failed"))
        bound_fds: list[int | None] = []
        real_bind = client_module.bind_voice_safe_agent_workspace_async

        async def spy_bind(workspace):
            spawn_dir, descriptor = await real_bind(workspace)
            bound_fds.append(descriptor)
            return spawn_dir, descriptor

        with (
            patch.object(client_module, "_resolve_kiro_bin", return_value=launch_path),
            patch.object(
                client_module,
                "wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (list(argv), None),
            ),
            patch.object(client_module, "assert_voice_runtime_outside_agent_workspace"),
            patch.object(client_module, "cgroup_scope_argv", side_effect=lambda argv: list(argv)),
            patch.object(
                client_module, "bind_voice_safe_agent_workspace_async", side_effect=spy_bind
            ),
            patch("asyncio.create_subprocess_exec", mock_exec),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")
            monkeypatch.setattr(sys, "platform", "darwin")

            with pytest.raises(RuntimeError, match="spawn failed"):
                await client._spawn()

        assert len(bound_fds) == 1
        bound_fd = bound_fds[0]
        assert isinstance(bound_fd, int)
        # The spy reads the descriptor before _spawn's failure handler closes it,
        # so the value survives even though the fd itself does not.
        assert mock_exec.await_args.kwargs["pass_fds"] == (bound_fd,)

    def test_env_override_ignored_when_missing_file(self, tmp_path):
        # A configured-but-nonexistent path must not be returned; resolution
        # falls through to the normal candidates / PATH lookup.
        from kiro_crew.acp.client import _resolve_kiro_bin

        missing = str(tmp_path / "does-not-exist")
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": missing}),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            assert _resolve_kiro_bin() is None

    def test_env_override_ignored_when_not_executable(self, tmp_path):
        from kiro_crew.acp.client import _resolve_kiro_bin

        nonexec = tmp_path / "kiro-cli"
        nonexec.write_text("#!/bin/sh\n")
        nonexec.chmod(0o644)  # not executable
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": str(nonexec)}),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            assert _resolve_kiro_bin() is None

    def test_no_env_falls_through(self):
        # With no override set, resolution uses the normal candidate/PATH path
        # and never returns the override sentinel.
        from kiro_crew.acp.client import _resolve_kiro_bin

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_KIRO_BIN"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            # Should not raise; returns either a real path or None.
            result = _resolve_kiro_bin()
            assert result is None or isinstance(result, str)


class TestDispatchSubagentEvents:
    """_dispatch_events must yield EVENT_SUBAGENT_LIST and EVENT_SUBAGENT_ACTIVITY
    for kiro-cli's _kiro.dev/subagent/list_update and _kiro.dev/session/update
    notifications."""

    @pytest.mark.asyncio
    async def test_dispatch_yields_subagent_list_and_activity(self):
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            EVENT_SUBAGENT_LIST,
            JsonRpcMessage,
        )

        client = AcpClient()

        subs = [{"sessionId": "s1", "role": "explorer", "status": {"type": "working"}}]
        frames = [
            ("subagent_list", JsonRpcMessage(params={"subagents": subs})),
            # tool_call_chunk → toolCallId attribution
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-1",
                            "title": "read",
                        },
                    }
                ),
            ),
            # agent_message_chunk → sub-agent text
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "hello from sub",
                        },
                    }
                ),
            ),
            # non-dict subagents payload must be ignored (no yield)
            ("subagent_list", JsonRpcMessage(params={"subagents": "nope"})),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        lists = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(lists) == 1
        assert lists[0].subagents == subs
        # one tool-call activity (with toolCallId) + one text activity
        tc = [a for a in acts if a.tool_call_id == "tc-1"]
        txt = [a for a in acts if a.text == "hello from sub"]
        assert len(tc) == 1 and tc[0].sub_session_id == "s1" and tc[0].title == "read"
        assert len(txt) == 1 and txt[0].sub_session_id == "s1"

    @pytest.mark.asyncio
    async def test_dispatch_ignores_activity_naming_this_session(self):
        """A frame naming THIS session is the parent's own stream, not a child's.

        kiro-cli carries the parent turn's own ``tool_call_chunk`` on
        ``_kiro.dev/session/update`` with ``params.sessionId`` set to the
        parent's session, the same method a child's update arrives on, so the
        sessionId is the only separator. Recorded live in
        ``test/fixtures/acp_frames/kiro/session.jsonl``. Both carriers are
        checked so one guard cannot cover half the shape.
        """
        from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, JsonRpcMessage

        client = AcpClient()
        client._session_id = "own-1"

        frames = [
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "own-1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-own",
                            "title": "shell",
                        },
                    }
                ),
            ),
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "own-1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "the parent's own streamed text",
                        },
                    }
                ),
            ),
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "child-1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-child",
                            "title": "read",
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert [a.sub_session_id for a in acts] == ["child-1"]
        assert acts[0].tool_call_id == "tc-child"

    @pytest.mark.asyncio
    async def test_a_long_roster_leaves_the_client_remembering_no_child_id(self):
        """``AcpClient`` is a PASSTHROUGH for the native-child roster: it yields
        the full list and retains nothing.

        The roster cap (``NATIVE_CHILD_ROSTER_CAP``) binds every store that
        REMEMBERS child ids -- the parent handle's counted set, its KAS display
        roster, and ``AcpRuntime``'s routing recognition set. This asserts the
        client is not a fourth such store, so the class is closed at three: a
        store added here would need the same cap, the same overflow count and
        the same answer for a child past it, and one that arrives without them
        is the one-of-N shape that makes a cap decision partial.
        """
        from kiro_crew.acp.types import EVENT_SUBAGENT_LIST, JsonRpcMessage

        client = AcpClient()
        roster = [{"sessionId": f"c-{i}"} for i in range(5000)]
        frames = [
            ("subagent_list", JsonRpcMessage(params={"subagents": roster})),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]
        events = [ev async for ev in client._dispatch_events(req_id=1, timeout=1.0)]

        lists = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        assert len(lists) == 1 and lists[0].subagents == roster
        probes = {"c-0", "c-2500", "c-4999"}
        for name, value in vars(client).items():
            if isinstance(value, (set, frozenset)):
                assert not probes & set(value), name
            elif isinstance(value, dict):
                assert not probes & {str(k) for k in value}, name
            elif isinstance(value, (list, tuple)):
                assert not probes & {str(v) for v in value}, name

    @pytest.mark.asyncio
    async def test_dispatch_redacts_and_extracts_subagent_output(self):
        """Sub-agent titles/text are LLM-influenced, so credentials + exfil URLs
        must be scrubbed before they surface as EVENT_SUBAGENT_ACTIVITY. Also
        the streamed text must be read from the nested ``content.text`` shape
        kiro-cli 2.10.0 emits, not only the flat top-level ``text`` field."""
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            JsonRpcMessage,
        )

        client = AcpClient()

        frames = [
            # tool_call_chunk with a credential embedded in the title
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-1",
                            "title": "leak AKIAIOSFODNN7EXAMPLE now",
                        },
                    }
                ),
            ),
            # agent_message_chunk with the text under nested content.text (2.10.0)
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": "secret AKIAIOSFODNN7EXAMPLE here"},
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        tc = [a for a in acts if a.tool_call_id == "tc-1"]
        txt = [a for a in acts if a.text]
        # Title redacted (credential scrubbed, no raw AKIA leaks through).
        assert len(tc) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in tc[0].title
        assert "[REDACTED" in tc[0].title
        # Nested content.text was extracted (robustness) AND redacted (security).
        assert len(txt) == 1 and txt[0].sub_session_id == "s1"
        assert "AKIAIOSFODNN7EXAMPLE" not in txt[0].text
        assert "[REDACTED" in txt[0].text

    @pytest.mark.asyncio
    async def test_dispatch_subagent_text_edge_cases(self):
        """Regression guards for the nested-vs-flat content.text extraction:
        (1) an EMPTY nested content.text must not shadow a populated flat
        top-level ``text`` (older payloads); (2) a reasoning/thinking content
        block must NOT surface as user-visible sub-agent output; (3) a non-dict
        ``content`` must not raise (the flat pre-port read tolerated it)."""
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            JsonRpcMessage,
        )

        client = AcpClient()

        frames = [
            # (1) empty nested content.text + populated flat text -> flat wins
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": ""},
                            "text": "flat fallback output",
                        },
                    }
                ),
            ),
            # (2) reasoning/thinking content block -> suppressed (no user text)
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "thinking", "text": "internal reasoning"},
                        },
                    }
                ),
            ),
            # (3) non-dict content -> must not crash; flat text used if present
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": ["a", "list", "block"],
                            "text": "flat when content is a list",
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        txt = [e.text for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text]
        # (1) flat fallback surfaced when nested content.text is empty.
        assert "flat fallback output" in txt
        # (2) the reasoning block was never surfaced as sub-agent output.
        assert "internal reasoning" not in txt
        # (3) non-dict content did not crash and the flat text still surfaced.
        assert "flat when content is a list" in txt


class TestTrackMetadataCredits:
    """_track_metadata extracts per-turn credits from kiro meteringUsage."""

    @staticmethod
    def _metadata_msg(credit_values, *, pct=17.1):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={
                "contextUsagePercentage": pct,
                "meteringUsage": [
                    {"value": v, "unit": "credit", "unitPlural": "credits"} for v in credit_values
                ],
            },
        )

    def test_accumulates_credits_from_metering_usage(self):
        client = AcpClient()
        client._track_metadata(self._metadata_msg([0.63]))
        assert client.last_prompt_stats.credits == pytest.approx(0.63)
        assert client.last_prompt_stats.context_pct == pytest.approx(17.1)

    def test_sums_credits_across_notifications(self):
        client = AcpClient()
        client._track_metadata(self._metadata_msg([0.5]))
        client._track_metadata(self._metadata_msg([0.25, 0.25]))
        assert client.last_prompt_stats.credits == pytest.approx(1.0)

    def test_ignores_non_credit_units_and_bad_values(self):
        client = AcpClient()
        msg = self._metadata_msg([])
        msg.params["meteringUsage"] = [
            {"value": 5, "unit": "token"},
            {"value": "x", "unit": "credit"},
            {"value": 0.4, "unit": "credit"},
        ]
        client._track_metadata(msg)
        assert client.last_prompt_stats.credits == pytest.approx(0.4)

    def test_metadata_without_metering_usage_leaves_credits_zero(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 12.5},
            )
        )
        assert client.last_prompt_stats.credits == 0.0
        assert client.last_prompt_stats.context_pct == pytest.approx(12.5)


class TestTrackMetadataWindowResolution:
    """_track_metadata derives context_window_tokens from the resolved model
    when no usage_update has set it (kiro-cli 2.10+ does not send one)."""

    @staticmethod
    def _metadata_msg(pct):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": pct},
        )

    def test_resolves_1m_window_from_resolved_model_id(self):
        client = AcpClient()
        assert client._model == "auto"
        client._resolved_model_id = "claude-opus-4.6"
        client._track_metadata(self._metadata_msg(25.0))
        assert client.last_prompt_stats.context_window_tokens == 1_000_000
        assert client.last_prompt_stats.context_used_tokens == 250_000
        assert client.last_prompt_stats.context_pct == pytest.approx(25.0)

    def test_resolves_200k_window_for_200k_model(self):
        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.5"
        client._track_metadata(self._metadata_msg(50.0))
        assert client.last_prompt_stats.context_window_tokens == 200_000
        assert client.last_prompt_stats.context_used_tokens == 100_000

    def test_falls_back_to_self_model_when_resolved_model_id_unset(self):
        client = AcpClient()
        client._model = "claude-sonnet-4.6"
        client._track_metadata(self._metadata_msg(10.0))
        assert client.last_prompt_stats.context_window_tokens == 1_000_000

    def test_does_not_overwrite_window_already_set_by_usage_update(self):
        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.5"  # would resolve to 200K
        # Simulate a real usage_update having established authoritative counts
        # (1M window, 300K used -> 30%). context_tokens_from_usage marks them
        # authoritative — the shape a real usage_update leaves behind.
        client.last_prompt_stats.context_window_tokens = 1_000_000
        client.last_prompt_stats.context_used_tokens = 300_000
        client.last_prompt_stats.context_pct = 30.0
        client.last_prompt_stats.context_tokens_from_usage = True
        client._track_metadata(self._metadata_msg(50.0))
        # Metadata must neither re-resolve the window (no backfill once
        # authoritative) nor clobber the usage-derived pct to its own 50%.
        assert client.last_prompt_stats.context_window_tokens == 1_000_000
        assert client.last_prompt_stats.context_pct == pytest.approx(30.0)

    def test_no_model_resolvable_leaves_window_zero(self):
        client = AcpClient()
        client._model = ""
        client._resolved_model_id = None
        client._track_metadata(self._metadata_msg(5.0))
        assert client.last_prompt_stats.context_window_tokens == 0

    def test_non_registry_uncached_model_leaves_window_zero_not_200k(self):
        # REGRESSION (gpt-5.6-terra showed 200K): a model that is NEITHER in the
        # registry NOR the kiro-list cache must NOT be backfilled to a guessed
        # 200k — that would override the frontend's authoritative per-model
        # window. Leave it 0 so the frontend cache drives the meter; kiro's real
        # usage_update.size still sets the correct window when it comes. Use a
        # clearly-synthetic id so a real kiro-list cache (which legitimately
        # carries GPT/DeepSeek windows) can't make this model "known".
        client = AcpClient()
        client._resolved_model_id = "totally-unknown-model-xyz"
        client._track_metadata(self._metadata_msg(10.0))
        assert client.last_prompt_stats.context_window_tokens == 0
        # pct is still recorded so the frontend can derive used from its window.
        assert client.last_prompt_stats.context_pct == pytest.approx(10.0)

    def test_kiro_cached_non_registry_model_backfills_real_window(self):
        # The flip side of the centralization: once the kiro-list cache is seeded
        # with a non-Anthropic model's real window, the backfill SHOULD report it
        # (it is now a "known" window), instead of the old no-op. This is what
        # lets GPT/DeepSeek sessions show their real window before a usage_update.
        import kiro_crew.model_registry as mr

        saved = dict(mr._KIRO_WINDOWS)
        try:
            mr._KIRO_WINDOWS["deepseek-3.2"] = 164000
            client = AcpClient()
            client._resolved_model_id = "deepseek-3.2"
            client._track_metadata(self._metadata_msg(50.0))
            assert client.last_prompt_stats.context_window_tokens == 164000
            assert client.last_prompt_stats.context_used_tokens == 82000
        finally:
            mr._KIRO_WINDOWS.clear()
            mr._KIRO_WINDOWS.update(saved)

    def test_malformed_pct_does_not_raise_or_update_stats(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.6"
        client.last_prompt_stats.context_pct = 42.0
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata", params={"contextUsagePercentage": "not-a-number"}
            )
        )
        assert client.last_prompt_stats.context_pct == 42.0
        assert client.last_prompt_stats.context_window_tokens == 0


class TestSubstitutionAdvisory:
    """The model-substitution advisory (-32603 'Using X instead') is non-fatal.

    claude-agent-acp returns a JSON-RPC -32603 error frame on session/new (and
    related calls) when admin-tier / headless-tier policy substitutes the
    requested model for another. The substitute is already live, so KiroCrew
    must treat the advisory as a warning and keep the session, NOT raise.
    """

    def test_advisory_detected(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] "
                    "instead."
                )
            },
        }
        assert _is_model_substitution_advisory(err) is True

    def test_advisory_detected_data_is_plain_string(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": 'Model "x" is restricted ... Using y instead.',
        }
        assert _is_model_substitution_advisory(err) is True

    def test_real_internal_error_still_raises(self):
        # A genuine -32603 with no substitution wording must NOT be swallowed.
        err = {"code": -32603, "message": "Internal error", "data": "request_id: abc-123"}
        assert _is_model_substitution_advisory(err) is False

    def test_throttle_not_treated_as_advisory(self):
        err = {
            "code": -32603,
            "message": "ThrottlingException: rate exceeded",
            "data": "rate limit",
        }
        assert _is_model_substitution_advisory(err) is False

    def test_wrong_code_not_advisory(self):
        # Right wording, wrong code -> still raise (only -32603 qualifies).
        err = {
            "code": -32602,
            "message": "Invalid params",
            "data": {"details": 'Model "x" is restricted ... Using y instead'},
        }
        assert _is_model_substitution_advisory(err) is False

    def test_non_dict_not_advisory(self):
        assert _is_model_substitution_advisory("plain string") is False
        assert _is_model_substitution_advisory(None) is False

    def test_empty_or_missing_data_not_advisory(self):
        assert (
            _is_model_substitution_advisory({"code": -32603, "message": "Internal error"}) is False
        )
        assert _is_model_substitution_advisory({"code": -32603, "data": ""}) is False

    def test_partial_wording_not_advisory(self):
        # "is restricted" without the "Using X instead" half must not match.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": {"details": 'Model "opus" is restricted by your settings.'},
        }
        assert _is_model_substitution_advisory(err) is False

    @pytest.mark.asyncio
    async def test_wait_for_response_returns_result_on_advisory(self, tmp_path):
        """_wait_for_response returns the result (no raise) on a substitution advisory."""
        client = AcpClient(work_dir=tmp_path)
        advisory = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = advisory
        msg.result = {"sessionId": "abc-123"}
        msg.method = None
        msg.id = 7

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        out = await client._wait_for_response(7, timeout=1.0)
        assert out == {"sessionId": "abc-123"}

    @pytest.mark.asyncio
    async def test_wait_for_response_raises_on_real_error(self, tmp_path):
        """A genuine -32603 (no substitution wording) still raises AcpError."""
        client = AcpClient(work_dir=tmp_path)
        real_err = {"code": -32603, "message": "Internal error", "data": "request_id: zzz"}
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = real_err
        msg.result = None
        msg.method = None
        msg.id = 9

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        with pytest.raises(AcpError):
            await client._wait_for_response(9, timeout=1.0)

    @pytest.mark.asyncio
    async def test_wait_for_response_records_substitute_on_error_only_advisory(self, tmp_path):
        """Spec-real advisory frame: error set, result is None.

        A spec-compliant JSON-RPC response carries either result or error,
        not both. On the realistic error-only advisory (no co-located result):
          1. ``_wait_for_response`` must NOT raise; it returns ``{}``.
          2. It MUST record the gateway-named substitute model on
             ``self._last_substitution_model`` so the session/new caller can
             adopt it and re-issue creation. Without this, ``_session_id``
             would silently become None and the next call would crash on the
             ``Cannot set config option before session is initialized`` guard.
        """
        client = AcpClient(work_dir=tmp_path)
        advisory = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = advisory
        msg.result = None  # spec-real: error-only frame, no co-located result
        msg.method = None
        msg.id = 11

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        out = await client._wait_for_response(11, timeout=1.0)

        assert out == {}, "advisory must surface as empty dict, not raise"
        assert (
            client._last_substitution_model == "global.anthropic.claude-sonnet-4-6[1m]"
        ), "substitute model must be recorded for the session/new caller to adopt"


class TestSubstituteModelExtractor:
    """_substitute_model_from_advisory pulls the gateway-served model id out of
    the -32603 advisory so the session/new caller can adopt it."""

    def test_extracts_versioned_substitute(self):
        err = {
            "code": -32603,
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        assert _substitute_model_from_advisory(err) == "global.anthropic.claude-sonnet-4-6[1m]"

    def test_extracts_with_trailing_period_and_quotes(self):
        err = {
            "code": -32603,
            "data": {"details": "Model X is restricted. Using claude-opus-4-6[1m] instead."},
        }
        assert _substitute_model_from_advisory(err) == "claude-opus-4-6[1m]"

    def test_returns_none_for_real_internal_error(self):
        err = {"code": -32603, "data": "request_id: abc-123"}
        assert _substitute_model_from_advisory(err) is None

    def test_returns_none_for_wrong_code(self):
        err = {"code": -32602, "data": {"details": "is restricted ... Using y instead"}}
        assert _substitute_model_from_advisory(err) is None

    def test_returns_none_for_non_dict(self):
        assert _substitute_model_from_advisory(None) is None
        assert _substitute_model_from_advisory("plain") is None


class TestSubstitutionFollow:
    """session/new returning a substitution advisory carries NO sessionId.
    _new_session_following_substitution must adopt the substitute model and
    re-issue session/new so a real session is actually created -- otherwise the
    next step (set_config_option) crashes on the uninitialized-session guard.
    This is the no-sessionId path the earlier suite did not cover.
    """

    def _advisory_resp(self):
        # Mirrors what _wait_for_response returns on the advisory: empty dict,
        # plus it sets _last_substitution_model as a side effect (simulated here).
        return {}

    @pytest.mark.asyncio
    async def test_adopts_substitute_and_reissues(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        # Avoid touching the real filesystem seeding.
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        # First wait: advisory (no sessionId) + record substitute, as the real
        # _wait_for_response does. Second wait: real session on the substitute.
        calls = {"n": 0}

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            calls["n"] += 1
            if calls["n"] == 1:
                client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
                return {}  # advisory frame -> no sessionId
            return {"sessionId": "real-sess-1"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") == "real-sess-1"
        # Adopted the gateway-served model and re-seeded settings before retry.
        assert client._model == "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings.assert_called_once()
        # Exactly two session/new issues: the original + one retry (bounded).
        assert sent.count("session/new") == 2

    async def _run_substitution_retry_case(self, tmp_path, monkeypatch, *, lose_surface):
        from kiro_crew import model_registry
        from kiro_crew.acp import seed_provenance as sp

        monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
        monkeypatch.setattr(sp, "_RECORDS", {})
        monkeypatch.setattr(sp, "_LIVE", {})
        monkeypatch.setattr(sp, "_SHARERS", {})
        monkeypatch.setattr(sp, "_sidecar_path", lambda: tmp_path / "seeds.json")

        owner = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        owner._model = "global.anthropic.claude-opus-4-8[1m]"
        owner._write_claude_local_settings()
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._model = owner._model
        client._write_claude_local_settings()
        assert client._claude_settings_shared is True
        assert client._permission_surface_governed is True

        server = {
            "name": "governed",
            "command": "/bin/governed",
            "args": [],
            "env": [],
            "type": "stdio",
        }
        client._session_mcp_cache = [server]
        client._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        resolve_calls = []

        def _resolve():
            resolve_calls.append(client._permission_surface_governed)
            client._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("new", "new")
            return [server] if client._permission_surface_governed else []

        client._resolve_session_mcp_servers = _resolve  # type: ignore[assignment]
        report_calls = []
        guard_calls = []
        client._begin_session_report = (  # type: ignore[assignment]
            lambda servers: report_calls.append(list(servers or []))
        )
        client._guard_unresolved_mcp_refs = (  # type: ignore[assignment]
            lambda wire_servers: guard_calls.append(list(wire_servers or []))
        )
        sent = []
        settings = tmp_path / ".claude" / "settings.local.json"

        async def _send(method, params):
            sent.append(list(params.get("mcpServers") or []))
            if len(sent) == 1 and lose_surface:
                # A link: the one shape that can be neither governed nor left out
                # of the session's setting sources.
                settings.unlink()
                settings.symlink_to(tmp_path / "elsewhere.json")
            return len(sent)

        waits = 0

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            nonlocal waits
            waits += 1
            if waits == 1:
                client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
                return {}
            return {"sessionId": "retry-session"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        response = await client._new_session_following_substitution()
        assert response == {"sessionId": "retry-session"}
        assert sent[0] == [server]
        return sent[1], server, resolve_calls, report_calls, guard_calls

    @pytest.mark.asyncio
    async def test_substitution_retry_withholds_mcp_when_surface_lost(self, tmp_path, monkeypatch):
        retried, _server, resolves, reports, guards = await self._run_substitution_retry_case(
            tmp_path, monkeypatch, lose_surface=True
        )
        assert retried == []
        assert resolves == [False]
        assert reports == [[_server], []]
        assert guards == [[_server], []]

    @pytest.mark.asyncio
    async def test_substitution_retry_keeps_mcp_when_surface_intact(self, tmp_path, monkeypatch):
        retried, server, resolves, reports, guards = await self._run_substitution_retry_case(
            tmp_path, monkeypatch, lose_surface=False
        )
        assert retried == [server]
        assert resolves == [True]
        assert reports == [[server], [server]]
        assert guards == [[server], [server]]

    @pytest.mark.asyncio
    async def test_happy_path_no_retry(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"sessionId": "sess-ok"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") == "sess-ok"
        # No substitution -> no re-seed, single session/new.
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1

    @pytest.mark.asyncio
    async def test_no_infinite_retry_when_substitute_unparseable(self, tmp_path):
        # Advisory fired but substitute could not be parsed (None) -> we do NOT
        # retry; caller gets an empty resp and raises a clear error upstream.
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            client._last_substitution_model = None  # unparseable
            return {}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") is None
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1  # no retry

    @pytest.mark.asyncio
    async def test_no_retry_when_substitute_same_as_current(self, tmp_path):
        # Defensive: if the gateway "substitutes" to the model we already asked
        # for, don't loop -- treat as terminal (no retry).
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") is None
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1


class TestSubstitutionWrappersAndRedaction:
    """Pin the wrapper raise paths and the dual-redaction discipline applied to
    backend-derived strings before they hit logger sinks (which fan out to the
    dashboard activity feed and Slack).
    """

    @pytest.mark.asyncio
    async def test_initialize_session_raises_on_no_sessionid(self, tmp_path):
        """If the helper still returns no sessionId after the substitution-follow
        retry, _initialize_session raises AcpError loudly instead of letting the
        next handshake step crash on the opaque "Cannot set config option before
        session is initialized" guard.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session(*args, **kwargs):
            return {}

        client._new_session_following_substitution = _no_session  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "session/new returned no sessionId" in str(excinfo.value)
        assert "the backend did not create a session" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_initialize_session_message_omits_substitute_when_unchanged(self, tmp_path):
        """When the helper returns no sessionId and self._model is unchanged
        (no substitution happened), the AcpError text MUST NOT include the
        "even after adopting substitute model" clause.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_no_substitution(*args, **kwargs):
            # Helper returned without changing self._model -> no substitution.
            return {}

        client._new_session_following_substitution = _no_session_no_substitution  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "even after adopting substitute model" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_initialize_session_message_includes_substitute_when_changed(self, tmp_path):
        """When the helper adopted a substitute (self._model != model_before),
        the AcpError text MUST include the "even after adopting substitute
        model {...}" clause.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_with_substitution(*args, **kwargs):
            # Helper adopted the gateway-served substitute before giving up.
            client._model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._new_session_following_substitution = _no_session_with_substitution  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "even after adopting substitute model" in str(excinfo.value)
        assert "claude-sonnet-4-6" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_new_session_following_substitution_redacts_log_payload(self, tmp_path):
        """The substitute model logged in _new_session_following_substitution
        MUST flow through redact_exfiltration_urls + redact_credentials before
        reaching logger.warning. Drives the helper with a substitute that
        contains a URL-shaped token and asserts the emitted log payload was
        redacted.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        # Inject a substitute that embeds an AWS access-key-shaped substring
        # so the dual redactor heuristic actually fires (redact_exfiltration_urls
        # matches the URL because the query carries an AKIA token, and
        # redact_credentials matches the standalone AKIA pattern). A bare
        # arbitrary URL is too tame for the heuristic and would leave the test
        # asserting on a no-op pass-through.
        url_shaped = "http://attacker.example.com/exfil?token=AKIAIOSFODNN7EXAMPLE"

        sent: list[str] = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        wait_calls = {"n": 0}

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            wait_calls["n"] += 1
            if wait_calls["n"] == 1:
                client._last_substitution_model = url_shaped
                return {}  # no sessionId on first call
            return {"sessionId": "real-sess"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._new_session_following_substitution()

        # Find the warning call that mentions the substitution adoption.
        adopt_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and "adopting gateway-served model" in c.args[0]
        ]
        assert adopt_calls, "expected an 'adopting gateway-served model' warning"
        # The redacted payload is the second positional argument.
        emitted = adopt_calls[0].args[1]
        # The credential MUST be gone. The URL MUST be replaced with the
        # redactor diagnostic marker. The redactor preserves the host name
        # inside "[REDACTED: suspicious URL to <host>]" so operators can
        # investigate the leak source -- that is BY DESIGN, not a bypass.
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in emitted
        ), "AWS access-key substring MUST be redacted before logging"
        assert (
            "REDACTED" in emitted
        ), "the redactor marker MUST appear, proving the URL was rewritten"
        assert (
            "http://attacker.example.com/exfil?token=" not in emitted
        ), "the bare exfil URL MUST NOT be emitted verbatim"

    @pytest.mark.asyncio
    async def test_initialize_session_redacts_model_in_info_log(self, tmp_path):
        """The "ACP session created: ... (model=...)" info log MUST flow
        self._model through dual-redaction. Drive _initialize_session through
        the happy path with a URL-shaped self._model and confirm the emitted
        info log does not leak it.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        # AWS access-key-shaped substring guarantees both redactors fire (URL
        # query matches _EXFIL_PATTERNS; standalone key matches _CREDENTIAL_PATTERNS).
        client._model = "http://attacker.example.com/m?token=AKIAIOSFODNN7EXAMPLE"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _ok_session(*args, **kwargs):
            return {"sessionId": "sess-ok"}

        client._new_session_following_substitution = _ok_session  # type: ignore[assignment]
        client._capture_available_models = MagicMock()  # type: ignore[assignment]
        client._store_session_config = MagicMock()  # type: ignore[assignment]

        async def _drain():
            return None

        client._drain_notifications = _drain  # type: ignore[assignment]

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._initialize_session()

        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and "ACP session created" in c.args[0]
        ]
        assert info_calls, "expected an 'ACP session created' info log"
        # Format: logger.info("ACP session created: %s (model=%s)", sid, model_log)
        # The third positional arg is the redacted model.
        emitted_model = info_calls[0].args[2]
        # See sibling test for the design note: redactor keeps the host as a
        # diagnostic label inside "[REDACTED: suspicious URL to <host>]".
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in emitted_model
        ), "AWS access-key substring MUST be redacted before logging"
        assert (
            "REDACTED" in emitted_model
        ), "the redactor marker MUST appear, proving the URL was rewritten"
        assert (
            "http://attacker.example.com/m?token=" not in emitted_model
        ), "the bare exfil URL MUST NOT be emitted verbatim"

    @pytest.mark.asyncio
    async def test_initialize_session_acperror_redacts_substitute_model(self, tmp_path):
        """The AcpError raised on no-sessionId-after-substitution MUST flow
        self._model through dual-redaction before interpolating it into the
        message. AcpError propagates to the dashboard activity feed and Slack
        via the same path as the logger sinks, so the redaction discipline
        applies to exception messages too -- not just logger.* calls.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_with_credshaped(*args, **kwargs):
            # Helper adopted a credential-shaped substitute then gave up without
            # producing a sessionId. Drives the "even after adopting substitute
            # model X" branch where X is credential-shaped.
            client._model = "AKIAIOSFODNN7EXAMPLE"
            return {}

        client._new_session_following_substitution = _no_session_with_credshaped  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()

        msg = str(excinfo.value)
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in msg
        ), "AWS access-key-shaped substitute MUST be redacted before reaching the AcpError message"
        assert "even after adopting substitute model" in msg

    @pytest.mark.asyncio
    async def test_wait_for_response_catchall_raise_redacts_msg_error(self, tmp_path):
        """The catch-all `raise AcpError(f"JSON-RPC error: {msg.error}")` MUST
        flow msg.error through dual-redaction before f-string-interpolating it
        into the exception message.
        """
        client = AcpClient(work_dir=tmp_path)
        real_err = {
            "code": -32603,
            "message": "Internal error",
            "data": "request_id: AKIAIOSFODNN7EXAMPLE backend trace failed",
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = real_err
        msg.result = None
        msg.method = None
        msg.id = 13

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._wait_for_response(13, timeout=1.0)

        emitted = str(excinfo.value)
        assert "AKIAIOSFODNN7EXAMPLE" not in emitted, (
            "AWS access-key-shaped substring in msg.error MUST be redacted "
            "before reaching the catch-all AcpError message"
        )
        assert "JSON-RPC error" in emitted, "the catch-all AcpError prefix MUST still be present"


class TestAcpClientIsShellSignal:
    """The canonical is_shell flow: the ACP shell kind ("execute") must be
    captured at the tool_call boundary and inherited by the later
    permission_request event (which carries no reliable kind), so the dashboard
    exempts long shell command titles from the 256-char cap. Without the
    inherited kind the permission path rejects long shell commands as
    "Tool name exceeds max length 256".
    """

    def _tool_call_msg(self, kind, tool_call_id="tc-1", command="echo hi"):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_call_id,
                    "title": "Running a very long command",
                    "kind": kind,
                    "rawInput": {"command": command},
                }
            },
        )

    def _permission_msg(self, tool_call_id="tc-1", kind=None):
        from kiro_crew.acp.types import JsonRpcMessage

        tool_call = {"toolCallId": tool_call_id, "title": "x" * 400}
        # By default no "kind" on the permission payload — mirrors live
        # behaviour where the request_permission toolCall carries an empty
        # kind. Pass kind=... to exercise the payload-kind fallback branch.
        if kind is not None:
            tool_call["kind"] = kind
        return JsonRpcMessage(
            id=42,
            method="session/request_permission",
            params={
                "toolCall": tool_call,
                "options": [{"optionId": "allow_once", "name": "Allow once"}],
            },
        )

    def _refinement_msg(self, kind, tool_call_id="tc-1", command="echo hi"):
        from kiro_crew.acp.types import JsonRpcMessage

        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_call_id,
            "title": "refined title",
            "rawInput": {"command": command},
        }
        # kind is optional on a refinement; only include it when provided so
        # tests can exercise the no-clobber path (kind absent).
        if kind is not None:
            update["kind"] = kind
        return JsonRpcMessage(method="session/update", params={"update": update})

    def test_execute_kind_marks_event_is_shell(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        ev = client._extract_tool_event(self._tool_call_msg("execute"))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_non_shell_kind_not_is_shell(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        ev = client._extract_tool_event(self._tool_call_msg("edit"))
        assert ev is not None
        assert ev.is_shell is False

    def test_permission_event_inherits_is_shell_from_tool_call(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # 1. tool_call notification arrives first (kind="execute")
        client._extract_tool_event(self._tool_call_msg("execute"))
        # 2. permission request for the SAME toolCallId carries no kind
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is True  # inherited via the toolCallId cache

    def test_permission_event_non_shell_stays_false(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("edit"))
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is False

    def test_permission_event_inherits_trusted_tool_identity(self, tmp_path):
        # The client caches the trusted _meta.kiro identity (mcpServerName +
        # toolName) from the tool_call so the later permission event (which
        # carries no _meta) can rebuild mcp__<server>__<tool> for the
        # app-own-server auto-approve's per-tool governance. Without the
        # tool_name cache the permission event's tool_name is always "" and the
        # auto-approve fails closed (never fires).
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        tc = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "Doing app work",  # prose, not the canonical mcp__ form
                    "kind": "other",
                    "rawInput": {},
                    "_meta": {
                        "kiro": {
                            "toolName": "list_intakes",
                            "mcpServerName": "beehive:beehive-mcp",
                        }
                    },
                }
            },
        )
        client._extract_tool_event(tc)
        perm = client._build_permission_event(self._permission_msg())
        assert perm.tool_name == "list_intakes"
        assert perm.mcp_server_name == "beehive:beehive-mcp"

    def test_permission_event_does_not_promote_title_on_identity_cache_miss(self, tmp_path):
        """Legacy per-session transport fails closed without ``_meta.kiro``."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.trust_patterns import approval_command

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "mcp__beehive:beehive-mcp__list_intakes",
                        "kind": "other",
                        "rawInput": {},
                    }
                },
            )
        )

        perm = client._build_permission_event(self._permission_msg())

        assert perm.tool_name == ""
        assert perm.mcp_server_name == ""
        assert (
            approval_command(
                perm.tool_input,
                is_shell=perm.is_shell,
                tool_name=perm.tool_name,
                mcp_server_name=perm.mcp_server_name,
            )
            == ""
        )

    def test_structured_non_shell_reprompt_keeps_argument_provenance(self, tmp_path):
        """A repeated MCP permission retains both display and raw provenance."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.trust_patterns import approval_command

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Looking up the record",
                        "kind": "other",
                        "rawInput": {"record_id": "sensitive-record"},
                        "_meta": {
                            "kiro": {
                                "toolName": "read_record",
                                "mcpServerName": "records:primary",
                            }
                        },
                    }
                },
            )
        )

        first = client._build_permission_event(self._permission_msg())
        repeated = client._build_permission_event(self._permission_msg())

        assert first.tool_input
        assert repeated.tool_input == first.tool_input
        assert repeated.raw_tool_params == {"record_id": "sensitive-record"}
        assert (
            approval_command(
                repeated.tool_input,
                is_shell=repeated.is_shell,
                tool_name=repeated.tool_name,
                mcp_server_name=repeated.mcp_server_name,
                raw_tool_params=repeated.raw_tool_params,
            )
            == ""
        )

    @pytest.mark.parametrize("raw_input", ["/etc/secret", ["/etc/secret"]])
    def test_non_dict_non_shell_reprompt_cannot_become_durable_tool_trust(
        self, tmp_path, raw_input
    ):
        """String/list rawInput must not become argument-free after one prompt."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.trust_patterns import approval_command

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Reading a path",
                        "kind": "read",
                        "rawInput": raw_input,
                        "_meta": {"kiro": {"toolName": "read_path", "mcpServerName": "files"}},
                    }
                },
            )
        )

        first = client._build_permission_event(self._permission_msg())
        repeated = client._build_permission_event(self._permission_msg())

        assert repeated.tool_input == first.tool_input
        assert repeated.tool_input
        assert (
            approval_command(
                repeated.tool_input,
                is_shell=repeated.is_shell,
                tool_name=repeated.tool_name,
                mcp_server_name=repeated.mcp_server_name,
                raw_tool_params=repeated.raw_tool_params,
            )
            == ""
        )

    def test_end_to_end_long_shell_title_passes_validation(self, tmp_path):
        """The full regression: a 400-char shell title validates only because
        is_shell propagated from tool_call → permission → _validate_tool_name."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.dashboard.chat_utils import _validate_tool_name

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("execute"))
        perm = client._build_permission_event(self._permission_msg())
        # Mirrors the chat_runner call site.
        assert _validate_tool_name(perm.title, is_shell=perm.is_shell) == perm.title

    def test_refinement_kindless_does_not_clobber_cached_shell(self, tmp_path):
        """A refinement update that omits `kind` must NOT overwrite the
        is_shell=True cached by the initial tool_call notification (kind is
        optional on updates). This pins the no-clobber invariant."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # 1. initial tool_call carries kind="execute" → caches True
        client._extract_tool_event(self._tool_call_msg("execute"))
        # 2. refinement arrives WITHOUT a kind — must preserve the cached True
        ev = client._extract_tool_call_refinement(self._refinement_msg(None))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_refinement_with_kind_refreshes_shell_signal(self, tmp_path):
        """A refinement that DOES carry a kind refreshes the cached signal."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # initial tool_call had no shell kind (edit) → cached False
        client._extract_tool_event(self._tool_call_msg("edit"))
        # refinement now reports kind="execute" → must flip cache to True
        ev = client._extract_tool_call_refinement(self._refinement_msg("execute"))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_permission_payload_kind_is_not_trusted_on_cache_miss(self, tmp_path):
        """SECURITY (deny-by-default): a cache miss must NOT be rescued by the
        permission payload's own kind. The payload is agent/LLM-influenced, so
        trusting kind="execute" there would let a malicious agent waive the
        length cap on the very name being validated. With no preceding
        tool_call to populate the cache, is_shell must stay False even when the
        payload claims kind="execute"."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # No _extract_tool_event call → cache miss; payload tries kind="execute".
        perm = client._build_permission_event(self._permission_msg(kind="execute"))
        assert perm.is_shell is False

    def test_permission_event_does_not_pop_shell_cache(self, tmp_path):
        """The permission event must read the cached signal with .get(), not
        .pop(): a later tool_call_update refinement for the same toolCallId
        reads the same cache, so popping would make it wrongly see is_shell=
        False. The read must use .get(), not a .pop() that consumes the entry."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("execute"))
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is True
        # Entry survives → a post-permission refinement still resolves True.
        assert client._tool_call_is_shell.get("tc-1") is True
        ev = client._extract_tool_call_refinement(self._refinement_msg(None))
        assert ev is not None and ev.is_shell is True

    def test_is_shell_kind_is_none_safe(self):
        """_is_shell_kind tolerates a non-str/None kind without raising — the
        ACP payload can carry "kind": null (JSON-legal), and equality keeps the
        classifier crash-free where a `.lower()`-based predicate would not."""
        from kiro_crew.acp.client import _is_shell_kind

        assert _is_shell_kind(None) is False
        assert _is_shell_kind("") is False
        assert _is_shell_kind("edit") is False
        assert _is_shell_kind("execute") is True

    def test_permission_event_null_kind_does_not_crash(self, tmp_path):
        """A request_permission payload carrying "kind": null (JSON-legal, and
        emitted by real ACP/MCP servers) must not crash event construction, and
        the resulting title must still validate. Guards the chat-runner event
        loop against a null-kind AttributeError on the length-cap path."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.dashboard.chat_utils import _validate_tool_name

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "toolCall": {"toolCallId": "tc-null", "title": "ReadFile", "kind": None},
                "options": [{"optionId": "allow_once", "name": "Allow once"}],
            },
        )
        perm = client._build_permission_event(msg)
        # No cached tool_call → deny-by-default; short non-shell name validates.
        assert perm.is_shell is False
        assert _validate_tool_name(perm.title, is_shell=perm.is_shell) == "ReadFile"

    def test_is_shell_propagates_through_to_llm_event(self):
        """AcpProvider._to_llm_event re-wraps an event field-by-field; the
        is_shell flag must survive that copy or the dashboard (which consumes
        the re-wrapped LLMEvent) would never see it on the live ACP path."""
        from kiro_crew.acp.types import AcpEvent
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST

        src = AcpEvent(kind=EVENT_PERMISSION_REQUEST, title="x" * 400, is_shell=True)
        wrapped = AcpProvider._to_llm_event(src)
        assert wrapped.is_shell is True
        # And the default carries through as False when not a shell tool.
        not_shell = AcpProvider._to_llm_event(AcpEvent(kind=EVENT_PERMISSION_REQUEST))
        assert not_shell.is_shell is False


class TestSpawnEnvScrub:
    """The default auto/standard ACP spawn path applies the full child scrub.

    The parent-side enforcement is mandatory for raw Windows Kiro delegation;
    POSIX launchers apply the same sensitive/Python scrub inline.
    """

    @pytest.mark.asyncio
    async def test_client_spawn_scrubs_sensitive_env_on_default_auto(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:FAKE-telegram")
        monkeypatch.setenv("WECOM_BOT_ID", "FAKE-wecom-bot")
        monkeypatch.setenv("WECOM_SECRET", "FAKE-wecom-secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-FAKE")
        monkeypatch.setenv("KIROCREW_OWNER_ID", "U_FAKE_OWNER")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE-secret")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
        monkeypatch.setenv("PYTHONPATH", "/gateway/pythonpath")
        monkeypatch.setenv("PYTHONHOME", "/gateway/pythonhome")
        # AWS account identity and a benign key are not denied.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE-akid")
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

        captured: dict[str, object] = {}

        class _StopSpawn(Exception):
            pass

        async def _fake_exec(*_args, **kwargs):
            captured["env"] = kwargs.get("env")
            raise _StopSpawn()

        # **_ absorbs the environ/home the spawn path now pins, so the search and
        # the "not found" diagnostic cannot read different environments.
        monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_: "/fake/kiro")
        monkeypatch.setattr(
            acp_client,
            "wrap_argv",
            lambda argv, mode, strip_python_env=False, is_kiro_cli=None, **_kw: (
                argv,
                None,
            ),
        )
        monkeypatch.setattr(acp_client, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(acp_client, "augmented_path", lambda p: p)
        monkeypatch.setattr(acp_client, "resolve_krb5_ccname", lambda env: None)
        monkeypatch.setattr(acp_client, "_resolve_ssh_auth_sock", lambda env: None)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        client = AcpClient(sandbox_mode="auto")  # default tier
        with pytest.raises(_StopSpawn):
            await client._spawn()

        env = captured["env"]
        assert isinstance(env, dict)
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "WECOM_BOT_ID",
            "WECOM_SECRET",
            "SLACK_BOT_TOKEN",
            "KIROCREW_OWNER_ID",
            "AWS_SECRET_ACCESS_KEY",
            "PYTHONPATH",
            "PYTHONHOME",
        ):
            assert key not in env, f"{key} leaked into ACP child env"
        # The default tier leaves ~/.ssh readable, so the agent socket is kept:
        # git-over-SSH works while every other credential above stays scrubbed.
        if sys.platform == "win32":
            # Windows has no SSH_AUTH_SOCK (a named-pipe agent): never forwarded.
            assert "SSH_AUTH_SOCK" not in env, "SSH_AUTH_SOCK leaked into ACP child env"
        else:
            assert env.get("SSH_AUTH_SOCK") == "/tmp/fake-agent.sock"
        assert env.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"
        assert env.get("AWS_ACCESS_KEY_ID") == "FAKE-akid"
        assert env.get("KIROCREW_RUNTIME_PYTHON") == sys.executable


class TestSetModelRebasesContextStats:
    """A mid-session set_model must re-anchor the context-meter stats.

    Without the re-anchor set_model leaves
    last_prompt_stats untouched, so the old model's window (and its
    authoritative context_tokens_from_usage flag) survived the switch. When
    the new model streams only contextUsagePercentage metadata (kiro 2.10+),
    the stale True gated _backfill_context_window forever — pct updated but
    the token text stayed scaled to the OLD model's window.
    """

    def _client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._send_request = AsyncMock()
        return client

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    @pytest.mark.asyncio
    async def test_rebases_window_and_clears_usage_flag(self, tmp_path, monkeypatch):
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 272_000)
        client = self._client(tmp_path)
        # Old model reported authoritative counts: 100K / 1M = 10%.
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))
        assert client.last_prompt_stats.context_tokens_from_usage is True

        await client.set_model("gpt-5.6-sol")

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 272_000
        assert stats.context_used_tokens == 100_000  # transcript unchanged
        assert stats.context_pct == round(100_000 / 272_000 * 100, 1)
        # The old model's usage_update does not describe the session.
        assert stats.context_tokens_from_usage is False

    @pytest.mark.asyncio
    async def test_rebases_to_the_substituted_models_window(self, tmp_path, monkeypatch):
        """The meter describes what is serving, not what was asked for.

        A config-option advisory can substitute the model a switch actually
        got. Keying the rebase on the requested id showed that model's window
        against the substitute's token counts, so the percentage was wrong for
        exactly the sessions that were silently moved.
        """
        import kiro_crew.acp.client as c

        windows = {"gpt-5.6-sol": 272_000, "substitute-model": 128_000}
        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: mid in windows)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: windows[mid])
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        async def _substitute(method, params):
            client._last_substitution_model = "substitute-model"
            return 1

        client._send_request = _substitute

        await client.set_model("gpt-5.6-sol")

        assert client._resolved_model_id == "substitute-model"
        assert client.last_prompt_stats.context_window_tokens == 128_000

    @pytest.mark.asyncio
    async def test_post_switch_metadata_backfills_new_window(self, tmp_path, monkeypatch):
        """Load-bearing: with the rebase reverted, context_tokens_from_usage
        stays True and this metadata pct would be ignored, leaving the window
        at the OLD model's 1M forever."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 272_000)
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        await client.set_model("gpt-5.6-sol")
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 50.0},
            )
        )

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 272_000
        assert stats.context_used_tokens == 136_000  # 50% of the NEW window
        assert stats.context_pct == 50.0

    @pytest.mark.asyncio
    async def test_unknown_window_zeroes_out(self, tmp_path, monkeypatch):
        """A registry miss must not keep the old window: zero it so downstream
        consumers fall back to their own model-derived value."""
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: False)
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        await client.set_model("mystery-model")

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 0
        # The old model's pct must not survive either — it would ship in the
        # model-switch reset broadcast as a claim about the unknown window.
        assert stats.context_pct == 0.0
        assert stats.context_tokens_from_usage is False

    def test_rebase_clamps_pct_when_used_exceeds_new_window(self):
        """Shrinking the window below used must clamp pct to 100, not overflow."""
        stats = AcpPromptStats(
            context_used_tokens=500_000,
            context_window_tokens=1_000_000,
            context_pct=50.0,
            context_tokens_from_usage=True,
        )
        stats.rebase_to_window(272_000)
        assert stats.context_pct == 100.0
        assert stats.context_window_tokens == 272_000
        assert stats.context_tokens_from_usage is False


class TestCompactionResetsContextStats:
    """A completed compaction must drop the stale context-usage counts.

    Without the drop, the pre-compaction
    counts carried an authoritative ``context_tokens_from_usage=True`` flag, so
    ``_track_metadata`` refused to apply any fresh percentage and every
    ``context_usage`` broadcast re-sent the old numbers — the dashboard bar
    never moved after a compact.
    """

    def _client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        return client

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    def _compaction_msg(self, status_type):
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        return JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": status_type}, "summary": ""},
        )

    def test_completed_resets_counts_and_keeps_window(self, tmp_path):
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))
        assert client.last_prompt_stats.context_pct == 75.0

        client._handle_compaction_status(self._compaction_msg("completed"))

        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_tokens_from_usage is False
        # The model did not change — the served window still holds.
        assert stats.context_window_tokens == 200_000

    def test_failed_keeps_counts(self, tmp_path):
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))

        client._handle_compaction_status(self._compaction_msg("failed"))

        stats = client.last_prompt_stats
        assert stats.context_pct == 75.0
        assert stats.context_used_tokens == 150_000
        assert stats.context_tokens_from_usage is True

    def test_post_compaction_metadata_reapplies(self, tmp_path, monkeypatch):
        """Load-bearing: with the reset reverted, context_tokens_from_usage
        stays True and this fresh metadata percentage would be ignored,
        freezing the meter at the pre-compaction value forever."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(150_000, 200_000))

        client._handle_compaction_status(self._compaction_msg("completed"))
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 12.0},
            )
        )

        assert client.last_prompt_stats.context_pct == 12.0

    def test_backfill_prefers_kept_served_window_over_registry(self, tmp_path, monkeypatch):
        """After the compaction reset the SERVED window survives (model
        unchanged) and can differ from the registry's static entry (opus
        served at 1M vs a 200K registry row). A metadata pct must derive
        against the kept served window, not clobber it with the registry."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))  # served 1M
        client._handle_compaction_status(self._compaction_msg("completed"))

        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 5.0},
            )
        )

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 1_000_000  # served window kept
        assert stats.context_used_tokens == 50_000  # derived against it
        assert stats.context_pct == 5.0

    @pytest.mark.asyncio
    async def test_wait_for_compaction_grace_drains_post_metadata(self, tmp_path, monkeypatch):
        """Load-bearing: kiro emits the real post-compaction pct in a
        metadata frame ~1s AFTER the completed status (live-probe confirmed).
        wait_for_compaction must capture it so the dashboard broadcast
        reports accurate numbers instead of the unknown fallback."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))

        post_meta = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 5.0},
        )
        client._read_message = AsyncMock(side_effect=[self._compaction_msg("completed"), post_meta])

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        stats = client.last_prompt_stats
        assert stats.context_pct == 5.0
        assert stats.context_window_tokens == 1_000_000
        assert stats.context_used_tokens == 50_000

    @pytest.mark.asyncio
    async def test_grace_drain_skips_metadata_without_percentage(self, tmp_path, monkeypatch):
        """A credits-only metadata frame (no contextUsagePercentage) must not
        end the grace drain — the real usage frame behind it would be
        stranded and the meter would fall back to the reset state."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))

        credits_only = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"meteringUsage": [{"unit": "credit", "amount": 0.1}]},
        )
        usage_meta = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 5.0},
        )
        client._read_message = AsyncMock(
            side_effect=[self._compaction_msg("completed"), credits_only, usage_meta]
        )

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        assert client.last_prompt_stats.context_pct == 5.0
        assert client.last_prompt_stats.context_used_tokens == 50_000

    @pytest.mark.asyncio
    async def test_wait_for_compaction_without_post_metadata_leaves_reset(self, tmp_path):
        """No metadata within the grace window: the reset state stands (the
        meter shows unknown and self-corrects on the next turn)."""
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))
        # side_effect exhaustion after the completed status makes the grace
        # drain bail immediately instead of sleeping out the window.
        client._read_message = AsyncMock(side_effect=[self._compaction_msg("completed")])

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 200_000


class TestModelEntitlementPreflight:
    """An unusable model is stopped BEFORE the wire, not explained afterwards.

    A post-hoc rejection is readable and terminal. These cover the
    turn never failing in the first place: the advertised set is known at
    session/new, so a model the account cannot run has no business being sent.
    """

    @staticmethod
    def _client(advertised, model, is_claude=False):
        from kiro_crew.acp.client import ACP_BACKEND_CLAUDE, AcpClient

        client = AcpClient()
        client._session_id = "sess-1"
        client._model = model
        # _is_claude is derived from the backend seam, not settable directly.
        client._acp_backend = ACP_BACKEND_CLAUDE if is_claude else ""
        client._available_models = [{"modelId": m, "name": m} for m in advertised]

        # The refusal and the withhold first re-ask entitlement on a throwaway
        # probe process. Held to a FAILED probe here (no evidence), so these pin
        # the snapshot's own verdict and never launch a real kiro-cli -- a host
        # with one installed would otherwise answer with its own account's list.
        async def _no_probe_evidence():
            return [], 0.0

        client._probe_advertised_models = _no_probe_evidence
        return client

    def test_unadvertised_model_is_unusable(self):
        client = self._client(["claude-sonnet-4.6"], "claude-opus-4.8")
        assert client._model_is_unusable("claude-opus-4.8") is True

    def test_advertised_model_is_usable(self):
        client = self._client(["claude-sonnet-4.6", "claude-opus-4.8"], "claude-opus-4.8")
        assert client._model_is_unusable("claude-opus-4.8") is False

    def test_unknown_entitlement_allows_the_send(self):
        """Empty advertised set means "unknowable", never "nothing is allowed".

        A backend that omits ``models`` must not have every model withheld.
        """
        client = self._client([], "claude-opus-4.8")
        assert client._model_is_unusable("claude-opus-4.8") is False

    def test_match_is_case_and_space_insensitive(self):
        client = self._client(["Claude-Opus-4.8"], "claude-opus-4.8")
        assert client._model_is_unusable("  claude-opus-4.8 ") is False

    @pytest.mark.asyncio
    async def test_startup_withholds_unusable_model(self):
        """The screenshot case: a stale default the account cannot run.

        Nothing goes on the wire and the session is recorded as running the
        backend default, so the turn proceeds instead of dying three retries in.
        """
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(["claude-sonnet-4.6"], "claude-opus-4.8")
        client._resolved_model_id = "claude-sonnet-4.6"
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert sent == []
        # Not left holding the id we declined -- the warm-pool re-apply path
        # reads this and would re-offer it on every claim.
        assert client._model == DEFAULT_MODEL
        assert client._resolved_model_id == "claude-sonnet-4.6"

    @pytest.mark.asyncio
    async def test_startup_withhold_records_the_pin_as_refused(self):
        """The withheld pin never ran, so it is recorded in ``model_pin_refused``,
        the field a caller that bills or labels a turn by the pin reads."""
        client = self._client(["claude-sonnet-4.6"], "claude-opus-4.8")
        client._send_request = _record([])

        await client._apply_startup_model()

        assert client.model_pin_refused == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_startup_auto_keeps_the_backend_default(self):
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(["gpt-5.6-terra"], DEFAULT_MODEL)
        client._resolved_model_id = "gpt-5.6-terra"
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert sent == []
        assert client._resolved_model_id == "gpt-5.6-terra"

    @pytest.mark.asyncio
    async def test_startup_still_applies_a_usable_model(self):
        client = self._client(["gpt-5.6-terra", "gpt-6-astra"], "gpt-6-astra")
        client._resolved_model_id = "gpt-5.6-terra"
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert len(sent) == 1
        assert sent[0][1]["modelId"] == "gpt-6-astra"
        assert client._model == "gpt-6-astra"
        assert client._resolved_model_id == "gpt-6-astra"
        assert client.model_pin_refused == ""

    @pytest.mark.asyncio
    async def test_startup_resolves_namespaced_pin_to_advertised_spelling(self):
        """A stale `<namespace>::` qualifier on a fully served model.

        The pin was stored when a catalog advertised the qualified spelling;
        this session advertises the bare id. The wire must send the ADVERTISED
        spelling — running the model the pin names — rather than withholding
        and silently dropping to the backend default.
        """
        client = self._client(["z-ai/glm-5.3-flash"], "openrouter::z-ai/glm-5.3-flash")
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert len(sent) == 1
        assert sent[0][1]["modelId"] == "z-ai/glm-5.3-flash"
        # _model records what the session actually runs (the warm-pool re-apply
        # path reads it), so it must hold the resolved spelling, not the
        # qualified pin.
        assert client._model == "z-ai/glm-5.3-flash"

    @pytest.mark.asyncio
    async def test_startup_still_withholds_namespaced_pin_absent_when_peeled(self):
        """The resolve is not a rubber stamp: absent under BOTH spellings withholds."""
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(["z-ai/glm-5.3-flash"], "openrouter::no-such-model")
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert sent == []
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_startup_leaves_claude_backend_alone(self):
        """The claude backend advertises BARE ids while _model is prefixed.

        Comparing the two namespaces would call every legitimate model unusable,
        so that backend keeps its own session/new substitution advisory.
        """
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        applied = []

        async def _set_config_option(config_id, value):
            applied.append((config_id, value))

        client.set_config_option = _set_config_option

        await client._apply_startup_model()

        assert applied == [("model", "global.anthropic.claude-opus-4-8[1m]")]
        assert client._resolved_model_id == "global.anthropic.claude-opus-4-8[1m]"

    @pytest.mark.asyncio
    async def test_startup_attributes_config_option_substitution_to_served_model(self):
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        sent = []

        async def _send_request(method, params):
            sent.append((method, params))
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client._apply_startup_model()

        assert sent == [
            (
                "session/set_config_option",
                {
                    "sessionId": "sess-1",
                    "configId": "model",
                    "value": "global.anthropic.claude-opus-4-8[1m]",
                },
            )
        ]
        assert client._resolved_model_id == "global.anthropic.claude-sonnet-4-6[1m]"

    @pytest.mark.asyncio
    async def test_startup_override_does_not_inherit_a_stale_substitution(self):
        """An advisory belongs to the request that emitted it.

        A substitution recorded by an earlier switch survives a process reset
        or resume. Reading it after the startup override, without clearing it
        first, attributes the session to a model this dispatch never got.
        """
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"

        async def _send_request(method, params):
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            return {}  # this dispatch carries no advisory of its own

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client._apply_startup_model()

        assert client._resolved_model_id == "global.anthropic.claude-opus-4-8[1m]"
        assert client._last_substitution_model is None

    @pytest.mark.asyncio
    async def test_clean_explicit_switch_does_not_reuse_startup_substitution(self):
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        waits = 0

        async def _send_request(method, params):
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            nonlocal waits
            waits += 1
            if waits == 1:
                client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client._apply_startup_model()
        assert client._resolved_model_id == "global.anthropic.claude-sonnet-4-6[1m]"

        await client.set_model("global.anthropic.claude-opus-4-8[1m]")

        assert client._model == "global.anthropic.claude-opus-4-8[1m]"
        assert client._resolved_model_id == "global.anthropic.claude-opus-4-8[1m]"
        assert client._last_substitution_model is None

    @pytest.mark.asyncio
    async def test_explicit_switch_is_refused_not_downgraded(self):
        """An explicit pick gets an error, because silence would misreport it."""
        from kiro_crew.acp.client import AcpModelUnavailable

        client = self._client(["claude-sonnet-4.6", "claude-haiku-4.5"], "claude-sonnet-4.6")
        sent = []
        client._send_request = _record(sent)

        with pytest.raises(AcpModelUnavailable) as excinfo:
            await client.set_model("claude-opus-4.8")

        assert sent == []
        msg = str(excinfo.value)
        assert "claude-opus-4.8" in msg
        assert "not available on your account" in msg
        # The actionable part: what they CAN pick.
        assert "claude-sonnet-4.6" in msg
        assert "claude-haiku-4.5" in msg
        # The identity hint stays CONDITIONAL and read-only. This error also
        # reaches users who really are on a free tier and for whom picking an
        # advertised model is the whole fix, so it must not read as an
        # instruction to re-authenticate: `whoami` only reports which tier is
        # signed in, and no destructive step is ever named.
        assert "if you expected" in msg.lower()
        assert "kiro-cli whoami" in msg
        assert "logout" not in msg.lower()
        assert "kiro-cli login" not in msg
        # Terminal, and EXPLICITLY so -- None would send the retry layer back to
        # string-matching, which is what produced the retry rows.
        assert excinfo.value.transient is False

    @pytest.mark.asyncio
    async def test_explicit_switch_to_advertised_model_works(self):
        client = self._client(["claude-sonnet-4.6", "claude-opus-4.8"], "claude-sonnet-4.6")
        sent = []
        client._send_request = _record(sent)

        await client.set_model("claude-opus-4.8")

        assert len(sent) == 1
        assert client._model == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_explicit_switch_attributes_config_option_substitution_to_served_model(self):
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        sent = []

        async def _send_request(method, params):
            sent.append((method, params))
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client.set_model("global.anthropic.claude-opus-4-8[1m]")

        assert sent == [
            (
                "session/set_config_option",
                {
                    "sessionId": "sess-1",
                    "configId": "model",
                    "value": "global.anthropic.claude-opus-4-8[1m]",
                },
            )
        ]
        assert client._model == "global.anthropic.claude-opus-4-8[1m]"
        assert client._resolved_model_id == "global.anthropic.claude-sonnet-4-6[1m]"


def _record(sink):
    """An async _send_request stub that records calls and returns a request id."""

    async def _send_request(method, params=None):
        sink.append((method, params or {}))
        return 1

    return _send_request


class TestColdCacheModelFallback:
    """The cold-cache push twin of TestModelEntitlementPreflight.

    ``resolve_wire_model_id`` folds onto the advertised spelling only when the
    advertised cache is warm. On the first-ever session that cache is empty,
    so a prefixed ``[1m]`` id reached the wire verbatim and the adapter
    rejected it — resetting the session and dropping the user onto the default
    with no explanation (the ``Invalid value for config option model:
    global.anthropic.claude-sonnet-4-6[1m]`` report). The push helper derives
    fallback spellings from the id itself and lets the adapter judge each.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _cold_advertised_cache(self, monkeypatch):
        """Pin the cache these tests are ABOUT to the state their name claims.

        ``_apply_startup_model`` folds the id onto the advertised spelling through
        ``model_registry.resolve_wire_model_id`` before it pushes anything, and
        the advertised set is a module global another test on the same xdist
        worker may have warmed (``claude-sonnet-4-6`` advertised). Warm, the fold
        collapses ``global.anthropic.claude-sonnet-4-6[1m]`` to the served
        spelling and the adapter sees ONE push instead of the three fallbacks --
        a 1-in-5 failure in full runs. Cold is the premise, so pin it.
        """
        from kiro_crew import model_registry

        monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})

    @staticmethod
    def _claude_client(model):
        from kiro_crew.acp.client import ACP_BACKEND_CLAUDE, AcpClient

        client = AcpClient()
        client._session_id = "sess-1"
        client._model = model
        client._resolved_model_id = model
        client._acp_backend = ACP_BACKEND_CLAUDE
        return client

    @staticmethod
    def _rejecting_set_config_option(applied, accepted):
        from kiro_crew.acp.client import AcpError

        async def _set_config_option(config_id, value):
            applied.append((config_id, value))
            if value not in accepted:
                raise AcpError(f"Invalid value for config option model: {value}")

        return _set_config_option

    @pytest.mark.asyncio
    async def test_startup_falls_back_to_bare_spelling(self):
        """The reported failure: prefixed [1m] id refused, bare spelling served."""
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(
            applied, {"claude-sonnet-4-6[1m]"}
        )

        await client._apply_startup_model()

        assert applied == [
            ("model", "global.anthropic.claude-sonnet-4-6[1m]"),
            ("model", "claude-sonnet-4-6[1m]"),
        ]
        # Records the spelling that actually went on the wire, not the refused
        # one (the warm-pool re-apply path reads this field).
        assert client._model == "claude-sonnet-4-6[1m]"
        assert client._model != DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_startup_prefers_verbatim_when_accepted(self):
        """A warm cache folds before the wire: the original id is accepted as-is."""
        client = self._claude_client("claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(
            applied, {"claude-sonnet-4-6[1m]"}
        )

        await client._apply_startup_model()

        assert applied == [("model", "claude-sonnet-4-6[1m]")]
        assert client._model == "claude-sonnet-4-6[1m]"

    @pytest.mark.asyncio
    async def test_startup_all_refused_stays_on_default(self):
        """Every spelling refused = the withhold contract, not a crash."""
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(applied, set())

        await client._apply_startup_model()

        assert len(applied) == 3  # verbatim, prefix-stripped, fully bare
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_explicit_switch_refused_raises_unavailable(self):
        """set_model is an explicit pick: refusal is typed, never a silent lie."""
        from kiro_crew.acp.client import AcpModelUnavailable

        client = self._claude_client("claude-sonnet-4-6[1m]")
        client.set_config_option = self._rejecting_set_config_option([], set())

        with pytest.raises(AcpModelUnavailable):
            await client.set_model("global.anthropic.claude-sonnet-4-6[1m]")

    @pytest.mark.asyncio
    async def test_explicit_switch_sends_accepted_fallback(self):
        """An explicit pick that SOME spelling serves still lands on the model."""
        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(
            applied, {"claude-sonnet-4-6[1m]"}
        )

        await client.set_model("global.anthropic.claude-sonnet-4-6[1m]")

        assert applied[-1] == ("model", "claude-sonnet-4-6[1m]")
        assert client._model == "claude-sonnet-4-6[1m]"
        assert client._resolved_model_id == "claude-sonnet-4-6[1m]"

    @pytest.mark.asyncio
    async def test_unknown_config_option_skips_push(self):
        """No 'model' option at all (other adapter build): no spelling retry."""
        from kiro_crew.acp.client import DEFAULT_MODEL, AcpError

        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []

        async def _unknown(config_id, value):
            applied.append((config_id, value))
            raise AcpError("Unknown config option: model")

        client.set_config_option = _unknown

        await client._apply_startup_model()

        assert applied == [("model", "global.anthropic.claude-sonnet-4-6[1m]")]
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_transport_error_propagates(self):
        """A non-value-rejection failure is not ours to swallow."""
        from kiro_crew.acp.client import AcpError

        client = self._claude_client("claude-sonnet-4-6[1m]")

        async def _transport(config_id, value):
            raise AcpError("transport closed")

        client.set_config_option = _transport

        with pytest.raises(AcpError, match="transport closed"):
            await client._apply_startup_model()

    def test_candidate_order_is_derived_from_the_id(self):
        from kiro_crew.acp.client import AcpClient

        assert AcpClient._model_config_candidates("global.anthropic.claude-sonnet-4-6[1m]") == [
            "global.anthropic.claude-sonnet-4-6[1m]",
            "claude-sonnet-4-6[1m]",
            "claude-sonnet-4-6",
        ]
        # No prefix / no window marker: single candidate, nothing derived.
        assert AcpClient._model_config_candidates("claude-sonnet-4-6") == ["claude-sonnet-4-6"]


class TestMiseNodeInstallsDir:
    """ACP node resolution must honour mise's real data root.

    ``_mise_node_installs_dir`` must not hardcode ``~/.local/share/mise``,
    silently missing installs whenever ``MISE_DATA_DIR`` or ``XDG_DATA_HOME``
    relocated the data dir — while ``env.mise_data_dir`` already resolved the
    same root correctly for the build toolchain. These pin the consolidated
    behaviour for the helper and both of its consumers' entry points.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("MISE_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    def test_default_layout_is_unchanged(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert (
            client_mod._mise_node_installs_dir()
            == tmp_path / ".local" / "share" / "mise" / "installs" / "node"
        )

    def test_mise_data_dir_env_is_honoured(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
        assert (
            client_mod._mise_node_installs_dir() == tmp_path / "custom-mise" / "installs" / "node"
        )

    def test_xdg_data_home_is_honoured(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert (
            client_mod._mise_node_installs_dir() == tmp_path / "xdg" / "mise" / "installs" / "node"
        )

    @_POSIX_EXEC_PATHS_ONLY
    def test_resolve_node_for_script_under_custom_mise_data_dir(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
        version_dir = tmp_path / "custom-mise" / "installs" / "node" / "22.1.0"
        node_bin = version_dir / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)
        script = version_dir / "lib" / "node_modules" / "some-tool" / "cli.js"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\n")

        assert client_mod._resolve_node_for_script(str(script)) == str(node_bin)

    @_POSIX_EXEC_PATHS_ONLY
    def test_resolve_node_for_script_outside_mise_returns_none(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
        script = tmp_path / "elsewhere" / "cli.js"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\n")

        assert client_mod._resolve_node_for_script(str(script)) is None


class TestCompactionFailureTurnBudget:
    """A `failed` compaction must FAIL THE TURN, not hang it.

    kiro-cli can report compaction `failed` and then abandon the prompt it was
    compacting for: no session/prompt response and no end_turn ever arrive, so
    the read loop drained in silence to the caller's full prompt ceiling
    (hours) while the slot stayed occupied — the user waited it out or pressed
    Stop. The budget bounds that wait and the turn ends with
    STOP_REASON_COMPACTION_FAILED. No retry is attempted: compaction stays
    kiro-cli's, this only makes its failure fail cleanly.
    """

    def _failed_msg(self, params: dict | None = None):
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        return JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params=params if params is not None else {"status": {"type": "failed"}},
        )

    def _silent_after(self, frames: list):
        """_read_message double: drain `frames`, then stay silent forever."""

        async def _read(timeout: float = 0.0):
            return frames.pop(0) if frames else None

        return _read

    @pytest.mark.asyncio
    async def test_silent_turn_after_failure_ends_at_budget(self, tmp_path, monkeypatch):
        """Armed by the failed status, the loop stops reading at the budget and
        marks the turn so the caller can terminate it explicitly."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        # Neither existing watchdog can save this turn: no text streamed and no
        # tool dispatched, so only the 2h prompt ceiling applied before the fix.
        client._stale_eligible = False
        client._tool_dispatched = False
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after([self._failed_msg()])

        actions = []
        t0 = time.monotonic()
        async for action, msg in client._prompt_loop(req_id=1, timeout=30.0):
            actions.append(action)
            if action == "compaction":
                # What _dispatch_events does with the frame — the arming site.
                client._handle_compaction_status(msg)
        elapsed = time.monotonic() - t0

        assert actions == ["compaction"]
        assert client._compaction_failed_turn is True
        assert client._turn_done.is_set()
        # Prove the budget ended it, not the 30s outer deadline.
        assert elapsed < 5.0, f"loop ran too long ({elapsed:.2f}s) — budget did not fire"

    @pytest.mark.asyncio
    async def test_failed_status_then_silence_completes_the_stream(self, tmp_path, monkeypatch):
        """Full path, the reported hang: a `failed` status arrives, the backend
        never answers the prompt, and the stream still terminates — with the
        compaction stop reason, well inside the 30s turn ceiling."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import (
            EVENT_COMPACTION_STATUS,
            EVENT_COMPLETE,
            STOP_REASON_COMPACTION_FAILED,
            AcpEvent,
        )

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after(
            [self._failed_msg({"status": {"type": "failed", "error": "context too large"}})]
        )

        events: list[AcpEvent] = []
        t0 = time.monotonic()
        async for ev in client.stream_events("hello", timeout=30.0):
            events.append(ev)
        elapsed = time.monotonic() - t0

        assert [e.kind for e in events] == [EVENT_COMPACTION_STATUS, EVENT_COMPLETE]
        assert events[0].title == "context too large"
        assert events[-1].stop_reason == STOP_REASON_COMPACTION_FAILED
        assert elapsed < 5.0, f"turn ran too long ({elapsed:.2f}s) — the hang is back"

    @pytest.mark.asyncio
    async def test_budget_is_disarmed_without_a_failure(self, tmp_path, monkeypatch):
        """No failed status → no budget: the loop runs to its own deadline, so
        an ordinary silent turn keeps its existing watchdog behavior."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.05)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._stale_eligible = False
        client._tool_dispatched = False
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True

        t0 = time.monotonic()
        async for _action, _msg in client._prompt_loop(req_id=1, timeout=0.4):
            pass
        elapsed = time.monotonic() - t0

        assert client._compaction_failed_turn is False
        assert elapsed >= 0.35, "loop exited early — the budget fired unarmed"

    @pytest.mark.asyncio
    async def test_completed_status_disarms_the_budget(self, tmp_path):
        """A retried compaction that succeeds must clear the armed failure, or
        the next silent stretch of a healthy turn would end it."""
        client = AcpClient(work_dir=tmp_path)
        client._handle_compaction_status(self._failed_msg())
        assert client._compaction_failed_at is not None

        client._handle_compaction_status(
            self._failed_msg({"status": {"type": "completed"}, "summary": "ok"})
        )
        assert client._compaction_failed_at is None

    @pytest.mark.asyncio
    async def test_frames_after_the_failure_defer_the_budget(self, tmp_path, monkeypatch):
        """The budget measures BACKEND SILENCE: a backend that keeps sending
        frames after a failed compaction is not reaped by it."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.3)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._is_process_alive = lambda: True
        client._handle_compaction_status(self._failed_msg())

        async def _steady_stream(timeout: float = 0.0):
            # flake-ok: pre-existing 50 ms pacing in the stubbed stream; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing pacing inside the stubbed stream; tracked for a de-flake pass
            await asyncio.sleep(0.05)
            return JsonRpcMessage(method=METHOD_METADATA, params={})

        client._read_message = _steady_stream

        actions = []
        async for action, _msg in client._prompt_loop(req_id=1, timeout=0.6):
            actions.append(action)

        # Ran to its own deadline while frames kept arriving.
        assert actions and all(a == "metadata" for a in actions)
        assert client._compaction_failed_turn is False

    @pytest.mark.asyncio
    async def test_dispatch_ends_turn_with_compaction_stop_reason(self):
        """The abandoned turn terminates with the explicit stop reason instead
        of raising the generic timeout, so the runner releases the slot."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            STOP_REASON_COMPACTION_FAILED,
            AcpEvent,
        )

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # The budget fired: the loop stops reading with no `complete`.
            client._compaction_failed_turn = True
            if False:  # pragma: no cover - keeps this an async generator
                yield "complete", None

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [e.kind for e in events] == [EVENT_COMPLETE]
        assert events[0].stop_reason == STOP_REASON_COMPACTION_FAILED
        # Single-shot: the marker must not leak into the next turn.
        assert client._compaction_failed_turn is False

    @pytest.mark.asyncio
    async def test_streamed_text_still_reports_compaction_failure(self):
        """A turn that streamed text before the failure must NOT be finalized as
        a normal end_turn by the stale-turn branch — the cause is the failure."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            STOP_REASON_COMPACTION_FAILED,
            AcpEvent,
        )

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            client._stale_eligible = True
            client._compaction_failed_turn = True
            if False:  # pragma: no cover - keeps this an async generator
                yield "complete", None

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [e.kind for e in events] == [EVENT_COMPLETE]
        assert events[0].stop_reason == STOP_REASON_COMPACTION_FAILED

    @pytest.mark.asyncio
    async def test_failed_event_title_carries_the_reason(self):
        """The notice reads AcpEvent.title. kiro-cli leaves `summary` empty on
        failure, which collapsed the row to "unknown error" — the raw
        notification's own reason now rides the title instead."""
        from kiro_crew.acp.types import (
            EVENT_COMPACTION_STATUS,
            METHOD_COMPACTION_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={
                "status": {"type": "failed", "error": "context window exceeded"},
                "summary": "",
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert events[0].kind == EVENT_COMPACTION_STATUS
        assert events[0].text == "failed"
        assert events[0].title == "context window exceeded"

    @pytest.mark.asyncio
    async def test_consumer_park_is_not_charged_to_the_budget(self, tmp_path, monkeypatch):
        """A human approval parks the whole generator chain at _prompt_loop's
        single yield. That interval is CONSUMER time (mirrors the
        AcpSessionHandle park accounting): without the exclusion, the resume
        computes idle = the park length and reaps a live turn whose backend
        was only ever waiting for the approval answer."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._stale_eligible = False
        client._tool_dispatched = False
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after(
            [self._failed_msg(), JsonRpcMessage(method=METHOD_METADATA, params={})]
        )

        parked = False
        resumed_at = None
        async for action, msg in client._prompt_loop(req_id=1, timeout=30.0):
            if action == "compaction":
                client._handle_compaction_status(msg)
            elif action == "metadata" and not parked:
                # The consumer holds this frame past the WHOLE budget — the
                # shape of a human answering an approval card slowly.
                parked = True
                await asyncio.sleep(0.3)
                resumed_at = time.monotonic()
        ended_at = time.monotonic()

        # The budget still ends the (genuinely) silent turn — but measured
        # from the resume, not from the frame before the park.
        assert client._compaction_failed_turn is True
        assert parked and resumed_at is not None
        assert ended_at - resumed_at >= 0.15, (
            f"turn ended {ended_at - resumed_at:.2f}s after the consumer resumed - "
            "the park was charged to the backend-silence budget"
        )

    @pytest.mark.asyncio
    async def test_a_tool_in_flight_suspends_the_budget(self, tmp_path, monkeypatch):
        """kiro-cli can recover from a failed compaction and dispatch a tool. A
        legitimately silent long tool (a build, a spawned subagent) must NOT be
        reaped at the compaction budget — that would cancel valid work the
        tool-stall watchdog already governs on its own longer budget."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.05)
        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 30.0)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._is_process_alive = lambda: True
        client._read_message = self._silent_after([self._failed_msg()])

        async def _drive(timeout: float) -> None:
            async for action, msg in client._prompt_loop(req_id=1, timeout=timeout):
                if action == "compaction":
                    client._handle_compaction_status(msg)
                    # The turn recovers and dispatches a tool, exactly as
                    # _dispatch_events would mark it.
                    client._tool_dispatched = True

        t0 = time.monotonic()
        await _drive(0.4)
        elapsed = time.monotonic() - t0

        # Ran to its own deadline: the budget stayed suspended for the whole of
        # the tool's silence, and the turn was not marked compaction-failed.
        assert client._compaction_failed_turn is False
        assert elapsed >= 0.35, f"loop exited at {elapsed:.2f}s — the budget reaped a live tool"
        # Still armed, so the budget re-fires once the tool resolves.
        assert client._compaction_failed_at is not None

    @pytest.mark.asyncio
    async def test_the_budget_re_arms_when_the_tool_resolves(self, tmp_path, monkeypatch):
        """Suspension is not a permanent disarm: once the tool resolves and the
        backend goes silent again, the abandoned turn is still ended."""
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        monkeypatch.setattr(acp_client, "_COMPACTION_FAILED_TURN_BUDGET", 0.1)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._is_process_alive = lambda: True
        # A failed status, then one more frame standing in for the tool's own
        # traffic — after which the backend goes silent for good.
        client._read_message = self._silent_after(
            [self._failed_msg(), JsonRpcMessage(method=METHOD_METADATA, params={})]
        )

        t0 = time.monotonic()
        async for action, msg in client._prompt_loop(req_id=1, timeout=30.0):
            if action == "compaction":
                client._handle_compaction_status(msg)
                client._tool_dispatched = True
            elif action == "metadata":
                # The tool resolved (what _dispatch_events does on its result).
                client._tool_dispatched = False
        elapsed = time.monotonic() - t0

        assert client._compaction_failed_turn is True
        assert elapsed < 5.0, f"loop ran {elapsed:.2f}s — the budget did not re-arm"


class TestCompactionFailureDetail:
    """`compaction_failure_detail` is what stops the notice collapsing to
    "unknown error": it prefers a named reason and otherwise surfaces the raw
    shape, redacted, because it lands on a chat row."""

    def test_prefers_a_named_reason(self):
        from kiro_crew.acp.client import compaction_failure_detail

        assert (
            compaction_failure_detail({"status": {"type": "failed", "reason": "too large"}})
            == "too large"
        )

    def test_reads_a_nested_error_object(self):
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail(
            {"status": {"type": "failed"}, "error": {"message": "throttled"}}
        )
        assert detail == "throttled"

    def test_falls_back_to_the_raw_shape(self):
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail({"status": {"type": "failed"}})
        assert "no reason reported" in detail
        assert "failed" in detail

    def test_is_bounded_and_redacted(self):
        from kiro_crew.acp.client import _COMPACTION_DETAIL_MAX_CHARS, compaction_failure_detail

        detail = compaction_failure_detail({"status": {"type": "failed", "error": "x" * 5000}})
        assert len(detail) == _COMPACTION_DETAIL_MAX_CHARS

        secret = compaction_failure_detail(
            {"status": {"type": "failed", "error": "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE"}}
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in secret

    def test_rejects_a_placeholder_reason(self):
        """A named reason of "error" is not a reason. KAS's summarization_failed
        frame carried exactly that, which rendered as "Compaction failed: error"
        — no cause on the row and nothing to grep server-side. Falling through
        to the raw shape is strictly more evidence than the word."""
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail({"kind": "summarization_failed", "error": "error"})
        assert detail != "error"
        assert "no reason reported" in detail

    def test_prefers_the_user_facing_sentence_over_the_machine_reason(self):
        """The nested pair is the shape KAS reports a throttle in. The machine
        reason identifies the fault; the sentence is the one that tells the
        reader what to do about it, so the sentence wins."""
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail(
            {
                "kind": "summarization_failed",
                "name": "ModelThrottleError",
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE", "httpStatusCode": 500},
                "userFacingSessionErrorMessage": "The model is experiencing high traffic.",
            }
        )
        assert detail == "The model is experiencing high traffic."


class TestKasSummarizationFailureLogging:
    """The KAS branch logs the WHOLE frame so the next failure is debuggable from
    our own logs. That frame carries ``conversationSummary`` — backend-echoed,
    conversation-derived text — so the dump has to be redacted, or a credential a
    user pasted is persisted into gateway.log by the very line added to make the
    failure diagnosable."""

    @staticmethod
    def _bare_handle():
        from kiro_crew.acp.session_handle import AcpSessionHandle

        handle = AcpSessionHandle.__new__(AcpSessionHandle)
        handle._compaction_failed_at = None
        handle.last_compaction_transient = False
        handle._session_id = "sess-test"
        return handle

    def test_the_frame_is_redacted_before_it_reaches_the_log(self, caplog):
        import logging

        handle = self._bare_handle()
        secret = "AKIAIOSFODNN7EXAMPLE"
        update = {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "kiro": {
                    "kind": "summarization_failed",
                    "reason": "MODEL_TEMPORARILY_UNAVAILABLE",
                    "conversationSummary": (
                        f"The user pasted aws_secret_access_key={secret} while debugging."
                    ),
                }
            },
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
            events = handle._handle_kas_session_info(update)

        assert any("KAS summarization failed" in r.getMessage() for r in caplog.records)
        assert secret not in caplog.text
        # The verdict still lands: redacting the log must not cost the classification.
        assert handle.last_compaction_transient is True
        assert events and events[0].text == "failed"
