"""Tests for _setup_electron desktop app installation."""

from unittest.mock import MagicMock, patch

from kiro_crew import cli_setup
from kiro_crew.cli_setup import (
    _find_prebuilt_app,
    _setup,
    _setup_desktop_app_step,
    _setup_electron,
)


class TestSetupElectronPlatformGuard:
    """_setup_electron exits early on non-macOS platforms."""

    def test_skips_on_linux(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Linux")
        _setup_electron()
        out = capsys.readouterr().out
        assert "only available on macOS" in out

    def test_skips_on_windows(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Windows")
        _setup_electron()
        out = capsys.readouterr().out
        assert "only available on macOS" in out


class TestSetupElectronNodeGuard:
    """_setup_electron exits early when Node.js is missing."""

    def test_no_node(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: None)
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: tmp_path)
        _setup_electron()
        out = capsys.readouterr().out
        assert "Node.js not found" in out

    def test_no_node_without_sources_skips_node_check(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: None)
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: None)
        _setup_electron()
        out = capsys.readouterr().out
        assert "Desktop app sources not found" in out
        assert "Node.js not found" not in out


class TestSetupElectronSourcesMissing:
    """_setup_electron returns gracefully (no clone) when sources are absent."""

    def test_sources_not_found_returns(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: None)
        monkeypatch.setattr("kiro_crew.cli_setup._find_prebuilt_app", lambda: None)
        run_called = {"n": 0}
        monkeypatch.setattr(
            "kiro_crew.cli_setup.subprocess.run",
            lambda *a, **kw: run_called.__setitem__("n", run_called["n"] + 1),
        )
        _setup_electron()
        out = capsys.readouterr().out
        assert "Desktop app sources not found" in out
        # Must NOT shell out to git/npm when sources are missing.
        assert run_called["n"] == 0


class TestSetupElectronBuild:
    """_setup_electron builds and installs the Electron app."""

    def _electron_dir(self, tmp_path, monkeypatch):
        electron_dir = tmp_path / "website" / "electron"
        electron_dir.mkdir(parents=True)
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: electron_dir)
        return electron_dir

    def test_npm_install_failure_aborts(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        self._electron_dir(tmp_path, monkeypatch)

        mock_fail = MagicMock(returncode=1, stdout="", stderr="npm ERR!")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_fail)
        _setup_electron()
        out = capsys.readouterr().out
        assert "npm install failed" in out

    def test_electron_builder_failure_aborts(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        self._electron_dir(tmp_path, monkeypatch)

        call_count = {"n": 0}

        def _side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="Error: build failed")

        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", _side_effect)
        _setup_electron()
        out = capsys.readouterr().out
        assert "Electron build failed" in out

    def test_app_not_found_after_build(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "arm64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        self._electron_dir(tmp_path, monkeypatch)

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        out = capsys.readouterr().out
        assert "KiroCrew.app not found" in out

    def test_successful_install(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "arm64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        electron_dir = self._electron_dir(tmp_path, monkeypatch)
        app_dir = electron_dir / "dist" / "mac-arm64" / "KiroCrew.app" / "Contents"
        app_dir.mkdir(parents=True)
        (app_dir / "Info.plist").write_text("<plist></plist>")

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        out = capsys.readouterr().out
        assert "installed to ~/Applications" in out
        assert (tmp_path / "Applications" / "KiroCrew.app" / "Contents" / "Info.plist").exists()

    def test_replaces_existing_app(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "arm64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        electron_dir = self._electron_dir(tmp_path, monkeypatch)
        app_dir = electron_dir / "dist" / "mac-arm64" / "KiroCrew.app" / "Contents"
        app_dir.mkdir(parents=True)
        (app_dir / "Info.plist").write_text("<new>")

        old_app = tmp_path / "Applications" / "KiroCrew.app" / "Contents"
        old_app.mkdir(parents=True)
        (old_app / "Info.plist").write_text("<old>")

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        installed = tmp_path / "Applications" / "KiroCrew.app" / "Contents" / "Info.plist"
        assert installed.read_text(encoding="utf-8") == "<new>"

    def test_x86_arch_fallback(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "x86_64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        electron_dir = self._electron_dir(tmp_path, monkeypatch)
        app_dir = electron_dir / "dist" / "mac" / "KiroCrew.app" / "Contents"
        app_dir.mkdir(parents=True)
        (app_dir / "Info.plist").write_text("<x86>")

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        out = capsys.readouterr().out
        assert "installed to ~/Applications" in out


class TestSetupElectronOnly:
    """kirocrew setup --electron-only dispatches correctly."""

    def test_electron_only_calls_setup_electron(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup._setup_electron", lambda: None)
        _setup(electron_only=True)
        out = capsys.readouterr().out
        assert "Desktop App" in out

    def test_electron_only_returns_early(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup._setup_electron", lambda: None)
        with patch("kiro_crew.agent.install_agent") as mock_agent:
            _setup(electron_only=True)
        mock_agent.assert_not_called()


class TestSetupDesktopPrompt:
    def _run_setup(self, monkeypatch, tmp_path, *, electron_dir, answer="n"):
        import kiro_crew.cli_setup as cs

        prompts: list[str] = []
        installs: list[int] = []
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent", lambda clean=False: tmp_path / "agent.json"
        )
        monkeypatch.setattr("kiro_crew.agent.ensure_kirocrew_on_path", lambda *a, **k: None)
        monkeypatch.setattr("kiro_crew.mcp_cleanup.clean_stale_managed_mcp", lambda: [])
        for name in (
            "_ensure_prerequisites",
            "_setup_workspace_dir",
            "_setup_sandbox_consent",
            "_setup_timezone",
            "_maybe_setup_dashboard_url",
            "_maybe_setup_custom_domain",
            "_maybe_setup_cloud",
            "_ensure_default_agent_in_config",
            "_remove_retired_conductor_skill",
        ):
            monkeypatch.setattr(cs, name, lambda *a, **k: None)
        monkeypatch.setattr(cs.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(cs, "_find_electron_dir", lambda: electron_dir)
        monkeypatch.setattr(cs, "_setup_electron", lambda: installs.append(1))

        def _input(prompt=""):
            prompts.append(prompt)
            return answer

        monkeypatch.setattr("builtins.input", _input)
        cs._setup_impl()
        return prompts, installs

    def test_a_packaged_install_is_not_asked_to_install_the_desktop_app(
        self, monkeypatch, tmp_path, capsys
    ):
        prompts, installs = self._run_setup(monkeypatch, tmp_path, electron_dir=None)

        assert not [p for p in prompts if "desktop app" in p]
        assert installs == []
        out = capsys.readouterr().out
        assert "Desktop app sources not found" in out
        assert "make desktop" in out

    def test_a_source_checkout_is_still_asked_and_installs_on_yes(self, monkeypatch, tmp_path):
        prompts, installs = self._run_setup(
            monkeypatch, tmp_path, electron_dir=tmp_path, answer="y"
        )

        assert prompts == ["  Install Kiro Crew desktop app to ~/Applications? [Y/n]: "]
        assert installs == [1]


def _make_bundle(root, marker="new"):
    bundle = root / "KiroCrew.app"
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "MacOS" / bundle.stem).write_text("bin", encoding="utf-8")
    (contents / "Info.plist").write_text(marker, encoding="utf-8")
    module = (
        contents
        / "Resources"
        / "backend-dist"
        / "kirocrew-backend-arm64"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "kiro_crew"
        / "cli_setup.py"
    )
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    return bundle, module


def _run_from(monkeypatch, module):
    monkeypatch.setattr(cli_setup, "__file__", str(module))


class TestFindPrebuiltApp:
    def test_finds_the_bundle_the_module_runs_from(self, tmp_path, monkeypatch):
        bundle, module = _make_bundle(tmp_path / "payload")
        _run_from(monkeypatch, module)
        assert _find_prebuilt_app() == bundle.resolve()

    def test_ignores_an_incomplete_bundle(self, tmp_path, monkeypatch):
        bundle, module = _make_bundle(tmp_path / "payload")
        (bundle / "Contents" / "MacOS" / bundle.stem).unlink()
        _run_from(monkeypatch, module)
        assert _find_prebuilt_app() is None

    def test_source_checkout_has_no_bundle(self, tmp_path, monkeypatch):
        module = tmp_path / "repo" / "src" / "kiro_crew" / "cli_setup.py"
        module.parent.mkdir(parents=True)
        module.write_text("", encoding="utf-8")
        _run_from(monkeypatch, module)
        assert _find_prebuilt_app() is None


class TestSetupElectronPrebuilt:
    def _packaged(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: home)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: None)
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: None)
        runs = []
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: runs.append(a))
        bundle, module = _make_bundle(tmp_path / "payload")
        _run_from(monkeypatch, module)
        return home / "Applications", bundle, runs

    def test_installs_prebuilt_without_node_or_sources(self, tmp_path, capsys, monkeypatch):
        apps, bundle, runs = self._packaged(tmp_path, monkeypatch)
        _setup_electron()
        out = capsys.readouterr().out
        assert "installed to ~/Applications" in out
        assert "Node.js not found" not in out
        assert "sources not found" not in out
        assert runs == []
        installed = apps / "KiroCrew.app" / "Contents" / "Info.plist"
        assert installed.read_text(encoding="utf-8") == "new"
        stamp = apps / ".KiroCrew.app.source"
        assert stamp.read_text(encoding="utf-8").strip() == str(bundle.resolve())

    def test_replaces_a_stale_copy(self, tmp_path, capsys, monkeypatch):
        apps, _bundle, _runs = self._packaged(tmp_path, monkeypatch)
        _make_bundle(apps, marker="old")
        _setup_electron()
        installed = apps / "KiroCrew.app" / "Contents" / "Info.plist"
        assert installed.read_text(encoding="utf-8") == "new"
        assert sorted(p.name for p in apps.iterdir()) == [".KiroCrew.app.source", "KiroCrew.app"]

    def test_symlinked_install_is_left_alone(self, tmp_path, capsys, monkeypatch):
        apps, bundle, _runs = self._packaged(tmp_path, monkeypatch)
        apps.mkdir()
        (apps / "KiroCrew.app").symlink_to(bundle)
        _setup_electron()
        assert "already this bundle" in capsys.readouterr().out
        assert (apps / "KiroCrew.app").is_symlink()

    def test_failed_copy_keeps_the_previous_install(self, tmp_path, capsys, monkeypatch):
        apps, _bundle, _runs = self._packaged(tmp_path, monkeypatch)
        _make_bundle(apps, marker="old")

        def _fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("kiro_crew.cli_setup.shutil.copytree", _fail)
        _setup_electron()
        out = capsys.readouterr().out
        assert "Could not install KiroCrew.app: disk full" in out
        installed = apps / "KiroCrew.app" / "Contents" / "Info.plist"
        assert installed.read_text(encoding="utf-8") == "old"
        assert [p.name for p in apps.iterdir()] == ["KiroCrew.app"]


class TestDesktopAppStep:
    def _no_prompt(self, monkeypatch):
        def _unexpected(prompt=""):
            raise AssertionError("prompted")

        monkeypatch.setattr("builtins.input", _unexpected)

    def _home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: home)
        return home / "Applications"

    def test_no_prompt_after_installing_this_bundle(self, tmp_path, capsys, monkeypatch):
        self._home(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: None)
        _bundle, module = _make_bundle(tmp_path / "payload")
        _run_from(monkeypatch, module)
        _setup_electron()
        capsys.readouterr()
        self._no_prompt(monkeypatch)
        _setup_desktop_app_step()
        assert "already installed" in capsys.readouterr().out

    def test_no_prompt_when_running_from_an_applications_folder(
        self, tmp_path, capsys, monkeypatch
    ):
        self._home(tmp_path, monkeypatch)
        _bundle, module = _make_bundle(tmp_path / "Applications")
        _run_from(monkeypatch, module)
        self._no_prompt(monkeypatch)
        _setup_desktop_app_step()
        assert "already installed" in capsys.readouterr().out

    def test_prompts_when_the_copy_came_from_another_bundle(self, tmp_path, capsys, monkeypatch):
        apps = self._home(tmp_path, monkeypatch)
        _make_bundle(apps, marker="old")
        (apps / ".KiroCrew.app.source").write_text("/elsewhere/KiroCrew.app\n", encoding="utf-8")
        _bundle, module = _make_bundle(tmp_path / "payload")
        _run_from(monkeypatch, module)
        prompts = []
        monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "n")
        _setup_desktop_app_step()
        assert len(prompts) == 1
        assert "Skipped" in capsys.readouterr().out

    def test_source_checkout_still_prompts(self, tmp_path, capsys, monkeypatch):
        apps = self._home(tmp_path, monkeypatch)
        _make_bundle(apps, marker="old")
        module = tmp_path / "repo" / "src" / "kiro_crew" / "cli_setup.py"
        module.parent.mkdir(parents=True)
        module.write_text("", encoding="utf-8")
        _run_from(monkeypatch, module)
        prompts = []
        monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "n")
        _setup_desktop_app_step()
        assert len(prompts) == 1
