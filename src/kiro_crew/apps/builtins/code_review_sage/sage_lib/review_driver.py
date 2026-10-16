#!/usr/bin/env python3
"""Review driver — code-enforced single-pass review per pull request.

The clean-session-per-change guarantee is not left to the LLM. This
deterministic driver owns the dispatch:

  Review     — one isolated session per pull request reviews every changed file
               in a single pass (design reasoning is one dimension of it, not a
               separate gate) and writes the complete result record.
  Follow-up  — only when that record reports ``coverage_complete=false``, the
               driver dispatches AT MOST ONE coverage follow-up session that
               reviews the still-uncovered files and appends net-new findings.

Every session runs on a **reusable worker pool** (``sage_lib/review_pool.py``): a bounded
set of long-lived ``AcpClient`` sessions, NOT a fresh ``/api/spawn`` sub-agent
per change. The driver hands each task to the pool via an injected ``dispatch``
callable and the call returns when that task's session finishes its turn (i.e.
the result record is on disk) — so there is no done-flag polling, no lingering
worker, and no reaper. Because pool workers are direct ACP sessions they bypass
the SubagentManager entirely: no agent card, no ``:lock:`` approval prompt, no
Slack relay — the review runs silently. Each reused worker is reset to a clean
conversation between CRs so reviews never cross-contaminate.

The driver then builds the Focus Report deterministically. The orchestrating
session cannot review inline because the driver owns the dispatch. The per-change
*judgment* (the gate verdict and the findings) still runs in each isolated worker
session using the code-review-sage ruleset — Python enforces the structure and
the follow-up bound, not the verdict itself.

Usage:
    python3 sage_lib/review_driver.py run --changes "<pr-url>[,<pr-url>...]" [--concurrency 3]
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

# Optional KiroCrew runtime dep (absent when running standalone / in tests).
# Kept at module top per the imports guideline; guarded at each use site.
try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback
    redact_credentials = redact_exfiltration_urls = None  # type: ignore

# The shared read-degrade helper is also a Kiro Crew runtime dep. Standalone the
# fallback keeps today's silent tolerance so `python3 sage_lib/review_driver.py`
# still runs; under the gateway the real helper adds the one warning.
try:
    from kiro_crew.atomic_write import read_json_or  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback

    def read_json_or(path, default, *, logger=None, what=None):  # type: ignore
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default


# Every loopback call below carries X-Internal-Secret, and urlopen honours
# HTTP_PROXY for loopback addresses, so a proxied environment would send the
# secret to the proxy in cleartext. The fallback must therefore stay
# proxy-disabled too -- degrading to a bare urlopen would leave the standalone
# path (`python3 sage_lib/review_driver.py`) carrying the leak this closes.
#
# It must ALSO refuse redirects, for the same reason the real helper does. An
# earlier version of this fallback omitted that on the grounds that "these
# endpoints never return a redirect" -- wrong for `_probe`, whose entire job is
# to dial candidate ports that may have something other than our gateway
# listening. A 302 from one of those would replay the secret to whatever host
# Location names.
try:
    from kiro_crew.loopback_http import loopback_urlopen  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback

    class _FallbackNoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def loopback_urlopen(req: urllib.request.Request | str, timeout: float):  # type: ignore
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _FallbackNoRedirect()
        ).open(req, timeout=timeout)


_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/review_driver.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import (  # noqa: E402
    adapters,
    discovery,
    followup,
    pipeline,
    report,
    results,
    review_pool,
    store,
)

# Whole definitions moved to sibling modules to keep this file under the line cap.
# Imported back so every ``review_driver.<name>`` reference and patch target still
# resolves here; the callers in this module read them from this namespace.
from sage_lib.delivery_intent import (  # noqa: E402,F401 -- historical module export
    _INTENT_ERROR_MAX_CHARS,
    _OPERATION_ID_RE,
    _OPERATION_MARKER_PREFIX,
    _OPERATION_MARKER_SUFFIX,
    DeliveryProbe,
    _canonical_payload,
    _canonical_target,
    _confirm_text,
    _intent_payload,
    _marker_free_body,
    _operation_marker,
    _outbound_payload,
    _payload_digest,
    _predecessor_instruction,
    _write_intent,
)
from sage_lib.review_tasks import (  # noqa: E402,F401 -- historical module export
    _confirmed_host,
    _fetch_instruction,
    build_consolidation_task,
    build_post_task,
    build_review_followup_task,
    build_review_task,
    python_command,
)


def _redact(text: str) -> str:
    """Scrub credentials + exfiltration URLs from LLM-generated text before it is
    posted to an external surface (the dashboard artifact store). No-op when the
    KiroCrew redaction lib isn't importable (standalone)."""
    if redact_exfiltration_urls is None or redact_credentials is None:
        return text
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


DEFAULT_TASK_TIMEOUT = 5400  # 90 min per review turn (the governing cap — passed
#   through run_review -> _one -> dispatch -> pool.send -> handle.prompt). A single
#   thorough pass needs headroom that a 30-min cap would force-kill on large PRs.
#   Stays under the runtime's 2h prompt default.
_REPORT_ARTIFACT_TAG = "sage-report"  # tags every per-run report artifact
DEFAULT_REPORT_RETENTION = 20  # keep the N most-recent report artifacts; prune older


def _api_request(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    """Authenticated loopback call to the gateway API. Never raises."""
    base = _gateway_base()
    # The credential belongs to the gateway this call DIALS, so it is read for the
    # port carried by the resolved base rather than from the home-wide file, which
    # names whichever generation wrote it last. An unparseable base leaves no dial
    # target to name, and a credential for an unknown gateway is not a thing that
    # exists -- so that is an error, not a reason to fall back to a home-wide read.
    try:
        base_port = int(base.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return {"error": f"could not read a port from the gateway base {base!r}"}
    secret = _local_secret(base_port)
    if not secret:
        return {"error": "gateway IPC secret unavailable"}
    headers = {"X-Internal-Secret": secret}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with loopback_urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"error": str(e)}


def _prune_old_reports(keep: int) -> None:
    """Best-effort: keep only the N most-recent report artifacts (by updated_at);
    delete older ones so the artifact list doesn't grow unbounded."""
    lst = _api_request("GET", "/api/artifacts?tag=" + _REPORT_ARTIFACT_TAG)
    items = lst.get("artifacts") if isinstance(lst, dict) else None
    if not items:
        return
    items = sorted(items, key=lambda a: a.get("updated_at", ""), reverse=True)
    for a in items[max(0, keep) :]:
        slug = a.get("slug")
        if slug:
            _api_request("DELETE", "/api/artifacts/" + slug)


def _archive_report(html_body: str, root: Path | None = None) -> str | None:
    """Create a NEW report artifact for this run (one per run, not versions of a
    single artifact) and prune old ones. Returns the new slug, or None on failure."""
    html_body = _redact(html_body)  # scrub LLM output before posting to the dashboard
    ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    slug = "sage-report-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    d = _api_request(
        "POST",
        "/api/artifacts",
        {
            "name": "Code Review Sage Report — " + ts,
            "content": html_body,
            "kind": "widget",
            "tags": ["cr", _REPORT_ARTIFACT_TAG],
            "slug": slug,
        },
    )
    if d.get("error"):
        return None
    new_slug = d.get("slug") or slug
    _prune_old_reports(DEFAULT_REPORT_RETENTION)
    return new_slug


def _default_archiver(html_body: str, root: Path | None = None) -> str | None:
    return _archive_report(html_body, root)


def archive_report(html_body: str, root: Path | None = None) -> str | None:
    """Publish a report HTML body as a dashboard artifact; returns the slug or None.

    Public entry point for the backend's on-demand "share / export" route, which
    re-archives a run whose automatic archive failed. Same redaction and pruning
    as the automatic path."""
    return _archive_report(html_body, root)


def _resolve_concurrency(explicit: int | None = None) -> int:
    """Effective driver fan-out: an explicit value wins; otherwise default to the
    worker pool's concurrency cap.

    Pool workers are direct ACP sessions (NOT ``/api/spawn`` sub-agents), so the
    gateway sub-agent cap does not apply — ``review_pool.effective_max_concurrent()``
    is the single source of truth for how many reviews run at once. The pool also
    hard-caps concurrency itself, so this only governs how many tasks the driver offers it."""
    if explicit and explicit > 0:
        return max(1, int(explicit))
    return max(1, review_pool.effective_max_concurrent())


def _cid(link: str) -> str:
    """Derive the change id from a GitHub PR link — filesystem-safe. A PR URL ->
    ``GH-<owner>-<repo>-<n>`` (matching the id ``adapters.parse_github_payload``
    records, so the worker's written record and the driver's read hit the same
    file); otherwise a sanitized fallback (never a raw URL, which is not a valid
    filename)."""
    try:
        host, owner, repo, number = pipeline.adapters.github_pr_ref(link)
        return pipeline.adapters.github_change_id(owner, repo, number, host=host)
    except pipeline.adapters.AdapterParseError:
        return results.safe_change_id(link)


def change_id_for(link: str) -> str:
    """Public alias for the change-id derivation. The app backend uses this to
    store the SAME key the driver writes progress under on the run record, so the
    dashboard can align each row with its live phase (queued/gating/deep/done/failed)
    and render a human label. Keeping this in one place prevents the frontend from
    re-deriving the id (and drifting from the backend's sanitization, e.g. an owner
    hyphen becoming an underscore)."""
    return _cid(link)


def reviewed_key_for(link: str) -> str:
    """Collision-free key for the durable reviewed-index (``reviewed.json``).

    Separate from ``change_id_for``: the change-id is also an on-disk filename and
    is therefore lossily sanitized (``-`` -> ``_``), which let two different repos
    (``acme/service-api`` vs ``acme/service_api``) with the same PR number collide
    on one dedup key and skip a requested review. The reviewed-index key never
    names a file, so it uses the lossless canonical identity instead. Falls back to
    the sanitized change-id for a non-PR link (defensive; repo-review only ever
    feeds real PR URLs from ``list_open_prs``)."""
    try:
        host, owner, repo, number = pipeline.adapters.github_pr_ref(link)
        return pipeline.adapters.github_review_key(owner, repo, number, host=host)
    except pipeline.adapters.AdapterParseError:
        return results.safe_change_id(link)


logger = logging.getLogger(__name__)


def _forget_followup(run_id: str, change_id: str) -> None:
    """Drop this review's kept transcript, best-effort."""
    try:
        followup.forget(run_id, change_id)
    except Exception:  # pragma: no cover - never break the review
        logger.debug("dropping the kept review transcript failed", exc_info=True)


def _accepts_kwarg(dispatch: Callable[..., Any], name: str) -> bool:
    """Whether a dispatch callable accepts the keyword ``name``.

    Inspected rather than probed with try/except TypeError, which would also
    swallow a TypeError raised from INSIDE the dispatcher and silently downgrade
    the feature. Keeps injected test fakes and older dispatchers working: an
    unsupported keyword is simply not passed.
    """
    try:
        return name in inspect.signature(dispatch).parameters
    except (TypeError, ValueError):
        return False


def _accepts_activity(dispatch: Callable[..., Any]) -> bool:
    """Whether a dispatch callable takes the ``on_activity`` reporter."""
    return _accepts_kwarg(dispatch, "on_activity")


_RESOLVED_BASE: str | None = None


def _candidate_ports() -> list[int]:
    """Ports this process can claim as ITS OWN gateway, in order of authority.

    Only self-declared sources are eligible: ``KIROCREW_BOUND_PORT`` (exported by
    the parent gateway once its listener is bound), then ``KIROCREW_PORT``, then this
    home's ``config.json`` ``dashboard.url``. The bound port leads because it carries
    the port actually held: a gateway asked for 5476 but given 5477 has the truth
    only there, and a ``--port auto`` gateway has no other numeric source at all.
    A pod deliberately drops it so it never inherits its parent's listener.

    A blind sweep of the common gateway range is deliberately NOT included, because
    every probe carries that port's own credential -- so a sweep would authenticate
    against whichever sibling gateway answered first and this app would then create
    and delete review artifacts in that instance's store. A port we cannot justify
    as ours is not a discovery candidate; when no source names one the caller falls
    back to the default and the request errors clearly, which fails closed instead
    of writing to a stranger.
    """
    out: list[int] = []

    def _add(v) -> None:
        try:
            p = int(v)
        except (TypeError, ValueError):
            return
        if 1 <= p <= 65535 and p not in out:
            out.append(p)

    _add(os.environ.get("KIROCREW_BOUND_PORT"))
    _add(os.environ.get("KIROCREW_PORT"))
    try:
        cfg = store.crew_home() / "config.json"
        if cfg.exists():
            _cfg = read_json_or(cfg, {}, logger=logger, what="dashboard config")
            _d = (_cfg.get("dashboard") if isinstance(_cfg, dict) else None) or {}
            url = _d.get("url") or ""
            m = re.search(r":(\d+)", url)
            if m:
                _add(m.group(1))
    except Exception:
        pass
    return out


def _probe(base: str, secret: str) -> bool:
    """True if a KiroCrew gateway is listening at base (any HTTP response, incl.
    401/404, means it's there; only connection errors mean it isn't).

    Proxy-disabled deliberately: ``_gateway_base`` calls this once per candidate
    port, so a cold resolve that misses sends the secret up to len(candidates)
    times -- this is the highest-multiplicity secret-bearing send in the app."""
    try:
        req = urllib.request.Request(
            base + "/api/spawn", headers={"X-Internal-Secret": secret} if secret else {}
        )
        with loopback_urlopen(req, timeout=3) as resp:
            return resp.status < 500
    except urllib.error.HTTPError:
        return True  # a gateway responded (e.g. 401/404) — it's the right port
    except Exception:
        return False


def _gateway_base() -> str:
    """Resolve the LIVE gateway base URL by probing candidate ports (cached). The
    gateway may not run on 5476 and config.json dashboard.url is often empty, so a
    blind default sends spawns to a dead port — probing finds the real one."""
    global _RESOLVED_BASE
    if _RESOLVED_BASE:
        return _RESOLVED_BASE
    ports = _candidate_ports()
    for port in ports:
        # Dial the IPv4 loopback LITERAL (not the ambiguous ``localhost``): the
        # credential is paired to the address dialled, and a literal reaches one
        # family, so a gateway that bound only v4 -- or a wildcard/v4-only
        # container that publishes a single v4-family entry -- still authenticates.
        # Dialling ``localhost`` would demand BOTH families be covered and refuse
        # such an ordinary single-family gateway. Mirrors cli_server's _CLI_LOOPBACK.
        base = f"http://127.0.0.1:{port}"
        # Each candidate is probed with ITS OWN credential. A single secret read
        # before the loop comes from the home-wide file, which holds one slot per
        # data home: on a host running more than one gateway that names whichever
        # generation wrote last, so every probe against the others 403s and the
        # resolution falls through to a guess.
        if _probe(base, _local_secret(port)):
            _RESOLVED_BASE = base
            return base
    # No candidate answered. Fall back only to a port a source positively NAMED
    # (ports[0]); do NOT invent 5476. An unnamed default is a guess, and dialing it
    # with its own credential is how a report write lands in whatever SIBLING gateway
    # happens to own that port. When no source names a port at all, there is no base
    # to justify -- return empty so _api_request fails closed with a clear error
    # rather than authenticating against a stranger.
    return f"http://127.0.0.1:{ports[0]}" if ports else ""


def _local_secret(port: int) -> str:
    """Credential for the gateway on *port*, via the shared resolver.

    The per-port-then-shared order lives in ``config.loader.read_local_secret``;
    duplicating it here would give this surface its own copy to drift. This app
    addresses its data home through ``store.crew_home()``, so a home-wide read is
    the resolution used ONLY when the package import is unavailable (standalone
    mode). When the import succeeds the shared resolver is authoritative,
    INCLUDING its fail-closed refusal (a ``""`` return for an uncovered family or
    an unreadable ``run/``): this never falls through to the home-wide file on
    that refusal, which would send a different listener's credential and
    reintroduce the desync the shared resolver closes.

    *port* is required for the same reason it is required there: the credential is
    only valid for the gateway it belongs to, so the dial target is never inferred.
    """
    try:
        # Optional dependency, so function-local: this app also runs STANDALONE,
        # outside the Kiro Crew package, where this import raises and the
        # crew_home() read below is the only resolution available. A module-scope
        # import would make the module itself unimportable there.
        from kiro_crew.config.loader import read_local_secret
    except Exception:
        # Import unavailable -> standalone mode: the crew_home() read is the ONLY
        # resolution path here.
        try:
            return (store.crew_home() / ".local_secret").read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    # Import available -> the shared resolver is authoritative, INCLUDING its
    # fail-closed refusal. When it returns "" because the dialled family is
    # uncovered (or run/ is unreadable), that is a REFUSAL, not "not found": we
    # must NOT fall through to the home-wide ``.local_secret``, which would send a
    # different listener's credential and reintroduce the exact desync this closes.
    # The crew_home() fallback above is reachable only when the import itself fails.
    return read_local_secret(port, dial_host="127.0.0.1")


def _unconfigured_dispatch(task: str, timeout: int = DEFAULT_TASK_TIMEOUT) -> dict:
    """Fallback when no pool dispatch was injected. The app backend always wires
    a real dispatch (``review_pool.make_sync_dispatch``); this only fires for a
    misconfigured/standalone call, and fails loudly rather than silently spawning.
    """
    return {
        "ok": False,
        "output": "",
        "error": "review pool dispatch not configured (no worker pool wired into run_review)",
    }


def _remote_payload(
    review: dict,
    comments: list[dict],
    *,
    host,
    owner: str,
    repo: str,
    number,
    pinned: tuple[str, str] | None,
) -> dict | None:
    """Project GitHub's review echo into the canonical outbound-payload shape."""
    projected = []
    lines_at = None
    for comment in comments:
        path = str(comment.get("path") or "")
        line = _resolved_line(comment)
        side = comment.get("side") or "RIGHT"
        if line is None:
            if lines_at is None:
                positions = _diff_positions(
                    host, owner, repo, number, str(review.get("commit_id") or ""), pinned
                )
                if positions is None:
                    return None
                lines_at = {
                    p: {pos: ln for ln, pos in mapping.items()} for p, mapping in positions.items()
                }
            position = comment.get("position")
            if not isinstance(position, int) or isinstance(position, bool):
                return None
            line = lines_at.get(path, {}).get(position)
            if line is None or side != "RIGHT":
                return None
        projected.append(
            {"path": path, "line": line, "side": side, "body": comment.get("body") or ""}
        )
    return {
        "body": review.get("body") or "",
        "commit_id": review.get("commit_id") or "",
        "comments": projected,
    }


def _authenticated_login(*, host: str) -> str | None:
    """The `gh` login this host posts as, or None when it cannot be determined."""
    try:
        return discovery.current_login(host=host)
    except (discovery.GhError, discovery.GhSetupError, OSError):
        return None


def _probe_delivery(link: str, intent: dict, payload: dict) -> tuple[DeliveryProbe, str, str]:
    """Reconcile an operation against GitHub without mutating remote state."""
    operation_id = intent.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_ID_RE.fullmatch(operation_id):
        return DeliveryProbe.CONFLICT, "", "delivery intent has invalid operation ID"
    predecessor = intent.get("predecessor") if isinstance(intent.get("predecessor"), dict) else None
    predecessor_operation_id = (predecessor or {}).get("operation_id")
    if predecessor is not None and (
        not isinstance(predecessor_operation_id, str)
        or not _OPERATION_ID_RE.fullmatch(predecessor_operation_id)
    ):
        return DeliveryProbe.CONFLICT, "", "delivery predecessor has invalid operation ID"
    try:
        host, owner, repo, number = adapters.github_pr_ref(link)
    except adapters.AdapterError as exc:
        return DeliveryProbe.CONFLICT, "", f"delivery target is no longer valid: {exc}"
    if _canonical_target(link) != str(intent.get("target") or ""):
        return DeliveryProbe.CONFLICT, "", "delivery target differs from the prepared intent"
    expected_revision = str(intent.get("revision") or "")
    if not expected_revision or expected_revision != str(payload.get("commit_id") or ""):
        return DeliveryProbe.CONFLICT, "", "delivery revision differs from the prepared intent"
    try:
        reviews = discovery.run_gh_json(
            f"repos/{owner}/{repo}/pulls/{number}/reviews", jq=".[]", paginate=True, host=host
        )
    except (discovery.GhError, discovery.GhSetupError, OSError) as exc:
        return DeliveryProbe.UNKNOWN, "", f"could not read GitHub delivery state: {exc}"
    marker = _operation_marker(operation_id)
    predecessor_id = str((predecessor or {}).get("review_id") or "")
    predecessor_marker = _operation_marker(str(predecessor_operation_id)) if predecessor else ""
    predecessor_digest = str((predecessor or {}).get("payload_digest") or "")
    matches = [review for review in reviews if marker in str(review.get("body") or "")]
    if len(matches) > 1:
        return DeliveryProbe.CONFLICT, "", "multiple reviews match the operation marker"
    found_id = ""
    predecessor_seen = False
    predecessor_submitted = False
    for review in sorted(reviews, key=lambda r: marker not in str(r.get("body") or "")):
        review_id = str(review.get("id") or "")
        body = str(review.get("body") or "")
        pinned = None
        if marker in body or (review_id == predecessor_id and predecessor_marker in body):
            try:
                pinned = _pull_revisions(host, owner, repo, number)
            except (discovery.GhError, discovery.GhSetupError, OSError):
                pass
        if marker in body:
            try:
                comments = discovery.run_gh_json(
                    f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/comments",
                    jq=".[]",
                    paginate=True,
                    host=host,
                )
            except (discovery.GhError, discovery.GhSetupError, OSError) as exc:
                return DeliveryProbe.UNKNOWN, "", f"could not read posted review {review_id}: {exc}"
            remote = _remote_payload(
                review, comments, host=host, owner=owner, repo=repo, number=number, pinned=pinned
            )
            if remote is None:
                return DeliveryProbe.UNKNOWN, "", "could not resolve posted review coordinates"
            if _payload_digest(remote) != str(intent.get("payload_digest") or ""):
                return (
                    DeliveryProbe.CONFLICT,
                    "",
                    "operation marker matched a review with different payload",
                )
            # Matching body and payload prove the CONTENT is ours, not that we
            # posted it. The marker is readable by anyone who can read this PR,
            # and the record carrying the intent is worker-writable, so content
            # alone would let a review posted by another account be adopted as
            # this operation's own delivery. Checked here rather than at the
            # marker, so a genuine payload mismatch still reports as the
            # conflict it is.
            author = str(((review.get("user") or {}).get("login") or ""))
            login = _authenticated_login(host=host)
            if login is None:
                return (
                    DeliveryProbe.UNKNOWN,
                    "",
                    "could not confirm the posting account for a matched review",
                )
            if author.casefold() != login.casefold():
                return (
                    DeliveryProbe.CONFLICT,
                    "",
                    "operation marker matched a review posted by another account",
                )
            found_id = review_id
            continue
        if found_id and str(review.get("state") or "") != "PENDING":
            continue
        if review_id == predecessor_id:
            if predecessor_marker not in body:
                return DeliveryProbe.CONFLICT, "", "known predecessor no longer has its marker"
            try:
                comments = discovery.run_gh_json(
                    f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/comments",
                    jq=".[]",
                    paginate=True,
                    host=host,
                )
            except (discovery.GhError, discovery.GhSetupError, OSError) as exc:
                return (
                    DeliveryProbe.UNKNOWN,
                    "",
                    f"could not read predecessor review {review_id}: {exc}",
                )
            remote = _remote_payload(
                review, comments, host=host, owner=owner, repo=repo, number=number, pinned=pinned
            )
            if remote is None:
                return DeliveryProbe.UNKNOWN, "", "could not resolve predecessor review coordinates"
            if _payload_digest(remote) != predecessor_digest:
                return (
                    DeliveryProbe.CONFLICT,
                    "",
                    "known predecessor no longer matches its recorded payload",
                )
            if str(review.get("state") or "") != "PENDING":
                login = _authenticated_login(host=host)
                if login is None:
                    return DeliveryProbe.UNKNOWN, "", "could not confirm the predecessor account"
                author = str((review.get("user") or {}).get("login") or "")
                if author.casefold() != login.casefold():
                    return DeliveryProbe.CONFLICT, "", "predecessor was posted by another account"
                if str(review.get("state") or "") not in {
                    "COMMENTED",
                    "APPROVED",
                    "CHANGES_REQUESTED",
                    "DISMISSED",
                }:
                    return DeliveryProbe.UNKNOWN, "", "could not confirm the predecessor state"
                predecessor_submitted = True
                continue
            # The verified content rides to the poster, which re-reads the
            # review and compares it to this immediately before any DELETE: a
            # human can edit the pending draft between this probe and that call.
            if predecessor is not None:
                predecessor["payload"] = {
                    "body": remote.get("body") or "",
                    "comments": [
                        {
                            "path": c.get("path"),
                            "line": c.get("line"),
                            "side": c.get("side"),
                            "position": c.get("position"),
                            "body": c.get("body") or "",
                        }
                        for c in comments
                    ],
                }
            predecessor_seen = True
            continue
        # A pending review that is not our recorded predecessor is not the
        # probe's business to block on: the driver has no receipt for it, so it
        # must never tell the poster to delete it. The POST itself fails with
        # 422 when such a draft is in the way, and the poster's step 2 tells it
        # to stop and report that failure instead of deleting anything.
    if found_id:
        if predecessor_seen:
            return DeliveryProbe.CONFLICT, "", "multiple pending drafts block confirmation"
        return DeliveryProbe.FOUND, found_id, ""
    if predecessor_submitted:
        return DeliveryProbe.PREDECESSOR_SUBMITTED, predecessor_id, ""
    if predecessor and not predecessor_seen:
        intent.pop("predecessor", None)
    return DeliveryProbe.ABSENT, "", ""


def post_recorded(
    change_id: str,
    link: str,
    *,
    dispatch,
    root: Path | None = None,
    run_id: str | None = None,
    timeout: float = DEFAULT_TASK_TIMEOUT,
    keys: list[str] | None = None,
    confirm=None,
) -> dict:
    """Publish a recorded review through a durable, reconcilable delivery intent."""
    cur = results.read_result(change_id, root, run_id)
    if not cur:
        return {
            "post_ok": True,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": 0,
            "post_error": "no recorded review for this change",
        }
    all_entries = pipeline.build_pending_comments(cur)
    previous = cur.get("delivery_intent")
    recovering = isinstance(previous, dict) and previous.get("state") in {
        "prepared",
        "attempting",
        "indeterminate",
    }
    pending = cur.get("pending_comments", []) if isinstance(previous, dict) else []
    if not isinstance(pending, list) or any(not isinstance(entry, dict) for entry in pending):
        error = (
            "delivery intent saved comments must be a list of objects; restore the saved selection"
        )
        _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(all_entries),
            "expected_units": 0,
            "posted_keys": [],
        }
    recorded_entries = {str(entry.get("key")): entry for entry in pending}
    already = (
        {
            str(entry.get("key"))
            for entry in all_entries
            if entry == recorded_entries.get(str(entry.get("key")))
            and str(entry.get("key")) in (cur.get("posted_keys") or [])
        }
        if isinstance(previous, dict)
        else set(cur.get("posted_keys") or [])
    )
    wanted = set(keys) if keys is not None else {str(entry.get("key")) for entry in all_entries}
    new = [
        entry
        for entry in all_entries
        if str(entry.get("key")) in wanted and str(entry.get("key")) not in already
    ]
    try:
        target = _canonical_target(link)
    except adapters.AdapterError as exc:
        if "://" in str(link or ""):
            # A URL-shaped link that is not a revalidatable GitHub PR link must
            # abort fail-closed: posting through it could address a host this
            # deployment has never vetted.
            error = f"refusing to post: {exc}"
            cur["post_ok"] = False
            cur["post_error"] = error
            results.write_result(cur, root, run_id)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": 0,
                "posted_keys": sorted(already),
            }
        # Not URL-shaped at all: a bare change id (tests, or a record that
        # predates the link field). run_review entries are PR links in
        # production, and the poster prompt builder still fails closed on any
        # host it cannot revalidate, so carrying the opaque target through
        # changes nothing for real links.
        target = str(link or "")

    reconciled_absent = False
    submitted_predecessor = False
    if isinstance(previous, dict) and previous.get("state") in {
        "prepared",
        "attempting",
        "indeterminate",
    }:
        wire, error = _intent_payload(cur, previous)
        if wire is None:
            _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(previous.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
        probe, review_id, error = _probe_delivery(link, previous, wire)
        if probe is DeliveryProbe.FOUND:
            cur["posted_keys"] = sorted(
                already | {str(key) for key in previous.get("selected_keys") or []}
            )
            cur["posted_comments"] = int(previous.get("selected_units") or 0)
            cur.pop("post_partial", None)
            # The VISIBLE payload decides, as in the fresh-dispatch path below:
            # `wire` always carries the operation marker, so `wire.get("body")`
            # would claim a design comment even for a comments-only delivery.
            cur["design_comment_posted"] = bool(cur["github_review_payload"].get("body"))
            cur["posted_review_id"] = review_id
            _write_intent(cur, previous, "confirmed", root=root, run_id=run_id, review_id=review_id)
            requested_pending = sorted(
                str(entry["key"]) for entry in new if str(entry["key"]) not in cur["posted_keys"]
            )
            return {
                "post_ok": not requested_pending,
                "post_error": (
                    "Earlier delivery reconciled; requested selection is still pending. "
                    "Post the remaining selection again."
                    if requested_pending
                    else ""
                ),
                "posted_comments": cur["posted_comments"],
                "design_comment_posted": cur["design_comment_posted"],
                "pending": len(pending),
                "expected_units": cur["posted_comments"],
                "posted_keys": cur["posted_keys"],
                "posted_review_id": review_id,
            }
        if probe not in {DeliveryProbe.ABSENT, DeliveryProbe.PREDECESSOR_SUBMITTED}:
            _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(previous.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
        _write_intent(cur, previous, "prepared", root=root, run_id=run_id)
        intent = previous
        submitted_predecessor = probe is DeliveryProbe.PREDECESSOR_SUBMITTED
        reconciled_absent = True
    else:
        if not new:
            return {
                "post_ok": True,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": 0,
                "posted_keys": sorted(already),
                "post_error": "nothing left to post" if all_entries else "",
            }

        predecessor = None
        if isinstance(previous, dict) and previous.get("state") == "confirmed":
            previous_operation_id = previous.get("operation_id")
            if not isinstance(previous_operation_id, str) or not _OPERATION_ID_RE.fullmatch(
                previous_operation_id
            ):
                error = "delivery intent has invalid operation ID"
                _write_intent(cur, previous, "indeterminate", root=root, run_id=run_id, error=error)
                return {
                    "post_ok": False,
                    "post_error": error,
                    "posted_comments": 0,
                    "design_comment_posted": False,
                    "pending": len(new),
                    "expected_units": int(previous.get("selected_units") or 0),
                    "posted_keys": sorted(already),
                }
            predecessor_id = str(cur.get("posted_review_id") or previous.get("review_id") or "")
            if predecessor_id:
                predecessor = {
                    "review_id": predecessor_id,
                    "operation_id": previous_operation_id,
                    "payload_digest": str(previous.get("payload_digest") or ""),
                }
        replaces = (
            {str(key) for key in previous.get("selected_keys") or []}
            if isinstance(previous, dict) and predecessor
            else {str(key) for key in cur.get("posted_keys") or []} - already
        )
        if not isinstance(previous, dict):
            replaces = already
        selected = {str(entry.get("key")) for entry in new} | replaces
        pending = [entry for entry in all_entries if str(entry.get("key")) in selected]
        cur["pending_comments"] = pending
        try:
            cur["github_review_payload"] = pipeline.build_github_review_payload(cur)
        except ValueError as exc:
            cur["post_ok"] = False
            cur["post_error"] = str(exc)
            results.write_result(cur, root, run_id)
            return {
                "post_ok": False,
                "post_error": str(exc),
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": 0,
                "posted_keys": sorted(already),
            }
        operation_id = uuid4().hex
        wire = _outbound_payload(cur["github_review_payload"], operation_id)
        intent = {
            "operation_id": operation_id,
            "target": target,
            "revision": str(wire.get("commit_id") or ""),
            "payload_digest": _payload_digest(wire),
            "selected_keys": sorted(str(entry.get("key")) for entry in pending),
            "selected_units": pipeline.review_payload_units(cur["github_review_payload"]),
            "state": "prepared",
        }
        if predecessor:
            intent["predecessor"] = predecessor
        cur["posted_comments"] = 0
        cur["design_comment_posted"] = False
        cur["posted_review_id"] = ""
        _write_intent(cur, intent, "prepared", root=root, run_id=run_id)

    wire, error = _intent_payload(cur, intent)
    if wire is None:
        _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(pending),
            "expected_units": int(intent.get("selected_units") or 0),
            "posted_keys": sorted(already),
        }
    if confirm is None and not reconciled_absent:
        probe, _review_id, error = _probe_delivery(link, intent, wire)
        submitted_predecessor = probe is DeliveryProbe.PREDECESSOR_SUBMITTED
        if probe not in {DeliveryProbe.ABSENT, DeliveryProbe.PREDECESSOR_SUBMITTED}:
            message = error or "delivery intent is already present on GitHub"
            _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=message)
            return {
                "post_ok": False,
                "post_error": message,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(intent.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
    if submitted_predecessor:
        try:
            # Rebuild from the saved selection, never a retry's new selection.
            # Its digest must still prove the entry-to-payload correspondence.
            rebuilt = pipeline.build_github_review_payload(cur)
            if _payload_digest(rebuilt) != intent["payload_digest"]:
                raise ValueError("saved selection no longer matches the prepared delivery")
        except ValueError as exc:
            error = f"could not reconcile submitted predecessor: {exc}"
            _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
            return {
                "post_ok": False,
                "post_error": error,
                "posted_comments": 0,
                "design_comment_posted": False,
                "pending": len(pending),
                "expected_units": int(intent.get("selected_units") or 0),
                "posted_keys": sorted(already),
            }
        intent.pop("predecessor", None)
        _write_intent(cur, intent, "prepared", root=root, run_id=run_id)
    cur["posted_comments"] = 0
    cur.pop("post_partial", None)
    _write_intent(cur, intent, "prepared", root=root, run_id=run_id)
    if run_id and not results.publish_to_shared(change_id, root, run_id):
        error = "could not stage the prepared delivery intent for the poster"
        _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(pending),
            "expected_units": int(intent.get("selected_units") or 0),
            "posted_keys": sorted(already),
        }
    try:
        post_prompt = build_post_task(
            link, intent["operation_id"], intent.get("predecessor"), recovering=recovering
        )
    except (adapters.AdapterError, ValueError) as exc:
        error = f"refusing to post: {exc}"
        _write_intent(cur, intent, "indeterminate", root=root, run_id=run_id, error=error)
        return {
            "post_ok": False,
            "post_error": error,
            "posted_comments": 0,
            "design_comment_posted": False,
            "pending": len(pending),
            "expected_units": int(intent.get("selected_units") or 0),
            "posted_keys": sorted(already),
        }

    # The poster owns the shared record, so its write-back cannot alter the
    # intent or wire payload that proves this dispatch reached GitHub.
    dispatched_intent = copy.deepcopy(intent)
    dispatched_wire = copy.deepcopy(wire)
    _write_intent(cur, intent, "attempting", root=root, run_id=run_id)
    spawn = dispatch(post_prompt, timeout)
    staged = results.read_result(change_id, root, None) or {}
    reported = staged.get("posted_comments") if staged.get("change_id") == change_id else None
    reported_count = reported if type(reported) is int and reported >= 0 else 0
    if run_id:
        results.clear_staged([change_id], root)
    after = cur
    if confirm is not None:
        confirmed_id = str(confirm(link, dispatched_wire) or "")
        probe = DeliveryProbe.FOUND if confirmed_id else DeliveryProbe.UNKNOWN
        error = (
            "" if confirmed_id else "the posted draft could not be confirmed on the pull request"
        )
    else:
        probe, confirmed_id, error = _probe_delivery(link, dispatched_intent, dispatched_wire)
    if probe is DeliveryProbe.FOUND:
        delivered_keys = sorted(already | {str(key) for key in intent.get("selected_keys") or []})
        after["posted_keys"] = delivered_keys
        after["posted_comments"] = int(intent.get("selected_units") or 0)
        after["design_comment_posted"] = bool(cur["github_review_payload"].get("body"))
        after["posted_review_id"] = confirmed_id
        _write_intent(
            after, dispatched_intent, "confirmed", root=root, run_id=run_id, review_id=confirmed_id
        )
        requested_pending = sorted(
            str(entry["key"]) for entry in new if str(entry["key"]) not in delivered_keys
        )
        return {
            "post_ok": not requested_pending,
            "post_error": (
                "Earlier delivery reconciled; requested selection is still pending. "
                "Post the remaining selection again."
                if requested_pending
                else ""
            ),
            "posted_comments": after["posted_comments"],
            "design_comment_posted": after["design_comment_posted"],
            "pending": len(pending),
            "expected_units": after["posted_comments"],
            "posted_keys": delivered_keys,
            "posted_review_id": confirmed_id,
        }
    error = (
        str(spawn.get("error") or "")
        or error
        or "the posted draft could not be confirmed on the pull request"
    )
    after["posted_comments"] = reported_count
    if spawn.get("ok") and reported_count:
        after["post_partial"] = True
    after["design_comment_posted"] = False
    after["posted_review_id"] = ""
    _write_intent(after, dispatched_intent, "indeterminate", root=root, run_id=run_id, error=error)
    return {
        "post_ok": False,
        "post_error": error,
        "posted_comments": reported_count,
        "design_comment_posted": False,
        "pending": len(pending),
        "expected_units": int(intent.get("selected_units") or 0),
        "posted_keys": sorted(already),
        "posted_review_id": "",
    }


def _resolved_line(comment: dict) -> int | None:
    """The line GitHub has resolved for a review comment, or None while the
    review is PENDING and only the diff position exists."""
    for key in ("line", "original_line"):
        value = comment.get(key)
        if value is not None:
            return int(value)
    return None


def _pull_revisions(host, owner: str, repo: str, number: str | int) -> tuple[str, str]:
    """The pull request's current (head sha, base sha); "" for either one
    GitHub did not report."""
    pulls = discovery.run_gh_json(f"repos/{owner}/{repo}/pulls/{number}", host=host)
    pull = pulls[0] if pulls else {}
    return (
        str((pull.get("head") or {}).get("sha") or ""),
        str((pull.get("base") or {}).get("sha") or ""),
    )


def _diff_positions(
    host,
    owner: str,
    repo: str,
    number: str | int,
    commit: str,
    pinned: tuple[str, str] | None,
) -> dict[str, dict[int, int]] | None:
    """Map each changed file to {new-file line: diff position} for the pull
    request at `commit`, or None when that diff cannot be read.

    A review comment's `position` counts lines down from the file's first `@@`
    header, through later hunk headers and unchanged lines alike, which is the
    layout of the `patch` field on `GET /pulls/{n}/files`. That endpoint serves
    the diff between the pull request's CURRENT base and CURRENT head, so
    either side moving changes which line a position names. `pinned` is the
    (head, base) the caller read before reading the review's comments; the map
    is refused unless that head is `commit`, both shas were reported, and the
    pull request still reads as the same pair after the files read. A push
    that lands while a draft is being posted, or a base retarget with the head
    unchanged, during or between those reads would otherwise place the
    payload's lines in a diff the draft was never anchored to. A file whose
    patch is withheld (binary, or too large) maps to nothing, so a comment on
    it cannot be confirmed by position.
    """
    if pinned is None or pinned[0] != commit or not pinned[1]:
        return None
    try:
        files = discovery.run_gh_json(
            f"repos/{owner}/{repo}/pulls/{number}/files", jq=".[]", paginate=True, host=host
        )
        if _pull_revisions(host, owner, repo, number) != pinned:
            return None
    except (discovery.GhError, discovery.GhSetupError, OSError):
        return None
    return {
        str(f.get("filename") or ""): _patch_positions(str(f.get("patch") or "")) for f in files
    }


def _patch_positions(patch: str) -> dict[int, int]:
    """{new-file line: diff position} for one file's unified diff."""
    positions: dict[int, int] = {}
    new_line = 0
    for position, text in enumerate(patch.split("\n")):
        if text.startswith("@@"):
            match = re.search(r"\+(\d+)", text)
            new_line = int(match.group(1)) if match else 0
            continue
        if text.startswith("\\"):
            continue  # "\ No newline at end of file"
        if text.startswith("-"):
            continue
        positions[new_line] = position
        new_line += 1
    return positions


def run_review(
    changes: list[str],
    *,
    dispatch=None,
    archiver=_default_archiver,
    concurrency: int = 0,
    timeout: int = DEFAULT_TASK_TIMEOUT,
    generate_report: bool = True,
    root: Path | None = None,
    progress=None,
    run_id: str | None = None,
    cancelled=None,
    post: bool | None = None,
    confirm=None,
    preflight=None,
) -> dict:
    """One single-pass review task per change (bounded concurrency), plus at most
    one coverage follow-up task when the review reports incomplete coverage.
    Each task is dispatched to the reusable worker pool (``dispatch``) and the
    call returns when that task's session finishes its turn. The driver then
    builds the Focus Report. Returns a deterministic summary.

    ``dispatch`` is an injected ``(task, timeout) -> {ok, output, error}`` callable
    (the app backend wires ``review_pool.make_sync_dispatch``; tests inject a fake).
    ``concurrency`` <= 0 means auto: default to the worker pool's concurrency
    cap (``review_pool.MAX_CONCURRENT``).

    ``run_id`` scopes this run's result records and report to
    ``data/runs/<run_id>/`` instead of the shared dirs, which is what makes it safe
    for several runs to be in flight at once. Omitting it keeps the legacy shared
    layout (standalone CLI use).

    ``cancelled`` is an optional zero-arg predicate polled between changes. When it
    turns true, changes that have not started are skipped and reported as
    ``cancelled``. A change already mid-dispatch runs to completion — the worker
    session owns an in-flight model turn that cannot be torn down mid-stream
    without corrupting the pool — so cancellation is prompt but not instant.

    ``post`` overrides the ``review.auto_post`` config for this run: findings are
    published back to the pull request as a PENDING (draft) review only when it is
    enabled. It defaults to OFF, because the review is meant to be READ in the
    app, and writing to a pull request is a side effect the user asks for rather
    than a consequence of running a review.

    ``preflight`` is an optional zero-arg callable returning ``""`` when the
    runtime the dispatched sessions need is available, else a message naming
    what is missing (the app backend wires ``review_pool.runtime_preflight``).
    A non-empty answer fails the run fast — every change is recorded as
    ``runtime_unavailable`` with that message, and nothing is dispatched — so a
    host that cannot spawn a reviewer reports the cause instead of completing
    with nothing written. ``None`` (tests, callers owning their own dispatch)
    skips the check."""
    if run_id:
        store.ensure_run_layout(run_id, root)
    store.ensure_layout(root)
    changes = [c for c in changes if c]
    if not changes:
        return {"ok": False, "error": "no changes to review", "spawned": 0}
    dispatch = dispatch or _unconfigured_dispatch
    progress = progress or (lambda *a, **k: None)  # (change_id, phase, extra) sink
    is_cancelled = cancelled or (lambda: False)

    # Fail-fast runtime preflight. Runs BEFORE the clean-slate resets below, so a
    # host that cannot spawn a reviewer keeps its previous report and staged
    # records intact, and every change carries a reason that names the missing
    # runtime instead of the untriageable "produced no result record".
    runtime_error = str(preflight() or "") if preflight is not None else ""
    if runtime_error:
        failed_records: list[dict] = []
        for link in changes:
            change_id = _cid(link)
            progress(change_id, "failed", {"error": runtime_error, "reason": "runtime_unavailable"})
            failed_records.append(
                {
                    "change": link,
                    "change_id": change_id,
                    "gate_spawn_ok": False,
                    "gate_error": runtime_error,
                    "gate_verdict": "UNKNOWN",
                    "phase2_ran": False,
                    "deep_spawn_ok": False,
                    "deep_error": runtime_error,
                    "deep_reviewed": False,
                    "result_recorded": False,
                    "design_block": False,
                    "deep_rounds": 0,
                    "skipped_reason": "runtime_unavailable",
                }
            )
        return {
            "ok": False,
            "error": runtime_error,
            "changes": len(failed_records),
            "gate_spawns": 0,
            "deep_spawns": 0,
            "design_blocked": 0,
            "phase2_skipped_on_block": 0,
            "cancelled": 0,
            "deep_reviewed": 0,
            "deep_rounds": 0,
            "design_comments_posted": 0,
            "result_records": 0,
            "failures": failed_records,
            "per_change": failed_records,
        }

    # Whether to publish findings back to the pull request. Read ONCE per run so a
    # mid-run config edit cannot post some PRs and not others. Explicit `is True`
    # so a stray string ("false") can never enable writing to a PR.
    if post is None:
        try:
            _review_cfg = store.load_config(root).get("review") or {}
        except Exception:  # pragma: no cover - defensive
            _review_cfg = {}
        auto_post = _review_cfg.get("auto_post") is True
    else:
        auto_post = bool(post)

    # Clean slate for this run: clear the previous run's displayed report and any
    # leftover result records, so a new review never shows confusing prior-run
    # data. The previous report is already archived as an artifact (history kept).
    # For a run-scoped run the dir is fresh anyway; this keeps the legacy path
    # behaving exactly as before.
    report.reset(root, run_id)
    results.clear_results(root, run_id)
    # Also sweep the SHARED staging path for the changes this run will review.
    #
    # The worker writes its record to the shared dir and the driver adopts it into
    # the run dir; a crash between those two steps leaves an orphan that nothing
    # reaps (`_reap_orphan_run_dirs` only walks `data/runs/`). Without this sweep,
    # the next review of that change whose worker completes but records nothing
    # would adopt the residue and report a stale review as a fresh success — and
    # `_record_reviewed` would then durably mark the change reviewed at the NEW
    # head. The legacy whole-run flow got this for free by clearing the whole
    # shared dir at run start; a run-scoped run has to clear its own keys.
    results.clear_staged([_cid(c) for c in changes], root)

    # Mark everything queued upfront so the page renders all rows at once.
    for _link in changes:
        progress(_cid(_link), "queued", {})

    concurrency = _resolve_concurrency(concurrency)
    per_change: list[dict] = []

    def _post_pending(change_id: str, link: str) -> dict:
        return post_recorded(
            change_id,
            link,
            dispatch=dispatch,
            root=root,
            run_id=run_id,
            timeout=timeout,
            confirm=confirm,
        )

    def _one(link: str) -> dict:
        change_id = _cid(link)

        # Cooperative cancellation checkpoint. A change that has not started yet is
        # dropped here rather than paying for a full review the user already
        # abandoned; one already past this point runs to completion (see the
        # docstring — an in-flight worker turn cannot be torn down safely).
        if is_cancelled():
            progress(change_id, "cancelled", {})
            return {
                "change": link,
                "change_id": change_id,
                "gate_spawn_ok": False,
                "gate_error": "",
                "gate_verdict": "CANCELLED",
                "phase2_ran": False,
                "deep_spawn_ok": False,
                "deep_error": "",
                "deep_reviewed": False,
                "result_recorded": False,
                "design_block": False,
                "deep_rounds": 0,
                "cancelled": True,
                "skipped_reason": "cancelled",
            }

        # --- Single thorough review pass (design is ONE dimension, not a gate) ---
        # No separate gate turn and no convergence loop: ONE dispatch does the whole
        # review (design reasoning + all code dimensions) and writes the complete
        # record. Keeping it to review + post (rather than gate + deep + follow-ups +
        # post) minimizes exposure to per-turn timeout / backend-generation failures.
        progress(change_id, "reviewing", {})
        # A single-PR review is ONE long worker turn, so "reviewing" alone leaves
        # the UI with nothing to show for minutes. The pool reports each tool the
        # reviewer invokes; relay it as live activity on this change's phase.
        # Only the real pool dispatch accepts the reporter — test fakes and the
        # standalone CLI pass a plain (task, timeout) callable, so this is opt-in
        # by signature rather than by a TypeError retry that could mask a genuine
        # argument bug inside the dispatcher.

        def report(tool: str, step: int) -> None:
            # Guarded here as well as in the pool: activity is decoration, and a
            # progress writer that raises must never be able to fail a review
            # that is otherwise going fine.
            try:
                progress(change_id, "reviewing", {"activity": {"tool": tool, "step": step}})
            except Exception:
                pass

        # Nothing may be sitting at this change's shared path when its reviewer starts.
        # Adoption proves only that a record NAMES this change, not who wrote it, and every
        # worker can write any change's path in the shared dir -- so a record present
        # beforehand is a leftover or another worker's plant, and adopting it would put
        # someone else's findings on this pull request. If the slot cannot be cleared, skip
        # adoption rather than trust it.
        # Build the prompt BEFORE staking the shared slot: the builder FAILS
        # CLOSED (raises) when the link's host fails revalidation against
        # `allowed_hosts()`, and a fetch instruction with an unconfirmed host
        # would route the worker at public github.com — reviewing (and later
        # posting about) a same-slug public PR instead of the intended one.
        try:
            review_prompt = build_review_task(link)
        except pipeline.adapters.AdapterError as exc:
            refused = f"refusing to review: {exc}"
            progress(change_id, "failed", {"error": refused, "reason": "review_failed"})
            return {
                "change": link,
                "change_id": change_id,
                "gate_spawn_ok": False,
                "gate_error": refused,
                "gate_verdict": "UNKNOWN",
                "phase2_ran": False,
                "deep_spawn_ok": False,
                "deep_error": refused,
                "deep_reviewed": False,
                "result_recorded": False,
                "design_block": False,
                "deep_rounds": 0,
                "skipped_reason": "review_failed",
            }
        slot_clear = results.stake_shared(change_id, root)
        # Keep THIS session (the deep review) resumable: the findings' reasoning
        # is in its context, so it is the only one worth asking about. The
        # gate/follow-up/post sessions are not kept.
        review_kwargs: dict[str, Any] = {}
        if _accepts_activity(dispatch):
            review_kwargs["on_activity"] = report
        if _accepts_kwarg(dispatch, "keep_session_key"):
            review_kwargs["keep_session_key"] = followup.chat_key(run_id or "", change_id)
        review_spawn = dispatch(review_prompt, timeout, **review_kwargs)
        # The worker writes the shared data/results/<id>.json its prompt names;
        # move it into this run's private dir before reading. Without this the
        # run's dir stays empty and a completed review reports no findings.
        if slot_clear:
            results.adopt_from_shared(change_id, root, run_id)
        rev_rec = results.read_result(change_id, root, run_id)
        verdict = str(((rev_rec or {}).get("phase1") or {}).get("gate_verdict", "")).upper()

        # The gate_*/deep_* keys are kept for downstream compatibility — the run
        # summary, _record_reviewed, and the dashboard read them; with the
        # single-pass model they reflect the ONE review dispatch (there is no
        # distinct gate).
        rec: dict = {
            "change": link,
            "change_id": change_id,
            "gate_spawn_ok": review_spawn.get("ok", False),
            "gate_error": review_spawn.get("error", ""),
            "gate_verdict": verdict or "UNKNOWN",
            "phase2_ran": review_spawn.get("ok", False),
            "deep_spawn_ok": review_spawn.get("ok", False),
            "deep_error": review_spawn.get("error", ""),
            "deep_reviewed": bool((rev_rec or {}).get("deep_reviewed")),
            "result_recorded": rev_rec is not None,
            "design_block": (verdict == "BLOCK"),
            "deep_rounds": 1,
        }

        # Fail only when the turn failed OR nothing usable was recorded — never
        # discard a record that DID land, so a trailing abnormal stop cannot drop
        # already-written verdicts/findings.
        if not review_spawn.get("ok", False):
            rec["skipped_reason"] = "review_failed"
            progress(
                change_id,
                "failed",
                {"error": review_spawn.get("error", "review failed"), "reason": "review_failed"},
            )
            return rec
        if not rec["deep_reviewed"]:
            if rev_rec is None:
                # Turn completed but wrote no record at all — the residual case
                # (a genuinely empty review, or a worker whose commands ran no
                # Python). Kept as the ``no_review_recorded`` value existing
                # consumers key on; environment failures are discriminated by
                # the preflight before any dispatch.
                rec["skipped_reason"] = "no_review_recorded"
                progress(
                    change_id,
                    "failed",
                    {"error": "review produced no result record", "reason": "no_review_recorded"},
                )
            else:
                # A record landed but never marked the review complete: the
                # worker got far enough to write, then stopped short. Distinct
                # from "wrote nothing" so the two can be triaged apart.
                rec["skipped_reason"] = "review_record_incomplete"
                progress(
                    change_id,
                    "failed",
                    {
                        "error": "review wrote a result record but never completed the review",
                        "reason": "review_record_incomplete",
                    },
                )
            return rec

        # --- Bounded coverage backstop: AT MOST ONE targeted follow-up, and only
        # when the review self-reported incomplete file coverage — a single,
        # signal-driven pass; a failed follow-up keeps whatever the first pass
        # recorded.
        if (rev_rec or {}).get("coverage_complete") is False:
            progress(change_id, "reviewing", {"coverage": "followup"})
            # The follow-up turn UPDATES the record, so it needs the current one
            # visible at the path its prompt names, and re-adopted afterwards. A
            # failed publish means the record there is not ours, and the follow-up
            # would adopt whatever replaced it -- skip the turn instead.
            published = results.publish_to_shared(change_id, root, run_id)
            # Same fail-closed contract as the first pass: no confirmed host, no
            # follow-up turn. A failed follow-up keeps the first pass's record.
            try:
                followup_prompt: str | None = build_review_followup_task(link)
            except pipeline.adapters.AdapterError:
                followup_prompt = None
            second_pass = (
                dispatch(followup_prompt, timeout)
                if followup_prompt and (published or not run_id)
                else {"ok": False}
            )
            if second_pass.get("ok", False):
                results.adopt_from_shared(change_id, root, run_id)
                rev_rec = results.read_result(change_id, root, run_id) or rev_rec
                rec["deep_rounds"] = 2
                rec["deep_reviewed"] = bool((rev_rec or {}).get("deep_reviewed"))
                # The kept transcript is the FIRST pass's session, and this
                # follow-up just added findings for files that pass never saw.
                # Asking it about one of those would get a confident answer
                # reconstructed from nothing — worse than having no follow-up at
                # all. The follow-up pass's own session is no better (it only
                # covered the remainder), so neither holds the whole record: drop
                # the kept transcript and let the panel offer nothing rather than
                # something wrong.
                _forget_followup(run_id or "", change_id)

        counts = (rev_rec or {}).get("counts") or {}
        red, yellow = counts.get("red", 0), counts.get("yellow", 0)
        if not auto_post:
            # Default path: the review is READ in the app. Nothing is written to
            # the pull request — publishing to someone else's PR is a side effect
            # the user opts into (``review.auto_post``), not a consequence of
            # looking at a review.
            #
            # ``posting_expected`` is 0 rather than red+yellow+1 so the durable
            # dedup index still accepts this change: _record_reviewed requires
            # posted >= expected, which is how it refuses to mark a PR reviewed
            # when a post half-failed. With posting off there is nothing to
            # deliver, so 0 >= 0 correctly means "this PR was reviewed".
            rec["posting_skipped"] = True
            rec["posted_comments"] = 0
            rec["posting_expected"] = 0
            rec["post_ok"] = True
            rec["design_comment_posted"] = False
            progress(
                change_id,
                "done",
                {
                    "counts": {"red": red, "yellow": yellow},
                    "design_block": rec.get("design_block", False),
                    "posted": 0,
                    "expected": 0,
                },
            )
            return rec
        # Opt-in path: the review only RECORDS findings; the driver builds the
        # Python-redacted comment bodies and a separate poster publishes them
        # verbatim — no LLM free-text reaches the CR (security control, unchanged).
        post = _post_pending(change_id, link)
        posted = post["posted_comments"]
        # What the poster was actually asked to deliver, not red+yellow+1: an
        # unanchored finding folds into the review body rather than becoming its
        # own inline comment, so the finding count over-states the payload. This
        # number gates `_record_reviewed` and `_all_delivered`, so over-stating it
        # left a fully-delivered review looking short in both.
        expected = int(post.get("expected_units") or 0)
        # Shared with the explicit-retry path in the backend, so a retry records
        # delivery exactly the way the first attempt would have.
        apply_post_outcome(rec, post)
        progress(
            change_id,
            "done",
            {
                "counts": {"red": red, "yellow": yellow},
                "design_block": rec.get("design_block", False),
                "posted": posted,
                "expected": expected,
            },
        )
        return rec

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        per_change = list(pool.map(_one, changes))

    design_blocked = [r for r in per_change if r.get("design_block")]
    cancelled_changes = [r for r in per_change if r.get("cancelled")]
    failures = [
        r
        for r in per_change
        if not r.get("cancelled") and (not r["gate_spawn_ok"] or r.get("deep_spawn_ok") is False)
    ]
    result_records = sum(1 for r in per_change if r["result_recorded"])
    summary = {
        "ok": True,
        "changes": len(per_change),
        "gate_spawns": len(per_change),  # every change is gated
        "deep_spawns": sum(1 for r in per_change if r["phase2_ran"]),
        "design_blocked": len(design_blocked),  # BLOCK verdicts (still deep-reviewed)
        "phase2_skipped_on_block": 0,  # BLOCK does not skip Phase 2
        "cancelled": len(cancelled_changes),
        "deep_reviewed": sum(1 for r in per_change if r["deep_reviewed"]),
        "deep_rounds": sum(r.get("deep_rounds", 0) for r in per_change),  # total Phase-2 rounds
        "design_comments_posted": sum(1 for r in per_change if r.get("design_comment_posted")),
        "result_records": result_records,
        "failures": failures,
        "per_change": per_change,
    }
    if generate_report and result_records > 0:
        # Runs AFTER all tasks complete (each dispatch call blocks until its
        # worker session ends its turn and the record is on disk), so the report
        # reflects this run's records. Then archive it as a NEW artifact (one
        # report per run) and, only if that archive succeeds, delete the now-
        # redundant result records — their content lives in the archived report
        # summary and as draft CR comments. Guarded on result_records > 0 so a
        # fully-failed run can't clobber the last good report. Never fails the run.
        #
        # The report is written to the run's own dir FIRST and kept there
        # regardless of whether the artifact archive succeeds — the in-app report
        # view reads that file, so a failed archive still leaves a report to read.
        try:
            rep = report.generate(root, run_id=run_id)
            summary["report"] = rep["index"]
            slug = archiver(rep.get("html", ""), root)
            if slug:
                report.set_report_slug(slug, root, run_id)
                summary["report_slug"] = slug
                # Records are only redundant once their content has actually been
                # DELIVERED. With posting deferred to an explicit action they are
                # the only source of the redacted comment payload, so clearing
                # them here would silently make "post comments" impossible.
                #
                # `auto_post` alone is NOT delivery evidence — it is the intent to
                # post. A run whose posts half-failed (network error, permission
                # loss, a partial batch) still reaches here, and deleting the
                # records then leaves the explicit posting retry with nothing to
                # send while the PR carries only some of the findings. Require the
                # same condition the durable dedup index enforces: every change
                # reported post_ok AND delivered at least what it expected.
                if auto_post and _all_delivered(per_change):
                    summary["results_cleaned"] = results.clear_results(root, run_id)
                elif auto_post:
                    summary["results_kept_undelivered"] = True
            else:
                summary["archive_error"] = "report not archived; result records kept"
        except Exception as e:  # pragma: no cover - defensive
            summary["report_error"] = str(e)
    return summary


def apply_post_outcome(rec: dict, post: dict) -> None:
    """Write a post result's delivery evidence onto a per-change record.

    The durable dedup index (``_record_reviewed``) and the run verdict
    (``_all_delivered``) both decide "was this actually delivered?" by reading
    exactly these four fields off the per-change record. Every path that delivers
    comments must therefore write them the SAME way, or the two readers disagree
    with reality: an explicit retry that succeeded but left the record showing the
    original failure keeps the PR out of the index, and the next repo review
    re-reviews and re-posts it.

    ``posting_expected`` comes from the payload units the poster was actually asked
    to deliver (``expected_units``), never from a finding count -- an unanchored
    finding folds into the review body instead of becoming its own inline comment.

    ``posted_keys`` names WHICH findings landed, which is what the UI needs rather
    than a count: it marks individual findings as sent, and it is the evidence the
    publish action requires before releasing the pending review. Omitting it here
    left the auto-post path delivering a draft the app then refused to publish,
    because the run carried no record of which change the draft belonged to.

    ``posted_review_id`` names WHICH DRAFT carries them. A later run replaces the
    draft by deleting and re-creating it, so without the id an earlier run's view
    cannot tell its own draft from that replacement -- and the publish action, which
    compares the two, has nothing to compare against.
    """
    rec["posted_comments"] = int(post.get("posted_comments") or 0)
    rec["posting_expected"] = int(post.get("expected_units") or 0)
    rec["post_ok"] = bool(post.get("post_ok"))
    rec["design_comment_posted"] = bool(post.get("design_comment_posted"))
    rec["posted_keys"] = list(post.get("posted_keys") or [])
    rec["posted_review_id"] = str(post.get("posted_review_id") or "")


def _all_delivered(per_change: list[dict]) -> bool:
    """True when every reviewed change delivered everything it expected to post.

    Mirrors the durable dedup index's own guard (posted >= expected) so the two
    cannot disagree about what "posted" means. A change that was cancelled, or
    that recorded no result, has nothing to deliver and does not block the
    verdict; anything that TRIED to post must have succeeded.
    """
    for rec in per_change:
        if rec.get("cancelled") or not rec.get("result_recorded"):
            continue
        if not rec.get("post_ok"):
            return False
        if int(rec.get("posted_comments") or 0) < int(rec.get("posting_expected") or 0):
            return False
    return True


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage review driver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="Review each change on the reusable worker pool")
    rp.add_argument(
        "--changes", required=True, help="newline/comma-separated GitHub pull request links"
    )
    rp.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="parallel reviews; 0 = auto (worker pool concurrency cap)",
    )
    rp.add_argument("--timeout", type=int, default=DEFAULT_TASK_TIMEOUT)
    rp.add_argument("--no-report", dest="report", action="store_false")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        changes = pipeline.parse_batch(args.changes)
        # Standalone CLI: stand up a private worker pool on a background event
        # loop and bridge the (synchronous) driver to it, mirroring how the app
        # backend wires the shared pool. No /api/spawn, no sub-agents.
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        pool = review_pool.ReviewPool()
        dispatch = review_pool.make_sync_dispatch(loop, pool, default_timeout=args.timeout)
        try:
            out = run_review(
                changes,
                dispatch=dispatch,
                concurrency=args.concurrency,
                timeout=args.timeout,
                generate_report=args.report,
            )
        finally:
            try:
                asyncio.run_coroutine_threadsafe(pool.shutdown(), loop).result(timeout=30)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
