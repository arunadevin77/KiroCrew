"""``test/conftest.py`` keeps an unstubbed ``mcp_core`` request off a developer's gateway.

The autouse ``gateway_posts`` fixture records ``mcp_core._post`` and wraps the one
transport every ``mcp_core`` helper sends through. Each test here stands a listener
up on 127.0.0.1 and makes it the address an unconfigured client resolves to, so a
request that slipped past the guard lands on the stand-in, never on a real gateway.

The helpers are taken from ``mcp_core`` when this module is collected, before any
fixture runs. That is exactly what a module holds after
``from kiro_crew.mcp_core import _get, _post`` (``mcp_panel``, ``mcp_work`` and
others), so the ``_post`` driven here is the real one, not the recorder.
"""

from __future__ import annotations

import http.server
import inspect
import json
import pathlib
import socketserver
import threading

import pytest

from kiro_crew import mcp_core, port_resolution

_SENDERS = {
    name: fn
    for name, fn in vars(mcp_core).items()
    if inspect.isfunction(fn)
    and fn.__module__ == mcp_core.__name__
    and "_send" in fn.__code__.co_names
}

_HELPERS = ("_delete", "_get", "_patch", "_post", "_put")

_METHOD = {"_delete": "DELETE", "_get": "GET", "_patch": "PATCH", "_post": "POST", "_put": "PUT"}


class _Record(http.server.BaseHTTPRequestHandler):
    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.seen.append((self.command, self.path))
        body = json.dumps({"answered_by": self.server.name}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_DELETE = do_GET = do_PATCH = do_POST = do_PUT = _answer

    def log_message(self, format: str, *args: object) -> None:
        return None


class _StandIn(http.server.ThreadingHTTPServer):
    """A loopback listener that records every request reaching it and answers 200."""

    daemon_threads = True

    def __init__(self, name: str) -> None:
        super().__init__(("127.0.0.1", 0), _Record)
        self.name = name
        self.seen: list[tuple[str, str]] = []

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves the host name between bind() and
        # listen(); a loopback stand-in is reached by address and needs no name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    @property
    def port(self) -> int:
        return int(self.server_address[1])


@pytest.fixture
def stand_in():
    started: list[tuple[_StandIn, threading.Thread]] = []

    def _start(name: str) -> _StandIn:
        server = _StandIn(name)
        assert server.server_address[0] == "127.0.0.1", server.server_address
        thread = threading.Thread(target=server.serve_forever, name=f"stand-in-{name}")
        thread.daemon = True
        thread.start()
        started.append((server, thread))
        return server

    yield _start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        assert not thread.is_alive(), f"stand-in {server.name!r} did not stop"


@pytest.fixture
def unconfigured_client(monkeypatch):
    """The client as an unconfigured test sees it, minus identity and retry pauses.

    No exported port, nothing cached, no session key. The refused-connection retry
    re-proves the target before every re-dial and never dials a no-evidence default,
    so emptying its pause schedule removes only the sleeps.
    """
    monkeypatch.delenv("KIROCREW_PORT", raising=False)
    monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
    for name in ("_API_PORT", "_API", "_API_UNIX_SOCKET"):
        monkeypatch.setattr(mcp_core, name, None)
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(mcp_core, "_REFUSED_RETRY_BACKOFFS", ())


def _suite_conftest(request):
    """The ``test/conftest.py`` module pytest loaded, found by its file."""
    path = pathlib.Path(__file__).with_name("conftest.py").resolve()
    for plugin in request.config.pluginmanager.get_plugins():
        source = getattr(plugin, "__file__", None)
        if source and pathlib.Path(source).resolve() == path:
            return plugin
    raise AssertionError(f"{path} is not loaded as a conftest plugin")


def _assert_refused(answer: dict, port: int) -> None:
    assert answer.get("refused") is True, answer
    assert "Connection refused by test/conftest.py" in answer.get("error", ""), answer
    assert f"to port {port}," in answer["error"], answer


def test_every_mcp_core_function_that_sends_is_driven_here():
    """A new ``mcp_core`` function that calls ``_send`` fails here until it is listed."""
    assert sorted(_SENDERS) == list(_HELPERS)
    assert all(fn.__module__ == "kiro_crew.mcp_core" for fn in _SENDERS.values())


@pytest.mark.parametrize("helper", _HELPERS)
def test_an_unstubbed_helper_is_refused_on_the_default_port(
    helper, stand_in, unconfigured_client, monkeypatch
):
    """The default port answers like a CI runner's, and the test's own server still answers.

    The second call is the positive control: the same helper, pointed at a server the
    test started, reaches it. It also proves the stand-in on the default port was up
    and recording when the first call left it untouched.
    """
    gateway = stand_in("default-gateway")
    monkeypatch.setattr(port_resolution, "_DEFAULT_PORT", gateway.port)
    path = f"/api/guard-check/{helper.lstrip('_')}"

    _assert_refused(_SENDERS[helper](path), gateway.port)
    assert gateway.seen == []

    own = stand_in("own-server")
    monkeypatch.setattr(mcp_core, "_API", f"http://127.0.0.1:{own.port}")
    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", "")

    assert _SENDERS[helper](path) == {"answered_by": "own-server"}
    assert own.seen == [(_METHOD[helper], path)]
    assert gateway.seen == []


def test_an_unstubbed_helper_is_refused_on_the_port_the_shell_exports(
    request, stand_in, unconfigured_client, monkeypatch
):
    """``KIROCREW_PORT`` exported by the shell that started pytest is a developer's gateway too."""
    default = stand_in("default-gateway")
    exported = stand_in("exported-gateway")
    monkeypatch.setattr(port_resolution, "_DEFAULT_PORT", default.port)
    monkeypatch.setattr(
        _suite_conftest(request), "_EXPORTED_GATEWAY_PORTS", frozenset({exported.port})
    )
    monkeypatch.setenv("KIROCREW_PORT", str(exported.port))

    _assert_refused(_SENDERS["_get"]("/api/guard-check/exported"), exported.port)
    assert exported.seen == []
    assert default.seen == []


def test_the_exported_ports_come_from_both_client_variables(request, monkeypatch):
    conftest_module = _suite_conftest(request)
    monkeypatch.setenv("KIROCREW_PORT", "6123")
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "6124")
    assert conftest_module._exported_gateway_ports() == {6123, 6124}

    monkeypatch.setenv("KIROCREW_BOUND_PORT", "not-a-port")
    monkeypatch.delenv("KIROCREW_PORT")
    assert conftest_module._exported_gateway_ports() == frozenset()
