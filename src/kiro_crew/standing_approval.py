"""The operator's STANDING declaration that every tool approval is skipped.

``SafetyOverride`` has two kinds of grant. An **ad-hoc** one is toggled mid-session
and expires on ``agent.yolo_duration``. A **declared** one is the operator's standing
instruction: it never expires, and it is re-established on every startup. This module
owns where that declaration is read from, and nothing else -- establishing the grant
stays with ``safety_override``.

Where the declaration lives, and why not ``config.json``
-------------------------------------------------------
``standing-approval/grant.json`` sits on the KEYSTONE floor
(``security._CREW_SECRET_LEAVES``) and is bind-masked out of every agent sandbox
(``sandbox._CREW_HIDDEN_LEAVES``). That is the same placement as
``computer_use.json``, ``aws_service_consent.json``, ``oauth_endpoints.json``,
``file_delivery_consent.json`` and ``ssh_auth_sock_consent.json``, and for the same
reason: this is an authorization, not a preference. It is the widest one the product
has -- every tool call in every future session, with no prompt and no expiry.

The declaration is deliberately NOT ``agent.dangerously_skip_permissions`` in
``config.json``. A read-only seal on that document closes a write to the sealed NAME,
and it is kept as defence in depth, but a seal cannot reach the inode behind the name:
the crew data-home root is writable in every sandbox, ``link(2)`` needs no write
permission on the file it copies a name for, and a bind mount seals a MOUNT rather
than an inode. So an in-sandbox process that owns that document can add a second name
for it in the writable root, write the standing value through that name, and unlink
it -- and the next startup reads that changed document under a single link.

Two properties of this keystone answer that, and only the pair does:

* **Masked, not sealed.** A masked leaf cannot be opened in-sandbox at all, so it is
  not a ``link(2)`` source. A sealed-but-readable one still is.
* **A directory, not a file.** Linux refuses ``link(2)`` on a directory outright, so
  the alias shape has no source here even in principle, and a directory bind covers
  every child name rather than one pinned inode.

How the operator grants it
--------------------------
Like ``oauth_endpoints.json``, the operator writes the leaf out-of-band, from outside
the agent sandbox::

    mkdir -p "$KIROCREW_HOME/standing-approval"
    printf '{"dangerously_skip_permissions": true}\\n' \\
        > "$KIROCREW_HOME/standing-approval/grant.json"

There is deliberately no dashboard toggle (there never was one for this switch) and
no CLI verb: a surface that records this grant on request is a grant an automated
caller can take. This module is READ-ONLY on purpose.

Migrating an existing declaration
---------------------------------
An operator who set ``agent.dangerously_skip_permissions: true`` in ``config.json``
keeps the standing grant only where no Crew sandbox CAN mask the keystone leaf on this
host (native Windows, or a host with no sandbox backend): the key is a DEPRECATED ALIAS
that still grants there. Wherever a Crew-masked spawn is reachable the alias is refused
and the operator moves the declaration to the keystone, so the grant is read only from a
leaf the agent cannot open. That refusal does NOT trust ``agent.sandbox``: it lives in
this same agent-writable ``config.json``, so a sandboxed process could set ``sandbox:
"off"`` through the ``link(2)`` route to pretend the host is unmaskable — the refusal
therefore decides from whether a confined mode WOULD mask the leaf on this host, and a
requested ``off`` on a maskable host is refused, not granted. On delegated macOS neither
source is honoured (the delegated kiro-cli spawn runs unmasked and could author the
leaf), so the operator uses the ad-hoc grant there. The deprecation is announced rather
than silent: :func:`migration_notice` returns the words a startup logs when the key is
set and the keystone is absent, naming the keystone to move to. The key is slated to
stop granting in a future release, which needs a recorded decision first.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from typing import Any

from kiro_crew import sandbox
from kiro_crew.config.loader import standing_approval_path

logger = logging.getLogger(__name__)

#: The field inside the keystone document. Spelled the same as the deprecated
#: ``config.json`` key on purpose: an operator moving the declaration copies one line
#: rather than learning a second name for the same switch.
GRANT_FIELD: str = "dangerously_skip_permissions"

#: Size bound for the grant document. It holds one boolean, so a few hundred bytes is
#: generous; anything larger is not a legitimate grant. The bound is what stops a huge
#: file at this name from being buffered into the gateway's memory on a synchronous
#: boot read.
_MAX_GRANT_BYTES: int = 4096


def _read_grant_bytes() -> bytes | None:
    """Read the grant document, refusing a link, a non-regular file or a second name.

    A plain ``read_text()`` follows a symlink, which would undo this leaf's whole
    point. The keystone directory is bind-masked and pre-created, so an in-sandbox
    process cannot place the link itself -- but an operator who aliases the document
    onto a sandbox-visible path (a dotfile manager is the ordinary way that happens)
    would hand the agent a writable name for the file that authorizes it. The reader
    refuses that shape rather than resolving it, which is the same disposition
    ``sandbox`` takes for a ceiling it cannot cover under a second name.

    Defences, in order, following :func:`session_pid_sig._read_regular_nofollow`:

    * ``O_NOFOLLOW | O_DIRECTORY`` on the PARENT directory, then an ``openat`` of the
      leaf relative to that pinned descriptor (``dir_fd``): ``O_NOFOLLOW`` on the leaf
      alone refuses a symlinked ``grant.json`` but NOT a symlinked ``standing-approval``
      directory, and the mask predicate matches by ``normpath`` without resolving links,
      so an operator-aliased directory (a dotfile manager is the ordinary way that
      happens) would still answer masked while handing the agent a writable parent for
      the file that authorizes it. Pinning the parent no-follow refuses the aliased
      directory at the open, and the dir_fd-relative open cannot traverse out of it.
    * ``O_NOFOLLOW`` (POSIX) on the leaf: the open itself refuses a symlink final
      component, race-free where an ``is_symlink()`` pre-check is not.
    * ``lstat`` pre-check plus a post-open identity check, for the one platform with
      no ``O_NOFOLLOW``: a link planted between the two opens its TARGET, whose
      ``(st_dev, st_ino)`` cannot match the vetted regular file.
    * ``S_ISREG``: rejects a FIFO or device at that name. ``O_NONBLOCK`` is in the
      open flags for that case specifically: a blocking ``O_RDONLY`` open of a FIFO
      with no writer never returns, so without it a FIFO planted at this name would
      hang the gateway's boot instead of being rejected by the check below. The flag
      is ignored for a regular file, which is every legitimate case.
    * ``st_nlink == 1``: a second hard link is a second writable name for this very
      inode, and the mask covers a path rather than an inode -- the exact shape this
      keystone exists to deny, so the reader must not accept a document carrying one.
    * A size bound checked against both ``fstat`` and the bytes actually read.

    **Why the deltas are not folded into that shared reader.**
    :func:`session_pid_sig._read_regular_nofollow` is read by ``session_pid_sig`` and
    ``session_token_sig``, and neither delta here is neutral for them. ``st_nlink == 1``
    is a REFUSAL, so folding it in refuses a legitimately hard-linked pid or token
    signature -- the ordinary output of a snapshot or dotfile tool, and harmless for those
    two because their documents are not authorizations: a second name for one grants
    nothing, while a second name for THIS one is the whole concern. Making the refusal
    conditional adds a parameter whose meaning is "be an authorization reader", which is
    this function under another name. ``O_NONBLOCK`` could fold on its own and would close
    a FIFO hang on that helper's own agent-writable mapping directory, but that changes
    two other modules' startup behaviour and belongs with whoever owns them rather than
    riding in on this leaf.

    Returns ``data`` on success -- the vetted document bytes -- and ``None`` on any
    refusal or I/O error, so every caller fails closed. The descriptor's release counts
    as part of the read: a failing ``close`` is caught here and resolves to no grant,
    because this reader runs on the gateway's startup thread outside any ``try``, so an
    error escaping it would abort boot instead of withholding a grant.
    """
    path = standing_approval_path()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    pre: os.stat_result | None = None
    o_directory = getattr(os, "O_DIRECTORY", 0)
    parent = os.path.dirname(path)
    leaf = os.path.basename(path)
    parent_fd: int | None = None
    try:
        if nofollow and o_directory:
            # Pin the PARENT DIRECTORY no-follow, then open the leaf RELATIVE to that
            # pinned descriptor (openat via dir_fd). O_NOFOLLOW on the leaf alone
            # refuses a symlinked ``grant.json`` but not a symlinked ``standing-approval``
            # directory: an operator who aliases the DIRECTORY (a dotfile manager is the
            # ordinary way that happens) onto a sandbox-visible target hands the agent a
            # writable parent for the file that authorizes it, while the mask predicate --
            # which matches by ``normpath`` and never resolves links -- still answers
            # "masked". O_NOFOLLOW | O_DIRECTORY on the parent refuses the aliased
            # directory at the open, race-free, and the subsequent dir_fd-relative open
            # cannot traverse out of the pinned inode.
            parent_fd = os.open(parent, os.O_RDONLY | nofollow | o_directory | nonblock)
            fd = os.open(leaf, os.O_RDONLY | nofollow | nonblock, dir_fd=parent_fd)
        else:
            # No O_NOFOLLOW/O_DIRECTORY (Windows): the keystone grants nothing on any
            # delegated-sandbox platform (`is_declared` refuses there), so this is the
            # best-effort leaf guard -- lstat pre-check plus the post-open identity check
            # below -- rather than a parent pin the platform cannot promise.
            if not nofollow:
                pre = os.lstat(path)
                if stat.S_ISLNK(pre.st_mode):
                    logger.warning("standing auto-approve keystone is a symlink; refusing it")
                    return None
            fd = os.open(path, os.O_RDONLY | nofollow | nonblock)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "standing auto-approve keystone could not be opened as a plain file; "
            "treating approvals as required"
        )
        return None
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
    closed_cleanly = True
    try:
        st = os.fstat(fd)
        if pre is not None and (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
            return None
        if not stat.S_ISREG(st.st_mode):
            logger.warning("standing auto-approve keystone is not a regular file; refusing it")
            return None
        if st.st_nlink != 1:
            logger.warning(
                "standing auto-approve keystone has %d hard links, so the grant is "
                "reachable under another name; refusing it",
                st.st_nlink,
            )
            return None
        if st.st_size > _MAX_GRANT_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = _MAX_GRANT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            closed_cleanly = False
    if not closed_cleanly:
        logger.warning(
            "standing auto-approve keystone could not be closed; treating approvals as required"
        )
        return None
    data = b"".join(chunks)
    if len(data) > _MAX_GRANT_BYTES:
        return None
    return data


def _read_all() -> dict[str, Any]:
    """The whole document, or ``{}`` when it is missing, refused or unreadable.

    Failing soft is the right READ behaviour: an authorization record that cannot be
    parsed is not an authorization, so the grant stays withheld and the session
    prompts. An absent document, a refused shape and an unparseable one resolve
    identically, which is also what makes the empty mask a sandboxed reader would see
    equal to no grant.
    """
    read = _read_grant_bytes()
    if read is None:
        return {}
    try:
        raw = json.loads(read.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning(
            "standing auto-approve keystone is unreadable; treating approvals as required"
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _mask_covers_leaf_state(requested_mode: str) -> str:
    """Three-state version of :func:`_mask_covers_leaf`.

    Returns ``"covered"``, ``"uncovered"``, or ``"indeterminate"``. The last means the
    sandbox backend probe could not conclude (a transient or deferred failure at
    startup), so we cannot yet tell whether the mask covers the leaf. A mask-dependent
    control must treat ``indeterminate`` as fail-SAFE, never as ``uncovered``: a
    transient, uncached probe failure that read as ``uncovered`` would let the
    agent-readable alias refusal fail OPEN and install a standing grant during ordinary
    startup probe pressure.

    * :func:`sandbox.credential_mask_state` answers, three-valued, whether ``wrap_argv``
      would thread the hidden-dir set through the backend it selects for *requested_mode*
      -- ``applies`` / ``absent`` (a settled no) / ``indeterminate`` (an inconclusive
      probe). It owns the clamp to the governed floor, the ``off`` tier, the no-backend
      host, and the process already running inside a Crew sandbox.

    ``normpath``, never ``realpath``: the target set is built with ``normpath`` for the
    reason :func:`sandbox._relocated_crew_targets` records, so a link-resolving syscall
    here could disagree with the rules the sandbox will actually install.
    """
    try:
        state = sandbox.credential_mask_state(requested_mode)
        if state == "indeterminate":
            return "indeterminate"
        if state != "applies":
            return "uncovered"
        leaf = os.path.normpath(str(standing_approval_path()))
        for target in sandbox._crew_hidden_sandbox_targets():
            masked = os.path.normpath(target)
            if leaf == masked or leaf.startswith(masked + os.sep):
                return "covered"
        return "uncovered"
    except Exception:
        logger.warning(
            "standing auto-approve keystone mask could not be established; "
            "treating approvals as required",
            exc_info=True,
        )
        # An unexpected error is itself an inconclusive answer: fail SAFE.
        return "indeterminate"


def _mask_covers_leaf(requested_mode: str) -> bool:
    """Whether Crew's own sandbox for *requested_mode* actually covers the keystone leaf.

    This is the DELEGATION-BLIND question: would ``wrap_argv`` thread the hidden-dir set
    through the backend it selects for this mode, and does that masked target set name
    the keystone leaf. It does NOT ask whether a particular spawn is handed off -- the two
    named predicates below layer that on, because the alias and the keystone grant need
    OPPOSITE answers about a delegated host.

    Two-valued over the three-valued :func:`_mask_covers_leaf_state`: an
    ``indeterminate`` probe maps to ``False`` here (the mask is not established), which is
    the fail-safe direction for the keystone-honour check that reads this. The alias
    refusal reads the three-valued helper directly, because it must refuse on
    ``indeterminate`` rather than grant.
    """
    return _mask_covers_leaf_state(requested_mode) == "covered"


def deprecated_alias_is_refused(requested_mode: str) -> bool:
    """Whether the agent-readable ``config.json`` alias must be REFUSED on this host.

    The alias is agent-readable, so honouring it reopens the ``link(2)`` route the
    keystone exists to close -- on any host where a Crew-masked spawn is reachable, so the
    keystone is available as the replacement. That is every host where Crew's mask covers
    the leaf for SOME spawn:

    * Masked Linux / macOS -> refuse (the keystone is the secure home).
    * Delegated macOS (kiro-cli's internal sandbox on) -> refuse: delegation covers the
      kiro-cli spawn ALONE, while a KAS/``--agent-engine v3`` session, an MCP probe or a
      cron script keeps Crew's Seatbelt mask, so the leaf is masked for them and the
      agent-reachable alias must not stand.
    * Native Windows -> NOT refused: there is no Crew OS backend, EVERY spawn is unmasked,
      the keystone can never be in force, so the alias stays the only standing path and
      keeps granting (no platform narrowed).

    So the alias is refused wherever Crew's mask covers the leaf AND the host is not the
    all-spawns-unmasked case (native Windows).

    Fail-SAFE on an inconclusive probe: if the sandbox backend probe could not conclude
    (``_mask_covers_leaf_state`` -> ``"indeterminate"``, a transient or deferred failure
    at startup), the alias is REFUSED, not granted. Only a DEFINITE ``"uncovered"`` -- a
    settled no-mask host -- keeps the alias granting. A transient, uncached probe failure
    that read as ``"uncovered"`` would let this refusal fail OPEN and install a standing
    skip of every approval during ordinary startup probe pressure; refusing on
    ``indeterminate`` closes that. The ad-hoc (time-limited) grant is unaffected.
    """
    if sandbox.sys.platform == "win32" and sandbox.spawn_delegates_masking():
        return False
    state = _mask_covers_leaf_state(requested_mode)
    # Refuse on "covered" AND "indeterminate"; a settled "uncovered" would otherwise
    # keep the alias granting.
    if state != "uncovered":
        return True
    # A settled "uncovered" is NOT yet proof this host cannot mask. ``requested_mode``
    # is ``agent.sandbox`` read from the same ``config.json`` the alias lives in, and
    # that document is agent-writable through the ``link(2)`` route the keystone exists
    # to close: a sandboxed process that can give the file a second name and write
    # ``dangerously_skip_permissions: true`` through it can equally write
    # ``sandbox: "off"`` in the same write. A requested ``off`` short-circuits
    # ``credential_mask_state`` to ``"absent"`` BEFORE any backend probe, so trusting it
    # here would let the agent turn its own refusal back off on a host whose real backend
    # would mask the leaf -- the refusal failing OPEN to the very actor it defends against.
    #
    # So an ``uncovered`` that rests on a requested ``off`` is only honoured once the HOST
    # itself is confirmed unmaskable -- a fact the agent cannot forge in ``config.json``.
    # ``sandbox.host_can_mask_leaf()`` asks whether a confined tier would mask the leaf on
    # this host, independent of the requested mode. If it can, the alias stays refused and
    # the keystone (available on a maskable host) is the operator's standing path; only a
    # host that genuinely cannot mask -- native Windows, a true no-backend host, where the
    # keystone can never be in force anyway -- keeps the alias granting.
    if sandbox._clamp_sandbox_mode(requested_mode) == "off" and host_can_mask_leaf():
        return True
    return False


def host_can_mask_leaf() -> bool:
    """Whether SOME confined tier would mask the keystone leaf on this host.

    Mode-INDEPENDENT: it answers a property of the host, not of the agent-writable
    ``agent.sandbox`` value. :func:`deprecated_alias_is_refused` reads it so an
    agent-forged ``sandbox: "off"`` cannot pretend the host is unmaskable and turn the
    alias refusal back off -- the backend capability a confined tier would select is the
    thing the agent cannot write into ``config.json``.

    Fail-SAFE: an ``indeterminate`` probe (a transient startup failure) counts as "the
    host can mask", so the refusal that reads this stays ON rather than failing open
    under ordinary startup probe pressure, matching :func:`_mask_covers_leaf_state`.
    """
    # Probe with a confined tier (never the possibly-forged requested mode), so the
    # ``off`` short-circuit in ``credential_mask_state`` cannot suppress a real backend.
    probed = _mask_covers_leaf_state("auto")
    return probed != "uncovered"


def _keystone_is_masked(requested_mode: str) -> bool:
    """Whether a keystone ``grant.json`` can be TRUSTED as the operator's own on this host.

    The document's authority rests on the mask, not on its own contents: an agent
    subprocess that can see the real data home creates the directory and writes the grant
    itself, and the next startup reads what that subprocess wrote as the operator's
    standing authority. So the grant counts only where NO reachable spawn can write the
    leaf unmasked.

    This is STRICTER than :func:`deprecated_alias_is_refused`, and the two differ on a
    delegated macOS host, which is why the predicate is split:

    * Masked Linux / macOS -> honoured: every spawn is Crew-masked, no spawn can author
      the leaf, so a ``grant.json`` is the operator's.
    * Delegated macOS (kiro-cli's internal sandbox on) -> NOT honoured: the kiro-cli spawn
      runs WITHOUT Crew's mask (``_delegate_to_kiro_internal_sandbox``), so an agent on
      that spawn can reach and write the leaf and startup would trust its own grant. The
      alias is already refused here (above); the keystone grant must be refused too, so a
      delegated host honours NEITHER standing source.
    * Native Windows -> NOT honoured: no Crew backend masks the leaf for any spawn.

    ``requested_mode`` is the live ``agent.sandbox`` the caller resolved immediately
    before activation (never a boot copy), passed unclamped -- the clamp belongs to
    :func:`sandbox.credential_mask_applies`.
    """
    # Delegation removes Crew's mask for the spawns it hands off. The alias refusal above
    # tolerates that (a non-kiro-cli spawn still keeps the mask), but TRUSTING a written
    # grant cannot: on a delegated host at least one reachable spawn (native Windows: all;
    # macOS internal sandbox: the kiro-cli spawn) runs unmasked and could author the leaf.
    # So any delegating host refuses to honour the grant, where the alias check refuses
    # only the native-Windows all-spawns-unmasked case.
    if sandbox.spawn_delegates_masking():
        return False
    # _mask_covers_leaf maps an INDETERMINATE probe to False, so an inconclusive
    # startup probe does not honour a written grant either -- the fail-safe direction,
    # matching the alias refusal above.
    return _mask_covers_leaf(requested_mode)


def is_declared(requested_mode: str) -> bool:
    """Whether the operator has declared a STANDING skip of every tool approval.

    Fails closed to ``False`` on a missing, unreadable or malformed document, which
    is what the issue's "an absent or unreadable leaf resolves to refusal" asks for.

    Only a real ``bool`` ``True`` counts. A truthy string or number does not, for the
    reason ``config.sections._read_skip_permissions`` gives about the key it replaces:
    ``"false"``, ``"0"`` and ``"no"`` are all truthy in Python, so a bare ``bool(...)``
    here would read an explicit disable as the standing grant. LOCAL only -- no network.

    A declared document is honoured only where :func:`_keystone_is_masked` holds, so an
    agent subprocess that can reach the leaf cannot author its own standing authority.
    The mask is established only once the document already grants, so a host with no
    grant -- every default install -- pays nothing for the check and stays silent;
    a host that HAS one and cannot mask it says so, because an operator who wrote the
    document and still sees prompts needs the reason.

    **One residual, stated honestly.** The mask closes the ONGOING threat: on this
    version no agent subprocess can open or write the keystone, so no session can author
    its own standing authority. One bounded, one-time window remains: on a release BEFORE
    this leaf was masked the crew data-home root was agent-writable and nothing masked
    ``standing-approval/``, so a pre-upgrade agent could write ``grant.json`` =
    ``True`` and an upgrade would read it back. That residual is irreducible by any
    on-disk artifact -- at the first masked boot every file in the then-unmasked directory
    is agent-writable, so no on-disk marker, mtime or nonce can be authenticated as
    the gateway's rather than one the agent wrote -- and closing it needs a trust root the
    pre-upgrade agent never held (a gateway secret minted after the window), which
    is out of this change's scope and tracked separately. This PR was SHRUNK per the audit
    to the minimal masked-leaf move; the earlier first-trusted-init marker/quarantine
    machinery was removed because it could not close that residual either and added an
    on-disk trust root that the agent could itself write.

    *requested_mode* is forwarded to :func:`_keystone_is_masked` and is required: pass
    the sandbox tier IN FORCE NOW, resolved from :func:`config.live.current` immediately
    before activation -- never a boot copy cached at startup. The grant is honoured only
    where the live sandbox masks the keystone, so a mode that went stale while the gateway
    booted (an operator flips ``agent.sandbox`` off, or down to an unmasked tier, between
    startup and this call) must not activate a grant the live host does not protect. The
    two startup paths (dashboard and Slack) read the live snapshot and pass its
    ``agent.sandbox`` here, so a mid-flight unmask refuses rather than granting under a
    mask that is not installed.
    """
    document = _read_all()
    if document.get(GRANT_FIELD) is not True:
        return False
    if not _keystone_is_masked(requested_mode):
        logger.warning(
            "standing auto-approve is declared on the keystone but is UNAVAILABLE on "
            "this host: Kiro Crew's own sandbox does not mask that leaf away from the "
            "agent here, so the document is not an authorization. Approvals are required"
        )
        return False
    # The mask holds and the grant is a real boolean True: an agent subprocess cannot
    # open or write the keystone on this version, so the document is the operator's
    # standing authorization and is honoured on every startup.
    return True


def migration_notice(
    requested_mode: str,
) -> str:
    """The words a startup logs when a DEPRECATED ``config.json`` declaration is in use.

    Which words depends on the mask. Where the keystone mask is UNAVAILABLE the config key
    still grants (platforms the keystone cannot cover are not narrowed), so the notice
    tells the operator the key is deprecated and that nothing need change on this host.
    Where the mask HOLDS the config key is refused -- it is agent-reachable, so
    honouring it would reopen the route the keystone closes -- so the notice tells the
    operator to move the declaration to the keystone, which is available there. Returned
    rather than logged here so the two startup paths that establish the declared grant (the
    dashboard's and Slack's) word it identically, and so a test can assert on the text an
    operator actually sees instead of on a log call.

    The whole value of this notice is that the person reading it can act on it without
    going to find the documentation first, so it names the resolved path and the one
    line to put in it. It deliberately does NOT hand over a runnable redirection
    command on any platform:

    * It does not spell the path as ``$KIROCREW_HOME/...``. That variable is unset on a
      default installation, where the data home comes from ``config_dir()``, so a shell
      expands the env-var form to ``/standing-approval`` and a command using it fails at
      the filesystem root.
    * It does not hand a Windows operator a POSIX one-liner. ``mkdir -p`` and ``printf``
      are not commands there, and POSIX single quotes are not quoting characters to
      ``cmd`` at all -- the same trap :func:`sandbox._delete_file_command` records.
    * It does not hand a POSIX operator a ``printf ... > path`` redirection either. A
      crew data-home root is agent-writable, so a symlink could sit at the keystone path,
      and an operator who copy-pastes such a line would follow that symlink and overwrite
      whatever it points at with operator privilege.
      So BOTH renderings name the path and the exact one line and ask the operator to
      create the file by hand -- no redirection this notice emits can be turned against
      them.

    *requested_mode* -- required, like :func:`is_declared`'s -- selects the rendering
    through two checks, because the alias and the keystone can disagree:
    :func:`deprecated_alias_is_refused` says whether the config key still grants, and
    :func:`_keystone_is_masked` says whether writing the keystone would restore the grant.

    **There are four renderings.** Where the alias is NOT refused (native Windows, an
    unmasked tier) the key keeps working and nothing need change. Where the alias is
    refused AND the keystone would be honoured (masked Linux, non-delegated macOS),
    writing the document restores the grant, so the notice hands over the path and the
    exact one line to write. Where the alias is refused but the keystone would NOT be
    honoured either -- delegated macOS, where the kiro-cli spawn runs unmasked so neither
    source can be trusted -- the standing grant is unavailable on this host: the notice
    states that and points at the ad-hoc duration, the grant that does work there, rather
    than promising a keystone remedy that :func:`is_declared` then refuses. And where the
    sandbox probe could not CONCLUDE (``_mask_covers_leaf_state`` -> ``"indeterminate"``,
    a transient or deferred failure at startup) the alias is refused fail-safe and no
    keystone is honoured, so the standing grant is unavailable this boot: the notice says
    the sandbox state could not be determined, points at the ad-hoc duration, and notes a
    restart re-probes -- it does NOT tell the operator the spawn is delegated, because the
    cause is an inconclusive probe, not delegation.
    """
    alias_refused = deprecated_alias_is_refused(requested_mode)
    keystone_honoured = _keystone_is_masked(requested_mode)
    mask_state = _mask_covers_leaf_state(requested_mode)
    # A requested ``off`` can be a value an agent wrote into ``config.json`` through the
    # ``link(2)`` route, which short-circuits the mask reads above to "uncovered"/not
    # honoured regardless of the real host. ``deprecated_alias_is_refused`` already looks
    # past that by probing host maskability directly, so when the alias is refused but the
    # mask reads uncovered ONLY because the requested mode is an (agent-forgeable) ``off``
    # on a host a confined tier WOULD mask, the keystone is the available remedy -- render
    # it so, rather than falling through to the "neither source trusted" (delegated)
    # message that would misstate why on this host. Gated tightly so the genuine cases are
    # untouched: it fires only on the forged-off signature (mask_state "uncovered" for the
    # requested mode) AND a DEFINITE confined-tier mask ("covered", never the fail-safe
    # "indeterminate" of a transient probe) AND no delegation -- the exact combination that
    # separates a forged ``off`` on masked Linux/macOS from delegated macOS and from an
    # inconclusive probe, both of which keep their own renderings below.
    if (
        alias_refused
        and mask_state == "uncovered"
        and not sandbox.spawn_delegates_masking()
        and _mask_covers_leaf_state("auto") == "covered"
    ):
        keystone_honoured = True
        mask_state = "covered"
    path = standing_approval_path()
    document = f'{{"{GRANT_FIELD}": true}}'
    if not alias_refused:
        return (
            "agent.dangerously_skip_permissions is set in config.json. It still grants "
            "the standing auto-approve, but it is DEPRECATED: the secure home for this "
            f"declaration is the operator-owned keystone {path}, which an agent sandbox "
            "cannot open. On THIS host, however, Kiro Crew's own sandbox does not mask "
            "that leaf away from the agent, so the keystone cannot be honoured here and "
            "the config key remains the only standing path for now. Nothing to change on "
            "this host; the config key keeps working."
        )
    if mask_state == "indeterminate":
        # The sandbox backend probe could not conclude (a transient or deferred failure
        # at startup). We refuse the alias fail-safe and do NOT honour a keystone, so the
        # standing grant is unavailable this boot -- but the cause is an inconclusive
        # probe, not delegation, so the operator must not be told the spawn is delegated.
        # Point at the ad-hoc grant (which works) and at a restart, which re-probes.
        return (
            "agent.dangerously_skip_permissions is set in config.json. On THIS host the "
            "standing auto-approve is unavailable this startup because the sandbox state "
            "could not be determined -- the backend probe did not conclude, so Kiro Crew "
            "cannot confirm the keystone leaf is masked and refuses the agent-readable "
            "key fail-safe rather than grant on an unknown mask. Use the ad-hoc grant "
            "instead (the dashboard, Slack or the API, which expire on "
            "agent.yolo_duration); a restart re-probes the sandbox and, where the mask "
            f"holds, the operator-owned keystone {path} is then the standing home."
        )
    if not keystone_honoured:
        # Delegated macOS: the alias is refused (a Crew-masked spawn is reachable) AND the
        # keystone is not honoured (the delegated kiro-cli spawn runs unmasked and could
        # author the leaf). Neither source is trusted, so the standing grant is
        # unavailable here. Do NOT send the operator to write a keystone that is_declared
        # would refuse; name the ad-hoc duration, the grant that works on this host.
        return (
            "agent.dangerously_skip_permissions is set in config.json. On THIS host it is "
            "refused as a standing auto-approve, and the operator-owned keystone cannot be "
            "honoured here either, because the agent spawn is delegated to kiro-cli's own "
            "sandbox and runs without Kiro Crew's mask -- so neither source is trusted and "
            "the no-expiry standing grant is unavailable on this host. Use the ad-hoc "
            "grant instead (the dashboard, Slack or the API, which expire on "
            "agent.yolo_duration), or run on a tier where Kiro Crew's own sandbox masks "
            "the keystone leaf."
        )
    preamble = (
        "agent.dangerously_skip_permissions is set in config.json. On THIS host it is "
        "refused: it is DEPRECATED and the key is an agent-readable document that "
        "can be reached through a second name, so honouring it would let a sandboxed "
        "process grant itself the standing auto-approve -- which is exactly what the "
        f"operator-owned keystone {path} prevents (an agent sandbox cannot open it). The "
        "standing declaration must move to the keystone, which is available here. "
    )
    # Deliberately NOT a runnable redirection command. The crew data-home root is
    # agent-writable, so a symlink could sit at ``path``, and an operator who copy-pastes a
    # ``printf ... > path`` line would follow that symlink and overwrite whatever it points
    # at with operator privilege. The notice names the path and the exact one-line content
    # and asks the operator to create the file by hand, so no redirection this notice emits
    # can resolve anywhere unexpected.
    remedy = (
        f"To migrate, create the directory {path.parent} if it does not exist, then "
        f"create the file {path} by hand containing exactly this one line: {document} . "
        "Restart Kiro Crew and the keystone grant is honoured. "
    )
    return preamble + remedy + "(then remove the deprecated key from config.json)."
