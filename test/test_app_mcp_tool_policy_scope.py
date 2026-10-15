"""The managed tool policy applies to Kiro Crew's own servers, not to an app's.

The reported defect: a user-installed app serves its own tools
through ``mcp_shared.run_mcp_stdio_loop``. On the shared ACP runtime a server the
agent spec declares is never handed a session token, so the loop's policy read
answered ``identity_unattestable`` and every ``tools/call`` was refused -- for a
policy that names only managed tools and that the app's spec does not even set.
``serves_managed_tools`` decides by where the handler's code lives, and the loop
skips the read for a handler outside the package.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import sys

from test_mcp_shared import _LoopHarness, _tools_call

import kiro_crew.mcp_shared as mcp_shared

APP_SOURCE = """
def call_tool(name, args):
    return "app:" + name


def list_tools():
    return [{"name": "app_read_state"}]
"""


def _app_module(tmp_path, monkeypatch):
    """Import a stand-in app server module from a directory outside the package."""
    path = tmp_path / "app_backend" / "mcp_server.py"
    path.parent.mkdir()
    path.write_text(APP_SOURCE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("kc_test_app_mcp_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class TestServesManagedTools:
    def test_a_handler_in_the_package_is_managed(self):
        assert mcp_shared.serves_managed_tools(mcp_shared.respond) is True

    def test_a_partial_or_bound_method_of_package_code_is_managed(self):
        assert mcp_shared.serves_managed_tools(functools.partial(mcp_shared.respond, 1)) is True
        harness_method = mcp_shared.ToolPolicy(frozenset(), "").include_for
        assert mcp_shared.serves_managed_tools(harness_method) is True

    def test_a_handler_outside_the_package_is_not_managed(self, tmp_path, monkeypatch):
        app = _app_module(tmp_path, monkeypatch)
        assert mcp_shared.serves_managed_tools(app.call_tool) is False
        assert mcp_shared.serves_managed_tools(functools.partial(app.call_tool, "x")) is False

    def test_a_cross_drive_handler_is_not_managed(self, tmp_path, monkeypatch):
        """A ValueError from commonpath (different Windows drives) does not count as managed."""
        app = _app_module(tmp_path, monkeypatch)

        def _raises_value_error(paths):
            raise ValueError("Paths don't have the same drive")

        monkeypatch.setattr(os.path, "commonpath", _raises_value_error)
        assert mcp_shared.serves_managed_tools(app.call_tool) is False

    def test_a_handler_with_no_code_location_keeps_the_policy(self):
        assert mcp_shared.serves_managed_tools(len) is True
        generated = eval("lambda name, args: 'ok'")  # noqa: S307 -- code with no file
        assert mcp_shared.serves_managed_tools(generated) is True


class TestAppServerSkipsThePolicyRead:
    def test_an_app_tool_runs_when_no_identity_can_be_attested(self, tmp_path, monkeypatch):
        app = _app_module(tmp_path, monkeypatch)
        harness = _LoopHarness(
            monkeypatch, app.call_tool, list_tools_fn=app.list_tools, managed=False
        )
        reads: list = []

        def _unattestable(*args, **kwargs):
            reads.append(args)
            return mcp_shared.ToolPolicy(frozenset(), "identity_unattestable")

        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", _unattestable)
        try:
            harness.send(_tools_call(1, "app_read_state"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "app:app_read_state" in body
            assert "identity_unattestable" not in body
            assert reads == []
        finally:
            harness.close()

    def test_a_managed_tool_still_refuses_on_the_same_reason(self, monkeypatch):
        ran: list[str] = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattestable"),
        )
        try:
            harness.send(_tools_call(2, "memory_recall"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            assert "identity_unattestable" in json.dumps(harness.responses[0][1])
        finally:
            harness.close()
