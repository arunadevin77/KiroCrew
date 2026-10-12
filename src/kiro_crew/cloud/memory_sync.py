"""``kirocrew cloud memory push | restore | list`` -- back up memory and lessons.

This is the testable engine behind the three CLI verbs; :mod:`kiro_crew.cli_cloud`
holds only the thin dispatchers. Kiro Crew is a public repo and names no storage
service: the destination is a URL chosen by the operator, resolved by
:func:`parse_target` into a scheme and a scheme-specific path. ``file://`` is
built in (a plain directory on this machine or a mount). Any other scheme is
delegated to an external helper, ``kirocrew-sync-<scheme>``, found on ``PATH`` --
the same pattern as git's ``git-remote-<transport>`` helpers -- so a destination
such as a private document space never needs Kiro Crew to carry its SDK or its
credential. Kiro Crew stores no credential for any target; a helper that needs one
reads the user's own session for that service.

A helper is invoked as ``kirocrew-sync-<scheme> <verb> <url> ...``, the full
target URL always the first argument after the verb, no shell involved:

* ``check <url>``
* ``put <url> <local-file> <remote-rel>``
* ``get <url> <remote-rel> <local-file>``
* ``ls <url> <remote-rel>`` -- one entry name per stdout line
* ``rm <url> <remote-rel>``

``<remote-rel>`` always uses ``/`` (e.g. ``restore/<UTC>.tar``,
``readable/lessons.md``), never the host OS's separator. Exit 0 is success; a
non-zero exit is a refusal and its stderr is shown to the operator.

What leaves the machine is always redacted. The snapshot bundle is run through
Kiro Crew's existing egress redactor by calling
:func:`snapshot.prepare_redacted_copy` with ``force=True`` -- redaction is forced
on, so the local opt-out switch used by on-host backups does not apply. The
readable markdown mirror is run through the same text scrubber
(:func:`snapshot_redact._scrub`) value by value. If the memory database cannot be
proven clean, the push fails and uploads nothing: a bundle reported as uploaded is
one the user will rely on.

Layout under the target, one subtree per host label::

    <target>/<host label>/
        readable/   lessons.md preferences.md projects.md facts.md   (one-way mirror)
        restore/<UTC>.tar                                            (exact restore, last 7 kept)

Visibility and sharing are the target's own business, not Kiro Crew's: a ``file://``
directory inherits filesystem permissions, and a helper-backed target inherits
whatever that service's own default is. Nothing here changes either automatically.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 -- a fixed external helper found via which(), no shell
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.request import url2pathname

from kiro_crew import snapshot, snapshot_redact
from kiro_crew.cloud import ui
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: How many restore bundles to keep per host label.
KEEP = 7
#: The four readable documents rendered from the stores.
READABLE_DOCS = ("lessons.md", "preferences.md", "projects.md", "facts.md")

# A host label becomes one path segment under the target, so it must not carry a
# slash or the path separators a target would read as nesting. Spaces are fine,
# control characters and slashes are not.
_LABEL_STRIP = re.compile(r"[\x00-\x1f/\\]+")

# A URL scheme, bounded and ASCII-only so it can never be read as a shell
# metacharacter or a path segment of its own. Mirrors RFC 3986's scheme
# production (letter, then letters/digits/+/-/.) with a length cap: long enough
# for any real transport name, short enough that a scheme cannot smuggle a
# second argument's worth of text into the helper binary name it becomes.
_SCHEME_RE = re.compile(r"\A[a-zA-Z][a-zA-Z0-9+.-]{0,30}\Z")


class TargetUnavailable(RuntimeError):
    """The configured target is missing, malformed, or its helper cannot run."""


class MemorySyncFailed(RuntimeError):
    """The push/restore could not complete and left nothing half-done off-host."""


def host_label(explicit: str = "") -> str:
    """The folder segment for this machine under the target root.

    An explicit ``--host`` wins; otherwise the machine's own hostname, which is
    what makes a second machine's restore list say which copy came from where.
    Sanitised to one path segment so a hostname carrying a slash cannot write
    outside the host's own folder.
    """
    raw = explicit.strip() or _default_hostname()
    cleaned = _LABEL_STRIP.sub("-", raw).strip(" .-")
    return cleaned or "unknown-host"


def _default_hostname() -> str:
    import socket

    try:
        return socket.gethostname() or "unknown-host"
    except OSError:
        return "unknown-host"


# -- target resolution --------------------------------------------------------


class Target:
    """A sync destination: a URL scheme plus the scheme-specific remainder.

    ``raw`` is the exact string the operator gave (``--target`` or the
    ``memory_sync.target`` config key), passed whole as the first argument to
    every helper call. ``path`` is everything after ``scheme://``, used only by
    the built-in ``file://`` handling as a filesystem root -- a helper-backed
    target never sees it; the helper resolves a ``<remote-rel>`` against its own
    idea of ``raw``.
    """

    __slots__ = ("scheme", "path", "raw")

    def __init__(self, scheme: str, path: str, raw: str) -> None:
        self.scheme = scheme
        self.path = path
        self.raw = raw


def rel(*parts: str) -> str:
    """Join *parts* into one ``<remote-rel>``, always with ``/``, never OS-native.

    Every path handed to a helper's ``put``/``get``/``ls``/``rm`` is relative to
    the target root and uses ``/`` regardless of host OS, so the same string
    means the same thing whether the target is reached from Linux or Windows.
    """
    return "/".join(p.strip("/") for p in parts if p)


def parse_target(raw: str) -> Target:
    """Parse ``scheme://path`` into a :class:`Target`, or raise :class:`TargetUnavailable`.

    The scheme is validated against :data:`_SCHEME_RE` before it ever names a
    helper binary (``kirocrew-sync-<scheme>``) or enters a
    subprocess argv, so a malformed or hostile value cannot become part of a
    command line or a surprising executable name.
    """
    raw = (raw or "").strip()
    if "://" not in raw:
        raise TargetUnavailable(
            f"{raw!r} is not a target URL (expected scheme://path, e.g. file:///home/me/backups)."
        )
    scheme, _, path = raw.partition("://")
    if not _SCHEME_RE.match(scheme):
        raise TargetUnavailable(
            f"{scheme!r} is not a valid target scheme (letters, digits, +, -, . only, "
            "starting with a letter)."
        )
    return Target(scheme.lower(), path, raw)


def resolve_target(explicit: str = "") -> Target:
    """The configured target: ``--target`` wins, else the ``memory_sync.target`` config key.

    Raises :class:`TargetUnavailable` with how to fix it when neither is set, so a
    first run tells the operator what to type rather than defaulting silently to
    somewhere they did not choose.
    """
    raw = explicit.strip()
    if not raw:
        raw = (KiroCrewConfig.load().cloud_sync.target or "").strip()
    if not raw:
        raise TargetUnavailable(
            "No sync target configured. Pass --target scheme://path (e.g. "
            "--target file:///home/me/kirocrew-backups), or set it once with "
            "`kirocrew config set memory_sync.target <url>`."
        )
    return parse_target(raw)


# -- generic put/get/ls/rm over a target --------------------------------------


def _file_root(target: "Target") -> Path:
    """The local directory a ``file://`` target names, cross-platform.

    ``target.path`` is the URL path after ``file://`` -- for a canonical
    ``file:///C:/dir`` that is ``/C:/dir``, which is not a usable Windows path.
    :func:`urllib.request.url2pathname` turns it into the OS-native form
    (a drive path on Windows, ``/home/me`` on POSIX) and percent-decodes it, so
    a target typed in the usual file-URL form resolves to the right place on
    either OS.
    """
    return Path(url2pathname(target.path))


def _file_dest(target: "Target", remote_rel: str) -> Path:
    """A ``<remote_rel>`` resolved under a ``file://`` root as an OS-native path.

    ``<remote_rel>`` is always ``/``-separated by contract; splitting on ``/``
    and joining through :class:`~pathlib.Path` makes the separator native, so the
    same relative path lands correctly whether the root is a POSIX or a Windows
    path.
    """
    root = _file_root(target)
    # remote_rel is a POSIX-style ("/"-separated) relative path by contract;
    # PurePosixPath.parts turns it into segments the OS-native root joins cleanly.
    parts = PurePosixPath(remote_rel).parts
    return root.joinpath(*parts) if parts else root


def _helper_bin(scheme: str) -> str:
    """Locate ``kirocrew-sync-<scheme>`` on PATH, or refuse with how to install it."""
    name = f"kirocrew-sync-{scheme}"
    found = shutil.which(name)
    if not found:
        raise TargetUnavailable(
            f"`{name}` is not on your PATH. Install the helper for the {scheme}:// "
            "target, then re-run."
        )
    return found


def _run_helper(
    target: Target, verb: str, *rest: str, capture: bool = True, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run ``kirocrew-sync-<scheme> <verb> <target.raw> <rest...>``, no shell.

    The argv contract every helper implements, the URL always the first argument
    after the verb so a helper never has to split it out of a later path:

    * ``check <url>``
    * ``put <url> <local-file> <remote-rel>``
    * ``get <url> <remote-rel> <local-file>``
    * ``ls <url> <remote-rel>`` -- one entry name per stdout line
    * ``rm <url> <remote-rel>``

    ``<remote-rel>`` always uses ``/`` regardless of host OS (e.g.
    ``restore/<UTC>.tar``, ``readable/lessons.md``). Exit 0 is success; a non-zero
    exit is a refusal, and its stderr (falling back to stdout) is shown.
    """
    cmd = [_helper_bin(target.scheme), verb, target.raw, *rest]
    proc = subprocess.run(  # nosec B603 -- helper path from which(), args are our own literals
        cmd,
        capture_output=capture,
        **UTF8_TEXT,
        check=False,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise MemorySyncFailed(
            f"`kirocrew-sync-{target.scheme} {verb}` failed (exit {proc.returncode}): {err}"
        )
    return proc


def _require_reachable(target: Target) -> None:
    """Refuse before doing any work if the target cannot prove it is usable.

    For ``file://`` this means the directory exists or can be created. For a
    helper-backed target this runs ``check <url>``, which is free to mean "signed
    in" for a target backed by its own login session, or anything else the helper
    needs to confirm before ``put``/``get`` would otherwise fail opaquely mid-run.
    """
    if target.scheme == "file":
        root = _file_root(target)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TargetUnavailable(f"could not create {root}: {exc}") from exc
        return
    proc = _run_helper(target, "check", check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise TargetUnavailable(
            f"kirocrew-sync-{target.scheme} check failed: {err or 'not reachable'}"
        )


def _put(target: Target, local_file: Path, remote_rel: str) -> None:
    """Upload *local_file* to ``<target>/<remote_rel>``, replacing it if present."""
    if target.scheme == "file":
        dest = _file_dest(target, remote_rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_file, dest)
        return
    _run_helper(target, "put", str(local_file), remote_rel)


def _put_dir(target: Target, local_dir: Path, remote_rel: str) -> None:
    """Upload every file directly under *local_dir*, one ``put`` call per file.

    The argv contract moves one file at a time, so a "directory" upload is this
    module calling :func:`_put` in a loop rather than a verb a helper implements.
    """
    for item in sorted(local_dir.iterdir()):
        if item.is_file():
            _put(target, item, f"{remote_rel}/{item.name}")


def _get(target: Target, remote_rel: str, local_file: Path) -> None:
    """Download ``<target>/<remote_rel>`` to *local_file*."""
    if target.scheme == "file":
        src = _file_dest(target, remote_rel)
        if not src.is_file():
            raise MemorySyncFailed(f"{src} does not exist")
        local_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local_file)
        return
    local_file.parent.mkdir(parents=True, exist_ok=True)
    _run_helper(target, "get", remote_rel, str(local_file))


def _ls(target: Target, remote_rel: str) -> list[str]:
    """Entry names directly under ``<target>/<remote_rel>``, or ``[]`` when absent."""
    if target.scheme == "file":
        d = _file_dest(target, remote_rel)
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir())
    proc = _run_helper(target, "ls", remote_rel, check=False)
    if proc.returncode != 0:
        return []
    return [n.strip() for n in (proc.stdout or "").splitlines() if n.strip()]


def _rm(target: Target, remote_rel: str) -> bool:
    """Delete the single file at ``<target>/<remote_rel>``. Returns whether it succeeded."""
    if target.scheme == "file":
        p = _file_dest(target, remote_rel)
        try:
            p.unlink()
            return True
        except OSError:
            return False
    proc = _run_helper(target, "rm", remote_rel, check=False)
    return proc.returncode == 0


# -- readable markdown mirror --------------------------------------------------


def _scrub_text(text: str) -> str:
    """Run the readable text through the same egress scrubber the bundle uses."""
    return snapshot_redact._scrub(text or "")[0]


def render_readable() -> dict[str, str]:
    """Render the readable one-way mirror, each document already scrubbed.

    Returns a mapping of file name to scrubbed markdown for the four documents in
    :data:`READABLE_DOCS`. A store that cannot be read contributes a short note
    rather than failing the whole push: the restore bundle, not these documents,
    is the thing a restore depends on.
    """
    pref, proj, facts = _memory_text()
    return {
        "lessons.md": _scrub_text(_render_lessons()),
        "preferences.md": _scrub_text(_wrap("# Preferences", pref)),
        "projects.md": _scrub_text(_wrap("# Projects", proj)),
        "facts.md": _scrub_text(_wrap("# Semantic facts", facts)),
    }


def _wrap(heading: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return f"{heading}\n\n_(none recorded)_\n"
    if body.lstrip().startswith("#"):
        return body + "\n"
    return f"{heading}\n\n{body}\n"


def _render_lessons() -> str:
    try:
        from kiro_crew.learn import LessonStore

        lessons = LessonStore().load_all()
    except Exception as exc:  # noqa: BLE001 -- a readable mirror never fails the push
        return f"# Lessons\n\n_(could not be read: {exc})_\n"
    if not lessons:
        return "# Lessons\n\n_(none recorded)_\n"
    lines = ["# Lessons", ""]
    for le in lessons:
        tier = le.applies or "unclassified"
        scope = le.repo_scope or "everywhere"
        lines.append(f"- **[{le.category}]** ({tier}, {scope}) {le.rule}")
        if le.negative:
            lines.append(f"  - NOT: {le.negative}")
    lines.append("")
    return "\n".join(lines)


def _memory_text() -> tuple[str, str, str]:
    """Read preferences, projects and semantic facts; a failure yields an empty string.

    Reads through the same :class:`~kiro_crew.memory.MemoryStore` API the rest of
    the app uses, so the readable mirror holds exactly what the agent sees.
    """
    pref = proj = facts = ""
    try:
        from kiro_crew.memory import MemoryStore

        store = MemoryStore()
        try:
            pref = store.read_preferences()
        except Exception:  # noqa: BLE001
            pref = ""
        try:
            proj = store.read_projects()
        except Exception:  # noqa: BLE001
            proj = ""
        facts = _facts_from(store)
    except Exception:  # noqa: BLE001 -- a readable mirror never fails the push
        pass
    return pref, proj, facts


def _facts_from(store: object) -> str:
    """Best-effort semantic-fact context; empty when the vector store is unavailable."""
    try:
        vs = getattr(store, "vector_store", None)
        if vs is None:
            return ""
        return vs.get_semantic_context(query_text="", cap=200_000, facts_only=True) or ""
    except Exception:  # noqa: BLE001
        return ""


# -- push ----------------------------------------------------------------------


def _build_memory_bundle(workdir: Path) -> Path:
    """Build a ``--components memory`` snapshot into *workdir*, return its path.

    Passes ``--strict`` so a bundle that OMITS any memory entry it was asked to carry
    (a persistently unreadable store, a vanished file) exits non-zero instead of
    returning 0 with a quietly-incomplete archive. Without it, an incomplete bundle
    would upload and then the remote prune would delete older COMPLETE bundles past
    the keep limit -- so a run of incomplete pushes could destroy the last good
    off-host copy, the exact loss `snapshot_main`'s own local no-prune guard exists
    to prevent. Failing here means nothing is uploaded and nothing is pruned.
    """
    out = workdir / "snapshot"
    out.mkdir(parents=True, exist_ok=True)
    rc = snapshot.snapshot_main(
        [str(out), "--components", "memory", "--keep", str(KEEP), "--strict"]
    )
    if rc != 0:
        raise MemorySyncFailed(
            "the memory snapshot was incomplete or could not be created, so nothing was "
            "uploaded and no old backup was pruned. Fix the unreadable memory state and "
            "re-run (see the snapshot output above for which entries were omitted)."
        )
    bundles = sorted(out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    if not bundles:
        raise MemorySyncFailed("the snapshot step reported success but wrote no bundle.")
    return bundles[-1]


def _redact_bundle(bundle: Path, workdir: Path) -> Path:
    """Produce the redacted, upload-safe bundle, redaction FORCED ON.

    Delegates to :func:`snapshot.prepare_redacted_copy` with ``force=True`` so the
    whole redaction pass -- the egress scrubber, the fail-closed refusal of a
    database or file that cannot be proven clean, and the "upload nothing on
    failure" rule -- is the shared, already-tested code path, not a copy of it. The
    only cloud-sync-specific choice is ``force``: a push here is always scrubbed,
    so the local opt-out switch is not consulted. ``prepare_redacted_copy`` prints
    the scrub report itself. With ``force=True`` it never returns ``None``.
    """
    try:
        payload = snapshot.prepare_redacted_copy(bundle, workdir, ["memory"], force=True)
    except snapshot.RedactionFailed as exc:
        raise MemorySyncFailed(
            f"the bundle could not be proven free of credentials, so nothing was "
            f"uploaded ({exc}). Your local state is untouched."
        ) from exc
    if payload is None:
        # Defensive: force=True must always redact. If a future change made it
        # return None, uploading the raw bundle would be a credential leak, so refuse.
        raise MemorySyncFailed(
            "the redaction step did not produce a scrubbed copy; nothing was uploaded."
        )
    return payload


def push(host: str = "", target: str = "") -> int:
    """``kirocrew cloud memory push`` -- redact, render, and upload to the sync target."""
    try:
        tgt = resolve_target(target)
        _require_reachable(tgt)
    except TargetUnavailable as exc:
        ui.fail(str(exc))
        return 1

    label = host_label(host)
    remote_root = rel(label)
    remote_readable = rel(label, "readable")
    remote_restore = rel(label, "restore")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    tar_name = f"{stamp}.tar"

    ui.note(f"Backing up memory and lessons to {tgt.raw} ({remote_root})")

    with tempfile.TemporaryDirectory(prefix="kc-cloud-mem-", dir=_scratch_root()) as tmp:
        work = Path(tmp)
        try:
            bundle = _build_memory_bundle(work)
            redacted = _redact_bundle(bundle, work)

            readable = work / "readable"
            readable.mkdir(parents=True, exist_ok=True)
            for name, text in render_readable().items():
                (readable / name).write_text(text, encoding="utf-8")

            restage = work / "restore"
            restage.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(redacted, restage / tar_name)

            ui.info("Uploading readable documents...")
            _put_dir(tgt, readable, remote_readable)
            ui.info("Uploading restore bundle...")
            _put_dir(tgt, restage, remote_restore)
            # Read the bundle back before claiming success. A successful upload call
            # is not proof the file landed where restore looks for it: if the target's
            # layout is not what is assumed here, the upload would report success
            # while `restore` later finds nothing -- discovered only after the machine
            # is gone, which is the exact case this feature exists for. So confirm
            # `tar_name` is now listed.
            _confirm_uploaded(tgt, remote_restore, tar_name)
        except (TargetUnavailable, MemorySyncFailed) as exc:
            ui.fail(str(exc))
            return 1

    removed = _prune_restore_folder(tgt, remote_restore)
    if removed:
        ui.detail(f"Pruned {len(removed)} old restore bundle(s), keeping the last {KEEP}.")

    ui.ok(f"Backed up to {remote_root}")
    ui.detail(
        f"readable/ -- {', '.join(READABLE_DOCS)} "
        "(one-way mirror; editing at the target does not write back)"
    )
    ui.detail(f"restore/{tar_name} -- the redacted bundle for `kirocrew cloud memory restore`")
    ui.detail("Visibility and sharing of the target are its own; nothing here changes them.")
    return 0


def _prune_restore_folder(target: Target, remote_restore: str, keep: int = KEEP) -> list[str]:
    """Delete all but the newest *keep* ``<UTC>.tar`` bundles. Returns the removed names."""
    names = [n for n in _ls(target, remote_restore) if n.endswith(".tar")]
    names.sort()  # <UTC>.tar sorts chronologically
    stale = names[:-keep] if len(names) > keep else []
    removed: list[str] = []
    for name in stale:
        if _rm(target, f"{remote_restore}/{name}"):
            removed.append(name)
    return removed


def _confirm_uploaded(target: Target, remote_restore: str, tar_name: str) -> None:
    """Fail unless *tar_name* is actually listed under *remote_restore* after the push.

    A successful upload call is not proof the bundle landed where restore looks for
    it: if the remote layout differs from what this module assumes, the upload
    could report success while the file is absent or nested elsewhere, and the user
    would only discover the empty backup at restore time -- after the machine is
    gone. Reading the folder back and requiring the exact name closes that gap, so
    a push that reports success is a push `restore` can find.
    """
    names = set(_ls(target, remote_restore))
    if tar_name not in names:
        raise MemorySyncFailed(
            f"the restore bundle was uploaded but {tar_name} is not listed under "
            f"{remote_restore} afterwards, so the backup cannot be trusted. Nothing was "
            "pruned. Check the target and its quota, then re-run."
        )


def _scratch_root() -> str | None:
    """Where temp workdirs go. Honours KIROCREW_SCRATCH when set, else the default."""
    scratch = os.environ.get("KIROCREW_SCRATCH")
    if scratch and Path(scratch).is_dir():
        return scratch
    return None


# -- list -----------------------------------------------------------------------


def _host_labels(target: Target) -> list[str]:
    return sorted(n.rstrip("/") for n in _ls(target, ""))


def list_backups(target: str = "") -> int:
    """``kirocrew cloud memory list`` -- show host labels and bundle timestamps."""
    try:
        tgt = resolve_target(target)
        _require_reachable(tgt)
    except TargetUnavailable as exc:
        ui.fail(str(exc))
        return 1

    labels = _host_labels(tgt)
    if not labels:
        ui.note(
            f"No backups yet at {tgt.raw}. Run `kirocrew cloud memory push` on a machine first."
        )
        return 0

    ui.note(f"{ui.BOLD}Memory backups{ui.RESET}")
    for label in labels:
        print()
        ui.info(label)
        stamps = _restore_stamps(tgt, rel(label, "restore"))
        if not stamps:
            ui.detail("(no restore bundles)")
        for s in stamps:
            ui.detail(s)
    return 0


def _restore_stamps(target: Target, remote_restore: str) -> list[str]:
    names = [n for n in _ls(target, remote_restore) if n.endswith(".tar")]
    return sorted(names, reverse=True)


# -- restore ----------------------------------------------------------------------


def restore(
    host: str = "", stamp: str = "", mode: str = "merge", dry_run: bool = False, target: str = ""
) -> int:
    """``kirocrew cloud memory restore`` -- download a chosen bundle and restore it."""
    try:
        tgt = resolve_target(target)
        _require_reachable(tgt)
    except TargetUnavailable as exc:
        ui.fail(str(exc))
        return 1

    label = host_label(host) if host else _pick_host(tgt)
    if not label:
        return 1
    remote_restore = rel(label, "restore")
    stamps = _restore_stamps(tgt, remote_restore)
    if not stamps:
        ui.fail(f"No restore bundles for host {label}. Run `kirocrew cloud memory list`.")
        return 1

    chosen = _pick_stamp(stamps, stamp)
    if not chosen:
        return 1

    with tempfile.TemporaryDirectory(prefix="kc-cloud-mem-", dir=_scratch_root()) as tmp:
        local = Path(tmp) / chosen
        try:
            _get(tgt, f"{remote_restore}/{chosen}", local)
        except (TargetUnavailable, MemorySyncFailed) as exc:
            ui.fail(str(exc))
            return 1
        if not local.is_file():
            ui.fail("The download reported success but wrote no file.")
            return 1

        argv = [str(local), "--components", "memory", "--mode", mode]
        if dry_run:
            argv.append("--dry-run")
        ui.note(f"Restoring memory from {label}/{chosen} (mode={mode})...")
        return snapshot.restore_main(argv)


def _pick_host(target: Target) -> str:
    labels = _host_labels(target)
    if not labels:
        ui.fail(f"No backups at {target.raw}. Run `kirocrew cloud memory push` on a machine first.")
        return ""
    if len(labels) == 1:
        return labels[0]
    idx = ui.choose("Restore memory from which machine?", [(x, "") for x in labels])
    return labels[idx]


def _pick_stamp(stamps: list[str], explicit: str) -> str:
    if explicit:
        if explicit in stamps:
            return explicit
        # Allow a bare timestamp without the .tar suffix.
        candidate = explicit if explicit.endswith(".tar") else f"{explicit}.tar"
        if candidate in stamps:
            return candidate
        ui.fail(f"No bundle {explicit!r}. Available: {', '.join(stamps)}")
        return ""
    # Newest first; default to the newest.
    idx = ui.choose("Restore which bundle?", [(x, "") for x in stamps])
    return stamps[idx]
