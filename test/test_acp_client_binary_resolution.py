"""Tests for ACP client: locating the backend binaries: the vendored claude adapter, the claude
executables and mise.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _POSIX_EXEC_PATHS_ONLY

from conftest import cap_node_module_walk
from kiro_crew.acp.client import (
    _CLAUDE_ACP_PKG_ENTRY,
    _resolve_vendored_claude_acp,
    _vendored_acp_roots,
)


class TestVendoredClaudeAcp:
    """Resolve the vendored claude-agent-acp adapter (no npm/network)."""

    def _make_vendored(self, root: Path, *, with_deps: bool = True) -> Path:
        """Create a fake vendored adapter under *root*.

        With *with_deps* (default) also creates the hoisted dependency marker
        ``@agentclientprotocol/sdk`` so the completeness guard accepts it.
        """
        from kiro_crew.acp.client import _CLAUDE_ACP_DEP_MARKER

        entry = root / _CLAUDE_ACP_PKG_ENTRY
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake adapter\n", encoding="utf-8")
        if with_deps:
            (root / _CLAUDE_ACP_DEP_MARKER).mkdir(parents=True, exist_ok=True)
        return entry

    def test_finds_vendored_in_pkg_vendor_dir(self, tmp_path, monkeypatch):
        # Toolbox/pip layout: <pkg_dir>/_vendor/node_modules/<pkg>/dist/index.js.
        # Inject a fake pkg_dir under an isolated home so the real workspace
        # (sibling-website detection / KIROCREW_PROJECT_DIR) is not consulted.
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(pkg_dir / "_vendor" / "node_modules")
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_finds_vendored_under_project_dir(self, tmp_path, monkeypatch):
        # KIROCREW_PROJECT_DIR/node_modules holds the adapter; pkg_dir has none.
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(tmp_path / "proj" / "node_modules")
        (tmp_path / "proj").mkdir(exist_ok=True)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        # Isolated pkg_dir with no _vendor and a project dir with no adapter.
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) is None

    def test_skips_incomplete_copy_missing_deps(self, tmp_path, monkeypatch):
        # Regression: an entry script whose dependency Node could not import must
        # be rejected (it would crash with ERR_MODULE_NOT_FOUND @agentclientprotocol/sdk),
        # falling through to a complete copy under KIROCREW_PROJECT_DIR.
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        # Incomplete copy in _vendor (entry only, no deps) — must be skipped. The
        # dependency walk runs to the filesystem root like Node's; cap it at tmp_path
        # so a node_modules the host keeps above the temp root cannot complete it.
        self._make_vendored(pkg_dir / "_vendor" / "node_modules", with_deps=False)
        cap_node_module_walk(monkeypatch, tmp_path)
        # Complete copy in the project dir — must win.
        (tmp_path / "proj").mkdir()
        good = self._make_vendored(tmp_path / "proj" / "node_modules")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(good)

    def test_roots_include_pkg_vendor_dir(self):
        # The toolbox-bundle vendor location must always be the first candidate.
        roots = _vendored_acp_roots()
        assert roots[0].name == "node_modules" and roots[0].parent.name == "_vendor"


class TestResolveClaudeAcpBin:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _resolve_claude_acp_bin

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(bin_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        argv, _search_path = _resolve_claude_acp_bin()
        assert argv is not None
        assert str(bin_path) in argv

    @_POSIX_EXEC_PATHS_ONLY
    def test_path_lookup(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(bin_path) if name == "claude-agent-acp" else None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv is not None
        assert str(bin_path) in argv

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_which_preferred(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        script = tmp_path / "bin" / "claude-agent-acp"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: str(script))
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv == [str(script)]

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_installed_script_resolves_node(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _resolve_claude_acp_bin

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "20.18.0"
        node_bin = mise_node / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)
        script = (
            mise_node
            / "lib"
            / "node_modules"
            / "@agentclientprotocol"
            / "claude-agent-acp"
            / "dist"
            / "index.js"
        )
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        argv, _search_path = _resolve_claude_acp_bin()
        assert argv == [str(node_bin), str(script.resolve())]

    def test_non_executable_script_falls_back_to_path_node(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)

        script = tmp_path / "node_modules" / "claude-agent-acp" / "dist" / "index.js"
        script.parent.mkdir(parents=True)
        script.write_text("console.log('hi')\n")
        script.chmod(0o644)  # NOT executable

        node_bin = tmp_path / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(node_bin) if name == "node" else None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv == [str(node_bin), str(script.resolve())]

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_glob_fallback(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "22.1.0"
        bin_dir = mise_node / "bin"
        bin_dir.mkdir(parents=True)
        acp_script = bin_dir / "claude-agent-acp"
        acp_script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        acp_script.chmod(0o755)
        node_bin = bin_dir / "node"
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        argv, _search_path = client_mod._resolve_claude_acp_bin()
        assert argv == [str(node_bin), str(acp_script.resolve())]

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        argv, search_path = client_mod._resolve_claude_acp_bin()
        assert argv is None
        assert search_path == client_mod.augmented_path(os.environ.get("PATH", ""))


class TestResolveClaudeCodeExecutable:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        exe = tmp_path / "claude"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(exe))
        # mise/PATH must NOT be consulted when the override is a real file.
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/should/not/win")
        assert client_mod._resolve_claude_code_executable() == str(exe)

    def test_env_override_ignored_when_missing(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(tmp_path / "nope"))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None

    def test_mise_preferred_over_path(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/mise/bin/claude")
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: "/usr/bin/claude")
        assert client_mod._resolve_claude_code_executable() == "/mise/bin/claude"

    @_POSIX_EXEC_PATHS_ONLY
    def test_path_lookup(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: "/srv/u/.toolbox/bin/claude" if name == "claude" else None,
        )
        assert client_mod._resolve_claude_code_executable() == "/srv/u/.toolbox/bin/claude"

    def test_none_when_absent(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None


class TestMiseWhich:
    def test_returns_path_on_success(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        script = tmp_path / "claude-agent-acp"
        script.write_text("#!/usr/bin/env node\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=str(script) + "\n"))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") == str(script)

    def test_returns_none_when_mise_not_installed(self, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(client_mod.shutil, "which", lambda name: None)
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_nonzero_exit(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_timeout(self, tmp_path, monkeypatch):
        import subprocess

        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_sub = MagicMock()
        mock_sub.run = MagicMock(side_effect=subprocess.TimeoutExpired("mise", 5))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        monkeypatch.setattr(client_mod, "subprocess_mod", mock_sub)
        assert _mise_which("claude-agent-acp") is None
