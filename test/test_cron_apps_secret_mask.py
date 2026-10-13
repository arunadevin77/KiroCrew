"""Cron children cannot read any app's ``.app_secret``.

``<config_dir>/apps/<app>/.app_secret`` is a bearer credential: whoever reads it can act
as that app against the Gateway. Both cron exec paths (``run_script_sandboxed``,
``run_command_sandboxed``) mask EVERY installed app's ``.app_secret`` leaf by absolute
path, the cron's own app included. The apps tree itself is NOT masked: an app's bundle
code and its ``data/`` stay readable and writable from a cron child exactly as on main --
only the credential leaves are hidden. Each masked secret is a REQUIRED mask, so a secret
moved aside before mask time refuses the spawn rather than being skipped.

PER-BUNDLE hard-link scan: the cron's OWN bundle (its script's bundle, or the job's
host-stamped ``created_by`` owner) is scanned for a hard link to any app's ``.app_secret``
inode. Such a link exposes the credential by a name the leaf mask does not cover, so the
owning cron is refused -- but one app's stray link never refuses another app's cron. The
scan FAILS CLOSED when the secret set cannot be fully built.

Two layers are pinned:

* ``cron_apps_mask`` -- which paths each cron asks the sandbox to hide, both spellings of
  a symlinked home, the per-bundle refusal, and the fail-closed cases. Pure planner
  tests; they run on every host.
* REAL cases a-f -- each actually launches a cron child under the real OS sandbox backend
  (Linux namespace / macOS Seatbelt, no mocks) and asserts the secret read is denied, the
  app code is readable, or the hard-link cron is refused. They SKIP cleanly, with the
  reason, where no backend is available (a nested sandbox, Windows, or a CI runner without
  unprivileged user namespaces); the macOS CI job runs them under real Seatbelt.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import cron_script
from kiro_crew.cron_script import (
    CronAppsMask,
    _path_within,
    _safe_cron_cwd,
    cron_apps_mask,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="no cron sandbox backend on Windows; the launcher and its paths are POSIX-only",
)

OWN = "own-app"
OTHER = "other-app"


def _make_app(apps: Path, name: str) -> Path:
    app = apps / name
    (app / "lib").mkdir(parents=True)
    (app / "data").mkdir()
    (app / ".app_secret").write_text(f"secret-of-{name}\n")
    (app / "job.py").write_text("def run(ctx):\n    pass\n")
    (app / "backend").mkdir()
    (app / "backend" / "sibling.mjs").write_text("export const two = 2;\n")
    # A small ESM module pair, left readable as ordinary bundle code a cron child may
    # import; the leaf mask hides only ``.app_secret``, not this.
    (app / "backend" / "server.mjs").write_text(
        "import { two } from './sibling.mjs';\n" "process.stdout.write('NODE_OK:' + two + '\\n');\n"
    )
    return app


@pytest.fixture()
def crew_home(tmp_path, monkeypatch) -> Path:
    """A crew home with two installed apps, reached through a plain (unlinked) $HOME."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


@pytest.fixture()
def linked_crew_home(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The same layout behind a symlinked $HOME (``/home -> /local/home``)."""
    real_home = tmp_path / "local" / "home"
    crew = real_home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    link_home = tmp_path / "home"
    link_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link_home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew.resolve()))
    return crew.resolve(), link_home / ".kiro" / "crew"


def _under(parent: str, child: str) -> bool:
    parent = os.path.normpath(parent)
    child = os.path.normpath(child)
    return child == parent or child.startswith(parent + os.sep)


def _hidden(path: str, mask: CronAppsMask) -> bool:
    """Whether *path* is hidden under the leaf-only *mask* (a hidden entry covers it)."""
    return any(_under(h, path) for h in mask.hidden)


class TestCronAppsMask:
    """Planner tests: every app's secret leaf is hidden; code and data/ are not."""

    def test_every_apps_secret_is_hidden_including_the_crons_own(self, crew_home):
        apps = crew_home / "apps"
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        assert not mask.refusal
        # Both apps' secrets are hidden -- the cron's own included.
        assert _hidden(str(apps / OWN / ".app_secret"), mask)
        assert _hidden(str(apps / OTHER / ".app_secret"), mask)

    def test_app_code_and_data_are_not_hidden(self, crew_home):
        apps = crew_home / "apps"
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        # The leaf mask hides only the secret leaves; every app's code and data/ stay
        # visible (readable and writable) exactly as on main.
        for name in (OWN, OTHER):
            assert not _hidden(str(apps / name / "job.py"), mask)
            assert not _hidden(str(apps / name / "lib"), mask)
            assert not _hidden(str(apps / name / "data"), mask)
            assert not _hidden(str(apps / name / "backend" / "server.mjs"), mask)
        # The apps root itself is not masked -- no whole-tree mask.
        assert not _hidden(str(apps), mask)

    def test_the_mask_opens_no_windows(self, crew_home):
        # Leaf-only: there is no bundle window and nothing is sealed read-only.
        mask = cron_apps_mask(script_file=str(crew_home / "apps" / OWN / "job.py"), owner_app=OWN)
        assert not hasattr(mask, "windows") or not getattr(mask, "windows", ())
        assert not hasattr(mask, "readonly") or not getattr(mask, "readonly", ())

    def test_every_hidden_secret_is_also_a_required_mask(self, crew_home):
        mask = cron_apps_mask(script_file=str(crew_home / "apps" / OWN / "job.py"), owner_app=OWN)
        # Required == hidden: a secret moved aside before mask time refuses the spawn
        # (fail-closed) rather than being skipped.
        assert set(mask.required) == set(mask.hidden)
        assert mask.required  # non-empty: there are secrets to protect

    def test_a_command_cron_with_no_owner_still_hides_every_secret(self, crew_home):
        apps = crew_home / "apps"
        mask = cron_apps_mask(script_file="", owner_app="")
        assert not mask.refusal
        assert _hidden(str(apps / OWN / ".app_secret"), mask)
        assert _hidden(str(apps / OTHER / ".app_secret"), mask)
        assert not _hidden(str(apps / OWN / "job.py"), mask)

    def test_host_stamped_owner_is_scanned_but_opens_no_window(self, crew_home):
        apps = crew_home / "apps"
        mask = cron_apps_mask(script_file="", owner_app=OWN)
        assert not mask.refusal
        assert _hidden(str(apps / OWN / ".app_secret"), mask)
        assert not _hidden(str(apps / OWN / "data"), mask)

    @pytest.mark.parametrize("owner", ["", ".", "..", "a/b", "a\\b"])
    def test_an_unsafe_owner_name_is_ignored(self, crew_home, owner):
        # A malformed owner name names no bundle; the mask still hides every secret.
        mask = cron_apps_mask(script_file="", owner_app=owner)
        assert not mask.refusal
        assert _hidden(str(crew_home / "apps" / OWN / ".app_secret"), mask)

    def test_a_secretless_app_contributes_no_path_and_does_not_refuse(self, crew_home):
        # An app that ships no secret (a builtin) is genuine absence, not a gap.
        (crew_home / "apps" / OTHER / ".app_secret").unlink()
        mask = cron_apps_mask(script_file=str(crew_home / "apps" / OWN / "job.py"), owner_app=OWN)
        assert not mask.refusal
        assert _hidden(str(crew_home / "apps" / OWN / ".app_secret"), mask)
        assert not _hidden(str(crew_home / "apps" / OTHER / ".app_secret"), mask)

    def test_both_spellings_of_a_symlinked_home_are_masked(self, linked_crew_home):
        real_crew, link_crew = linked_crew_home
        mask = cron_apps_mask(script_file=str(real_crew / "apps" / OWN / "job.py"), owner_app=OWN)
        assert not mask.refusal
        # The secret is hidden under BOTH the real and the symlinked spelling.
        assert _hidden(str(real_crew / "apps" / OWN / ".app_secret"), mask)
        assert _hidden(str(link_crew / "apps" / OWN / ".app_secret"), mask)


class TestPerBundleHardLinkScan:
    """Item 3: a secret hard-linked into the cron's own bundle refuses only that app."""

    def test_own_bundle_linking_another_secret_refuses(self, crew_home):
        apps = crew_home / "apps"
        link = apps / OWN / "lib" / "stolen"
        os.link(apps / OTHER / ".app_secret", link)
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        assert mask.refusal
        assert OWN in mask.refusal
        assert "stolen" in mask.refusal

    def test_a_stray_link_in_one_bundle_does_not_refuse_another_apps_cron(self, crew_home):
        apps = crew_home / "apps"
        # OTHER's bundle links OWN's secret, but we run OWN's cron -- unaffected.
        os.link(apps / OWN / ".app_secret", apps / OTHER / "lib" / "stolen")
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        assert not mask.refusal
        assert _hidden(str(apps / OWN / ".app_secret"), mask)

    def test_a_second_link_to_the_bundles_own_secret_also_refuses(self, crew_home):
        apps = crew_home / "apps"
        # A second hard link to OWN's own secret, at a NON-masked name inside OWN.
        os.link(apps / OWN / ".app_secret", apps / OWN / "lib" / "copy")
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        assert mask.refusal
        assert "copy" in mask.refusal

    def test_an_unreadable_app_dir_fails_the_scan_closed(self, crew_home):
        apps = crew_home / "apps"
        # OTHER's dir cannot be read to build the secret id/path set -> fail closed.
        bad = apps / OTHER
        os.chmod(bad, 0o000)
        try:
            mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        finally:
            os.chmod(bad, 0o755)
        assert mask.refusal
        assert "could not be read" in mask.refusal

    def test_a_symlinked_secrets_referent_is_still_scanned(self, crew_home, tmp_path):
        # OTHER's secret is a symlink to a real file; a hard link to the REFERENT inside
        # OWN's bundle must still be caught.
        apps = crew_home / "apps"
        referent = tmp_path / "real-other-secret"
        referent.write_text("real\n")
        (apps / OTHER / ".app_secret").unlink()
        (apps / OTHER / ".app_secret").symlink_to(referent)
        os.link(referent, apps / OWN / "lib" / "stolen")
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        assert mask.refusal
        assert "stolen" in mask.refusal

    def test_a_link_outside_the_apps_tree_is_the_known_residual(self, crew_home, tmp_path):
        apps = crew_home / "apps"
        outside = tmp_path / "outside-link"
        os.link(apps / OTHER / ".app_secret", outside)
        # A link OUTSIDE the apps tree is not scanned (the known #16789 residual): the
        # own-bundle scan passes and the cron runs.
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        assert not mask.refusal

    def test_a_secret_mid_update_to_staging_fails_the_scan_closed(self, crew_home):
        # The GPT :2369 interleaving: update_app moves a live .app_secret to a
        # .<name>-secret-tmp staging sibling. If the live leaf is gone but staging exists,
        # the secret is live under one name the snapshot cannot pin -- treating it as a
        # secretless app would leave it unmasked (fail-open). The scan must fail CLOSED.
        apps = crew_home / "apps"
        (apps / OTHER / ".app_secret").rename(apps / f".{OTHER}-secret-tmp")
        mask = cron_apps_mask(script_file=str(apps / OWN / "job.py"), owner_app=OWN)
        assert mask.refusal
        assert "could not be read" in mask.refusal


class TestSafeCronCwd:
    """A cron child must not run with a cwd inside the apps tree (relative-read bypass)."""

    def test_a_cwd_outside_the_apps_tree_is_pinned_as_is(self, crew_home, monkeypatch):
        outside = crew_home / "crons"
        monkeypatch.chdir(outside)
        got = _safe_cron_cwd(os.path.realpath(str(crew_home / "apps")))
        assert got is not None
        assert not _path_within(got, str(crew_home / "apps"))

    def test_a_cwd_inside_the_apps_tree_is_replaced_with_a_neutral_dir(
        self, crew_home, monkeypatch
    ):
        inside = crew_home / "apps" / OWN
        monkeypatch.chdir(inside)
        got = _safe_cron_cwd(os.path.realpath(str(crew_home / "apps")))
        assert got is not None
        assert not _path_within(got, str(crew_home / "apps"))

    def test_refuses_when_no_safe_dir_can_be_established(self, crew_home, monkeypatch):
        monkeypatch.chdir(crew_home / "apps" / OWN)

        def _boom(*a, **k):
            raise OSError("no neutral dir")

        monkeypatch.setattr(cron_script, "_path_within", lambda *a, **k: True)
        got = _safe_cron_cwd(os.path.realpath(str(crew_home / "apps")))
        assert got is None


class TestMaskHelperEdges:
    def test_a_script_outside_the_apps_tree_names_no_bundle_but_still_masks(self, crew_home):
        mask = cron_apps_mask(script_file=str(crew_home / "crons" / "x.py"), owner_app="")
        assert not mask.refusal
        assert _hidden(str(crew_home / "apps" / OWN / ".app_secret"), mask)

    def test_a_linked_bundle_is_not_scanned_as_own_but_secrets_still_masked(
        self, crew_home, tmp_path
    ):
        apps = crew_home / "apps"
        # Replace OWN with a symlink to a dir: not a plain bundle, so it is not the
        # scanned own bundle, but every real secret is still masked.
        real = tmp_path / "real-own"
        shutil.move(str(apps / OWN), str(real))
        (apps / OWN).symlink_to(real, target_is_directory=True)
        mask = cron_apps_mask(script_file="", owner_app=OWN)
        assert not mask.refusal
        assert _hidden(str(apps / OTHER / ".app_secret"), mask)

    def test_a_failed_mkdir_refuses_the_spawn(self, crew_home, monkeypatch):
        # Point the apps root at a path under a read-only parent, so cron_apps_mask's
        # own ``root.mkdir`` fails (config_dir() has already succeeded above).
        ro_parent = crew_home / "ro"
        ro_parent.mkdir()
        os.chmod(ro_parent, 0o500)
        target = ro_parent / "apps"

        monkeypatch.setattr(cron_script, "apps_dir", lambda: target, raising=False)
        import kiro_crew.apps.manager as _mgr

        monkeypatch.setattr(_mgr, "apps_dir", lambda: target)
        try:
            mask = cron_apps_mask(script_file="", owner_app="")
        finally:
            os.chmod(ro_parent, 0o700)
        assert mask.refusal
        assert "could not be created" in mask.refusal


# ── REAL sandbox cases a-f ──
#
# Each launches a cron child under the real OS backend. They skip cleanly with a reason
# where no backend is available.

_PRELUDE = textwrap.dedent("""
    import json, os, sys
    """)


def _skip_if_no_backend():
    from kiro_crew.sandbox import wrap_argv

    argv = ["/bin/true"]
    wrapped, cleanup = wrap_argv(list(argv), mode="cc")
    if cleanup:
        Path(cleanup).unlink(missing_ok=True)
    if wrapped == argv:
        pytest.skip("no OS sandbox backend available on this host (nested sandbox / no userns)")


class TestRealSandbox:
    """REAL cron children under the live backend. Skip cleanly where none is available."""

    def _run_script(self, crew: Path, body: str, owner_app: str = "") -> dict:
        _skip_if_no_backend()
        from kiro_crew.cron_script import run_script_sandboxed

        script = crew / "crons" / "probe.py"
        script.write_text(body)
        return run_script_sandboxed(
            str(script) + ":run",
            job_id="probe",
            job_message="",
            session_key="cron:probe",
            internal_secret="",
            owner_app=owner_app,
        )

    def test_a_app_cron_imports_a_sibling_in_its_own_bundle(self, crew_home):
        # The bundle's code is readable (no read-only seal), so a sibling import resolves.
        apps = crew_home / "apps"
        (apps / OWN / "sib.py").write_text("VALUE = 7\n")
        body = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(apps / OWN)!r})
            import sib
            def run(ctx):
                print('IMPORT_OK:' + str(sib.VALUE))
            """)
        res = self._run_script(crew_home, body, owner_app=OWN)
        assert res.get("status") == "ok", res

    def test_b_script_and_command_cron_cannot_read_another_apps_secret(self, crew_home):
        # The child RAISES if it reads any credential bytes, so status != "ok" on a leak.
        # A masked secret reads as empty (Linux empty-bind) or raises (no stand-in);
        # either way no bytes. status == "ok" therefore asserts the secret was NOT read.
        apps = crew_home / "apps"
        secret_bytes = (apps / OTHER / ".app_secret").read_bytes()
        body = textwrap.dedent(f"""
            def run(ctx):
                try:
                    data = open({str(apps / OTHER / '.app_secret')!r}, 'rb').read()
                except OSError:
                    return  # denied outright -> no bytes
                if data == {secret_bytes!r}:
                    raise AssertionError('LEAK: read another app secret bytes')
                assert data == b'', 'masked secret must read empty, got %r' % data
            """)
        res = self._run_script(crew_home, body, owner_app=OWN)
        assert res.get("status") == "ok", res

    def test_c_app_cron_cannot_read_its_own_secret(self, crew_home):
        # Same invariant for the cron's OWN secret: a leak raises -> status != "ok".
        apps = crew_home / "apps"
        secret_bytes = (apps / OWN / ".app_secret").read_bytes()
        body = textwrap.dedent(f"""
            def run(ctx):
                try:
                    data = open({str(apps / OWN / '.app_secret')!r}, 'rb').read()
                except OSError:
                    return
                if data == {secret_bytes!r}:
                    raise AssertionError('LEAK: read own app secret bytes')
                assert data == b'', 'masked own secret must read empty, got %r' % data
            """)
        res = self._run_script(crew_home, body, owner_app=OWN)
        assert res.get("status") == "ok", res

    def test_d_app_cron_can_read_another_apps_code_and_data(self, crew_home):
        # Item 2 removed: other apps' code and data/ are readable from a cron child.
        apps = crew_home / "apps"
        (apps / OTHER / "data" / "state.json").write_text("{}\n")
        body = textwrap.dedent(f"""
            def run(ctx):
                open({str(apps / OTHER / 'job.py')!r}).read()
                open({str(apps / OTHER / 'data' / 'state.json')!r}).read()
                print('READ_OTHER_CODE_AND_DATA_OK')
            """)
        res = self._run_script(crew_home, body, owner_app=OWN)
        assert res.get("status") == "ok", res

    def test_e_symlinked_home_covers_both_spellings(self, linked_crew_home):
        real_crew, link_crew = linked_crew_home
        secret_bytes = (real_crew / "apps" / OTHER / ".app_secret").read_bytes()
        body = textwrap.dedent(f"""
            def run(ctx):
                for p in ({str(real_crew / 'apps' / OTHER / '.app_secret')!r},
                          {str(link_crew / 'apps' / OTHER / '.app_secret')!r}):
                    try:
                        data = open(p, 'rb').read()
                    except OSError:
                        continue  # denied -> no bytes
                    if data == {secret_bytes!r}:
                        raise AssertionError('LEAK via ' + p)
                    assert data == b'', 'masked secret must read empty via %s, got %r' % (p, data)
            """)
        res = self._run_script(real_crew, body, owner_app=OWN)
        assert res.get("status") == "ok", res

    def test_f_hardlink_in_own_bundle_refuses_only_that_app(self, crew_home):
        apps = crew_home / "apps"
        os.link(apps / OTHER / ".app_secret", apps / OWN / "lib" / "stolen")
        body = "def run(ctx):\n    print('RAN')\n"
        res = self._run_script(crew_home, body, owner_app=OWN)
        # The own-bundle link refuses the spawn before it runs.
        assert res.get("status") == "error", res
        assert "stolen" in (res.get("error") or "")
