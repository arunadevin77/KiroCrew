"""``kirocrew cloud memory`` -- the engine that backs memory up to a sync target.

These tests never touch a real external helper, a real network target, or the
live crew home: ``file://`` goes to ``tmp_path`` and the helper subprocess is
faked. The one property that matters most is pinned directly: an upload is
redacted even when the local opt-out switch is OFF, because redaction is forced
on for this destination. The helper argv contract is pinned too: every call is
``kirocrew-sync-<scheme> <verb> <url> ...`` with the URL first and
``/``-separated ``<remote-rel>`` paths.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact
from kiro_crew.cloud import memory_sync

TOKEN = "8412345678:AAH9xSECRETtokenvalue_here12345"


def _real_db(path: Path, secret: str | None = None) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE semantic_memory (key, value_json)")
    conn.execute("INSERT INTO semantic_memory VALUES (?, ?)", ("note", secret or "nothing secret"))
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A snapshot bundle whose memory.db carries a credential."""
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir()
    (payload / "memory.db").write_bytes(_real_db(tmp_path / "src.db", f"token {TOKEN} here"))
    (payload / "MANIFEST.json").write_text(
        json.dumps({"version": 3, "components": {"memory": "unresolved"}}),
        encoding="utf-8",
    )
    out = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return out


@pytest.fixture
def file_target(tmp_path: Path) -> memory_sync.Target:
    """A built-in ``file://`` target rooted in a tmp directory."""
    root = tmp_path / "target"
    return memory_sync.parse_target(f"file://{root}")


# -- target parsing ------------------------------------------------------------


def test_parse_target_splits_scheme_and_path():
    t = memory_sync.parse_target("file:///home/me/backups")
    assert t.scheme == "file"
    assert t.path == "/home/me/backups"


def test_parse_target_lowercases_scheme_keeps_path():
    t = memory_sync.parse_target("Remote://docs/x")
    assert t.scheme == "remote"
    assert t.path == "docs/x"


@pytest.mark.parametrize("bad", ["no-scheme-here", "", "   "])
def test_parse_target_rejects_non_url(bad):
    with pytest.raises(memory_sync.TargetUnavailable):
        memory_sync.parse_target(bad)


@pytest.mark.parametrize("bad", ["1file", "bad scheme", "a/b", "x" * 40])
def test_parse_target_rejects_malformed_scheme(bad):
    with pytest.raises(memory_sync.TargetUnavailable):
        memory_sync.parse_target(f"{bad}://p")


def test_resolve_target_prefers_explicit_over_config(monkeypatch):
    class _Cfg:
        class cloud_sync:
            target = "file:///from/config"

    monkeypatch.setattr(memory_sync.KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
    assert memory_sync.resolve_target("file:///from/flag").path == "/from/flag"
    assert memory_sync.resolve_target("").path == "/from/config"


def test_resolve_target_refuses_when_unset(monkeypatch):
    class _Cfg:
        class cloud_sync:
            target = ""

    monkeypatch.setattr(memory_sync.KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
    with pytest.raises(memory_sync.TargetUnavailable):
        memory_sync.resolve_target("")


def test_rel_joins_with_forward_slash_only():
    assert memory_sync.rel("host", "restore", "x.tar") == "host/restore/x.tar"
    assert memory_sync.rel("host", "", "readable") == "host/readable"
    assert memory_sync.rel() == ""


def test_file_root_handles_canonical_file_url(monkeypatch):
    """A canonical ``file:///path`` URL resolves to a real local path on either OS.

    ``parse_target`` leaves ``target.path`` as the URL path (``/C:/dir`` on a
    Windows URL, ``/home/me`` on POSIX); ``_file_root`` must turn that into the
    OS-native filesystem path via ``url2pathname`` rather than ``Path`` of the raw
    URL path, which would mangle a drive letter on Windows.
    """
    import ntpath
    import posixpath

    # POSIX: url2pathname is identity on a rooted path.
    monkeypatch.setattr(memory_sync, "url2pathname", lambda p: posixpath.normpath(p))
    t = memory_sync.parse_target("file:///home/me/backups")
    assert str(memory_sync._file_root(t)) == "/home/me/backups"

    # Windows: /C:/dir -> C:\\dir. Emulate ntpath so the assertion holds on any host.
    monkeypatch.setattr(memory_sync, "url2pathname", lambda p: ntpath.normpath(p.lstrip("/")))
    t = memory_sync.parse_target("file:///C:/dir/backups")
    assert "C:" in str(memory_sync._file_root(t))


def test_file_dest_joins_remote_rel_with_native_separator(tmp_path):
    """A ``/``-separated remote-rel joins under the root as a native path."""
    t = memory_sync.parse_target(f"file://{tmp_path}")
    dest = memory_sync._file_dest(t, "box-1/restore/2026.tar")
    # The result is the root plus each segment, however the OS spells separators.
    assert dest == memory_sync._file_root(t) / "box-1" / "restore" / "2026.tar"
    # An empty remote-rel is the root itself.
    assert memory_sync._file_dest(t, "") == memory_sync._file_root(t)


# -- host label ----------------------------------------------------------------


def test_host_label_defaults_to_hostname(monkeypatch):
    monkeypatch.setattr(memory_sync, "_default_hostname", lambda: "box-1")
    assert memory_sync.host_label() == "box-1"


def test_host_label_explicit_wins(monkeypatch):
    monkeypatch.setattr(memory_sync, "_default_hostname", lambda: "box-1")
    assert memory_sync.host_label("my laptop") == "my laptop"


def test_host_label_strips_path_separators():
    # A slash would let the folder segment escape the host's own folder.
    assert "/" not in memory_sync.host_label("a/b\\c")
    assert memory_sync.host_label("///") == "unknown-host"


# -- forced-on redaction -------------------------------------------------------


def test_redaction_is_forced_even_when_opt_out_is_off(bundle: Path, tmp_path: Path, monkeypatch):
    """The property the design turns on: an upload is scrubbed regardless of the switch."""
    # The local opt-out switch is OFF -- an on-host backup would NOT redact.
    monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: False)
    work = tmp_path / "work"
    work.mkdir()

    redacted = memory_sync._redact_bundle(bundle, work)

    assert redacted.is_file()
    with tarfile.open(redacted) as tf:
        for m in tf.getmembers():
            if m.name.endswith("memory.db"):
                f = tf.extractfile(m)
                assert f is not None
                assert (
                    TOKEN.encode() not in f.read()
                ), "the credential survived into the upload copy"
                break
        else:
            raise AssertionError("memory.db missing from the redacted bundle")


def test_prepare_redacted_copy_force_true_vs_false(bundle: Path, tmp_path: Path, monkeypatch):
    """force=True redacts with the switch off; force=False (unset) returns None (no upload copy)."""
    monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: False)
    w1 = tmp_path / "w1"
    w1.mkdir()
    assert snap.prepare_redacted_copy(bundle, w1, ["memory"], force=True) is not None
    w2 = tmp_path / "w2"
    w2.mkdir()
    assert snap.prepare_redacted_copy(bundle, w2, ["memory"]) is None


def test_redact_bundle_fails_closed_on_unprovable_db(tmp_path: Path, monkeypatch):
    """If the DB cannot be proven clean, nothing is produced -- the push must upload nothing."""

    def boom(_stage):  # noqa: ANN001
        raise snapshot_redact.PayloadDatabaseUnprovable(
            ["memory.db"], details=["memory.db: contentless"]
        )

    monkeypatch.setattr(snapshot_redact, "redact_bundle_for_egress", boom)
    # A trivial bundle to extract.
    payload = tmp_path / "kirocrew-snapshot-x"
    payload.mkdir()
    (payload / "memory.db").write_bytes(_real_db(tmp_path / "s.db"))
    (payload / "MANIFEST.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "kirocrew-snapshot-x.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(memory_sync.MemorySyncFailed):
        memory_sync._redact_bundle(out, work)


# -- file:// transport ---------------------------------------------------------


def test_file_put_get_ls_rm_roundtrip(file_target: memory_sync.Target, tmp_path: Path):
    memory_sync._require_reachable(file_target)
    local = tmp_path / "a.txt"
    local.write_text("hello", encoding="utf-8")

    memory_sync._put(file_target, local, "host1/readable/lessons.md")
    assert memory_sync._ls(file_target, "") == ["host1"]
    assert memory_sync._ls(file_target, "host1/readable") == ["lessons.md"]

    dl = tmp_path / "dl.txt"
    memory_sync._get(file_target, "host1/readable/lessons.md", dl)
    assert dl.read_text(encoding="utf-8") == "hello"

    assert memory_sync._rm(file_target, "host1/readable/lessons.md") is True
    assert memory_sync._ls(file_target, "host1/readable") == []


def test_file_put_dir_uploads_each_file(file_target: memory_sync.Target, tmp_path: Path):
    memory_sync._require_reachable(file_target)
    src = tmp_path / "readable"
    src.mkdir()
    (src / "lessons.md").write_text("l", encoding="utf-8")
    (src / "facts.md").write_text("f", encoding="utf-8")

    memory_sync._put_dir(file_target, src, "host1/readable")
    assert sorted(memory_sync._ls(file_target, "host1/readable")) == ["facts.md", "lessons.md"]


# -- helper argv contract ------------------------------------------------------


class HelperSpy:
    """Records every helper argv and answers from an in-memory FS, no subprocess."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.files: dict[str, bytes] = {}
        self.reachable = True

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name.startswith("kirocrew-sync-") else None

    def run(self, cmd, *, capture_output, check, text=True, **_kw):  # noqa: ANN001
        args = list(cmd[1:])  # drop the helper binary path
        self.calls.append(args)
        rc, out, err = self._answer(args)

        class _P:
            returncode = rc
            stdout = out
            stderr = err

        return _P()

    def _answer(self, args: list[str]) -> tuple[int, str, str]:
        verb = args[0] if args else ""
        if verb == "check":
            return (0, "", "") if self.reachable else (1, "", "not reachable")
        if verb == "put":
            # put <url> <local> <remote-rel>
            local, remote = args[2], args[3]
            self.files[remote] = Path(local).read_bytes()
            return (0, "", "")
        if verb == "get":
            # get <url> <remote-rel> <local>
            remote, local = args[2], args[3]
            if remote not in self.files:
                return (1, "", f"{remote} missing")
            Path(local).write_bytes(self.files[remote])
            return (0, "", "")
        if verb == "ls":
            # ls <url> <remote-rel> -- one name per line
            remote = args[2]
            prefix = f"{remote}/" if remote else ""
            names = sorted(
                {
                    k[len(prefix) :].split("/", 1)[0]
                    for k in self.files
                    if k.startswith(prefix) and len(k) > len(prefix)
                }
            )
            return (0, "".join(f"{n}\n" for n in names), "")
        if verb == "rm":
            self.files.pop(args[2], None)
            return (0, "", "")
        return (0, "", "")


@pytest.fixture
def helper(monkeypatch) -> HelperSpy:
    spy = HelperSpy()
    monkeypatch.setattr(memory_sync.shutil, "which", spy.which)
    monkeypatch.setattr(memory_sync.subprocess, "run", spy.run)
    return spy


def test_helper_argv_url_first_and_slash_paths(helper: HelperSpy, tmp_path: Path):
    tgt = memory_sync.parse_target("remote://docs")
    memory_sync._require_reachable(tgt)
    local = tmp_path / "x.tar"
    local.write_text("z", encoding="utf-8")

    memory_sync._put(tgt, local, "host/restore/2026.tar")
    memory_sync._ls(tgt, "host/restore")
    dl = tmp_path / "dl.tar"
    memory_sync._get(tgt, "host/restore/2026.tar", dl)
    memory_sync._rm(tgt, "host/restore/2026.tar")

    verbs = [c[0] for c in helper.calls]
    assert verbs == ["check", "put", "ls", "get", "rm"]
    # The URL is always the first argument after the verb.
    for call in helper.calls:
        assert call[1] == "remote://docs"
    # put/get carry the remote-rel with forward slashes.
    put = next(c for c in helper.calls if c[0] == "put")
    assert put[3] == "host/restore/2026.tar"
    get = next(c for c in helper.calls if c[0] == "get")
    assert get[2] == "host/restore/2026.tar"


def test_helper_missing_refuses(monkeypatch):
    monkeypatch.setattr(memory_sync.shutil, "which", lambda _n: None)
    tgt = memory_sync.parse_target("remote://x")
    with pytest.raises(memory_sync.TargetUnavailable):
        memory_sync._require_reachable(tgt)


def test_require_reachable_refuses_on_check_failure(helper: HelperSpy):
    helper.reachable = False
    tgt = memory_sync.parse_target("remote://x")
    with pytest.raises(memory_sync.TargetUnavailable):
        memory_sync._require_reachable(tgt)


# -- push / list / restore over file:// ----------------------------------------


def test_push_refuses_when_target_unset(tmp_path, monkeypatch, capsys):
    class _Cfg:
        class cloud_sync:
            target = ""

    monkeypatch.setattr(memory_sync.KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
    assert memory_sync.push() == 1
    assert "target" in capsys.readouterr().out.lower()


def test_list_empty_when_target_absent(file_target, monkeypatch, capsys):
    monkeypatch.setattr(memory_sync, "resolve_target", lambda t="": file_target)
    assert memory_sync.list_backups() == 0
    assert "No backups" in capsys.readouterr().out


def test_list_shows_hosts_and_stamps(file_target, monkeypatch, capsys):
    monkeypatch.setattr(memory_sync, "resolve_target", lambda t="": file_target)
    memory_sync._require_reachable(file_target)
    root = Path(file_target.path)
    for host in ("box-1", "box-2"):
        (root / host / "restore").mkdir(parents=True)
    (root / "box-1" / "restore" / "2026-01-01T00-00-00Z.tar").write_text("a")
    (root / "box-1" / "restore" / "2026-01-02T00-00-00Z.tar").write_text("b")

    assert memory_sync.list_backups() == 0
    out = capsys.readouterr().out
    assert "box-1" in out and "box-2" in out
    assert "2026-01-02T00-00-00Z.tar" in out


# -- retention -----------------------------------------------------------------


def test_prune_keeps_last_seven(file_target):
    memory_sync._require_reachable(file_target)
    folder = Path(file_target.path) / "box-1" / "restore"
    folder.mkdir(parents=True)
    names = [f"2026-01-{d:02d}T00-00-00Z.tar" for d in range(1, 11)]  # 10 bundles
    for n in names:
        (folder / n).write_text("x")

    removed = memory_sync._prune_restore_folder(file_target, "box-1/restore")
    assert len(removed) == 3  # 10 - 7
    assert removed == names[:3]  # the three oldest


def test_confirm_uploaded_passes_when_bundle_is_listed(file_target):
    memory_sync._require_reachable(file_target)
    folder = Path(file_target.path) / "box-1" / "restore"
    folder.mkdir(parents=True)
    (folder / "2026-01-02T00-00-00Z.tar").write_text("x")
    # Does not raise when the exact name is present.
    memory_sync._confirm_uploaded(file_target, "box-1/restore", "2026-01-02T00-00-00Z.tar")


def test_confirm_uploaded_fails_when_bundle_absent(file_target):
    memory_sync._require_reachable(file_target)
    folder = Path(file_target.path) / "box-1" / "restore"
    folder.mkdir(parents=True)
    (folder / "something-else.tar").write_text("x")
    with pytest.raises(memory_sync.MemorySyncFailed):
        memory_sync._confirm_uploaded(file_target, "box-1/restore", "2026-01-02T00-00-00Z.tar")
    # Also fails when the folder is missing entirely.
    with pytest.raises(memory_sync.MemorySyncFailed):
        memory_sync._confirm_uploaded(file_target, "box-1/absent", "x.tar")


def test_build_bundle_fails_closed_on_incomplete_snapshot(tmp_path: Path, monkeypatch):
    """An incomplete (--strict) snapshot must abort before any upload or prune.

    `snapshot_main` returns EXIT_INCOMPLETE (3) under --strict when the bundle omits
    an entry it was asked to carry; `_build_memory_bundle` must turn that into a
    refusal so push never uploads an incomplete bundle and never prunes older
    complete ones.
    """
    passed = {}

    def fake_snapshot_main(argv):  # noqa: ANN001
        passed["argv"] = list(argv)
        return 3  # EXIT_INCOMPLETE

    monkeypatch.setattr(memory_sync.snapshot, "snapshot_main", fake_snapshot_main)
    with pytest.raises(memory_sync.MemorySyncFailed):
        memory_sync._build_memory_bundle(tmp_path / "w")
    assert "--strict" in passed["argv"], "the bundle must be built with --strict"


# -- restore -------------------------------------------------------------------


def test_restore_refuses_when_no_bundles(file_target, monkeypatch, capsys):
    monkeypatch.setattr(memory_sync, "resolve_target", lambda t="": file_target)
    memory_sync._require_reachable(file_target)
    (Path(file_target.path) / "box-1" / "restore").mkdir(parents=True)
    assert memory_sync.restore(host="box-1") == 1
    assert "No restore bundles" in capsys.readouterr().out


def test_restore_downloads_and_calls_snapshot_restore(file_target, monkeypatch):
    monkeypatch.setattr(memory_sync, "resolve_target", lambda t="": file_target)
    memory_sync._require_reachable(file_target)
    folder = Path(file_target.path) / "box-1" / "restore"
    folder.mkdir(parents=True)
    (folder / "2026-01-02T00-00-00Z.tar").write_bytes(b"fake-bundle")

    captured: dict[str, list[str]] = {}

    def fake_restore(argv):  # noqa: ANN001
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(memory_sync.snapshot, "restore_main", fake_restore)
    rc = memory_sync.restore(host="box-1", stamp="2026-01-02T00-00-00Z.tar", mode="merge")
    assert rc == 0
    assert "--components" in captured["argv"] and "memory" in captured["argv"]
    assert "--mode" in captured["argv"] and "merge" in captured["argv"]
