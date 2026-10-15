"""Launch resolution uses a managed probe directory outside agent-writable overlays."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import launch_resolve, rewriter

ENFORCE_LAUNCH_APPROVAL = True


def test_launch_probe_ignores_a_planted_overlay_symlink(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    real_overlay = home / "mcp-gateway" / "agents"
    old_scratch = real_overlay.parent / ".launch-probe-planted"
    old_scratch.mkdir(parents=True)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (old_scratch / "agents").symlink_to(attacker, target_is_directory=True)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(old_scratch))
    monkeypatch.setattr(
        launch_resolve,
        "rewrite_kwargs",
        lambda _cfg, _stubs: {"overlay_dir": real_overlay},
    )
    observed: dict[str, Path] = {}
    expected_root = home / "run" / "mcp-tmp"

    def fake_rewrite_agents(**kwargs):
        overlay = Path(kwargs["overlay_dir"])
        observed["overlay"] = overlay
        overlay.relative_to(expected_root)
        overlay.mkdir(parents=True)
        (overlay / "probe.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(rewriter, "rewrite_agents", fake_rewrite_agents)
    cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=frozenset()))

    assert "srv" not in launch_resolve.resolve_launches(["srv"], cfg=cfg, refresh_specs=False)
    assert list(attacker.iterdir()) == []
    assert not observed["overlay"].parent.exists()


def _probe_world(tmp_path, monkeypatch, fake_rewrite_agents):
    home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(
        launch_resolve,
        "rewrite_kwargs",
        lambda _cfg, _stubs: {"source_dir": agents, "overlay_dir": home / "overlay"},
    )
    monkeypatch.setattr(rewriter, "rewrite_agents", fake_rewrite_agents)
    return agents.parent, SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=frozenset()))


def _capture(approvals, name, tag):
    from kiro_crew.mcp_gateway.launch_approval import env_fingerprint, launch_fingerprint

    env_hash = env_fingerprint({})
    approvals.admit(
        name,
        launch_fingerprint(f"/opt/{tag}", []),
        launch=(f"/opt/{tag}", []),
        env={},
        derived_env_hash=env_hash,
    )


def test_a_pass_that_read_every_source_is_complete(tmp_path, monkeypatch):
    def fake(**kwargs):
        _capture(kwargs["approvals"], "srv", "srv")

    kiro, cfg = _probe_world(tmp_path, monkeypatch, fake)
    (kiro / "settings").mkdir()
    (kiro / "settings" / "mcp.json").write_text("{}", encoding="utf-8")

    got = launch_resolve.resolve_launches(["srv"], cfg=cfg, refresh_specs=False)

    assert got.complete is True and "srv" in got and not got.over_cap


def test_a_kept_agent_overlay_makes_the_pass_incomplete(tmp_path, monkeypatch):
    """An unread agent spec keeps its old overlay: its launches are not in the result."""

    def fake(**kwargs):
        _capture(kwargs["approvals"], "srv", "srv")
        kwargs["approvals"].live_incomplete = True

    _kiro, cfg = _probe_world(tmp_path, monkeypatch, fake)

    got = launch_resolve.resolve_launches(["srv"], cfg=cfg, refresh_specs=False)

    assert got.complete is False


@pytest.fixture
def real_home(tmp_path, monkeypatch):
    """A hermetic config, crew home and agents dir the real rewriter resolves from."""
    import kiro_crew.config.loader as loader_mod
    import kiro_crew.config.paths as paths_mod
    from kiro_crew.config.paths import kiro_agents_dir

    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(loader_mod, "config_path", lambda: cfg)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: tmp_path / "kiro" / "agents")
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    # The share target lives in an agent spec; the global file declares another.
    entry = {"command": sys.executable, "args": ["-c", "pass"]}
    (agents / "a.json").write_text(json.dumps({"name": "a", "mcpServers": {"srv": entry}}))
    settings = agents.parent / "settings" / "mcp.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"mcpServers": {"glob": {"command": sys.executable, "args": ["-c", "1"]}}})
    )
    return agents, settings


def _fail_reads_of(monkeypatch, target, exc):
    """``read_text`` of *target* raises *exc*; every other read, and ``read_bytes``, works."""
    real = Path.read_text

    def read_text(self, *a, **k):
        if self == target:
            raise exc
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_a_real_pass_over_readable_sources_is_complete(real_home):
    got = launch_resolve.resolve_launches(["srv", "glob"], refresh_specs=False)

    assert got.complete is True
    assert "srv" in got and "glob" in got


def test_a_global_mcp_json_read_failing_inside_the_pass_makes_it_incomplete(real_home, monkeypatch):
    """The file reads fine before and after; only the rewriter's own read fails."""
    _agents, settings = real_home
    _fail_reads_of(monkeypatch, settings, PermissionError(13, "denied"))
    assert settings.read_bytes()  # a separate read still succeeds

    got = launch_resolve.resolve_launches(["srv", "glob"], refresh_specs=False)

    assert got.complete is False
    assert "glob" not in got


@pytest.mark.parametrize(
    "content", [b"{not json", b"\xff\xfe\x00bad"], ids=["malformed", "undecodable"]
)
def test_a_global_mcp_json_with_bad_content_stays_complete(real_home, content):
    """Bad content injects nothing at runtime too, and keeps no old overlay."""
    _agents, settings = real_home
    settings.write_bytes(content)

    got = launch_resolve.resolve_launches(["srv", "glob"], refresh_specs=False)

    assert got.complete is True
    assert "srv" in got and "glob" not in got


def test_an_agent_spec_read_failing_inside_the_pass_makes_it_incomplete(real_home, monkeypatch):
    from kiro_crew import agent_discovery

    agents, _settings = real_home
    real = agent_discovery.read_agent_spec_strict

    def read(path, *a, **k):
        if Path(path) == agents / "a.json":
            raise PermissionError(13, "denied")
        return real(path, *a, **k)

    monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", read)

    got = launch_resolve.resolve_launches(["srv"], refresh_specs=False)

    assert got.complete is False and "srv" not in got


def test_a_missing_agents_dir_is_incomplete(real_home, monkeypatch):
    """The runtime keeps every published overlay on this path; the probe sees none."""
    import kiro_crew.config.paths as paths_mod

    agents, _settings = real_home
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: agents.parent / "gone")

    got = launch_resolve.resolve_launches(["srv"], refresh_specs=False)

    assert got.complete is False


def test_a_name_past_the_launch_cap_is_reported_not_resolved(tmp_path, monkeypatch):
    from kiro_crew.mcp_gateway import launch_approval

    def fake(**kwargs):
        for i in range(launch_approval._MAX_SERVER_LAUNCHES + 1):
            _capture(kwargs["approvals"], "big", f"big{i}")
        _capture(kwargs["approvals"], "small", "small")

    _kiro, cfg = _probe_world(tmp_path, monkeypatch, fake)

    got = launch_resolve.resolve_launches(["big", "small"], cfg=cfg, refresh_specs=False)

    assert "big" in got.over_cap and "big" not in got
    assert "small" in got and got.complete is True


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_directory_listing_failure_is_incomplete_then_recovers(real_home, monkeypatch, failure):
    """Stat succeeds; the actual enumeration fails, then the same files resolve."""
    import os

    agents, _settings = real_home
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.mcp_gateway.launch_approval import LaunchApprovals

    kwargs = launch_resolve.rewrite_kwargs(KiroCrewConfig.load(), frozenset({"srv", "glob"}))
    overlay = kwargs["overlay_dir"]
    overlay.mkdir(parents=True, exist_ok=True)
    previous = overlay / "previous.json"
    previous.write_text('{"mcpServers": {}}', encoding="utf-8")
    scandir = os.scandir
    attempts = []

    def scan(path):
        if path == agents:
            attempts.append(path)
            raise failure(13, "listing unavailable")
        return scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scan)
        assert agents.is_dir()
        got = launch_resolve.resolve_launches(["srv", "glob"], refresh_specs=False)
        approvals = LaunchApprovals(probe=True)
        rewriter.rewrite_agents(**kwargs, approvals=approvals)
        assert previous.read_text(encoding="utf-8") == '{"mcpServers": {}}'
        assert approvals.live_incomplete
    recovered = launch_resolve.resolve_launches(["srv", "glob"], refresh_specs=False)

    assert attempts
    assert got.complete is False
    assert recovered.complete is True
    assert set(recovered) == {"srv", "glob"}
