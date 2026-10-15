"""Launch the Rust port of the stub when it is built; else the Python one.

``packages/kirocrew-stub-rs`` builds ``kirocrew-mcp-stub-rs``, a drop-in for
``python -m kiro_crew.mcp_gateway.stub`` that speaks the same Register / bridge /
reconnect / fallback protocol to gatewayd. It uses about a tenth of the Python
stub's memory per session. Selection:

* ``mcp_gateway.stub_impl`` in ``config.json``: ``auto`` (default), ``rust``
  or ``python``.
* ``auto`` takes the Rust stub when its binary exists and is executable and
  silently keeps the Python stub otherwise; ``python`` always keeps it.

Build: ``cargo build --release --locked`` in ``packages/kirocrew-stub-rs``.
The binary is the crate's own release build inside this checkout. A missing or
non-executable binary keeps the Python stub, so the Rust stub can never cost a
session its servers.

The Rust stub hands itself to the Python stub (same flags, env and stdio) for
what it does not port: a stub without a verified session token, and the
direct-exec fallback while the gateway cannot serve.

The config key is read RAW (``_raw_config``) rather than declared on
``McpGatewayConfig``: it is an operator escape hatch, not a Settings control.

Linux only: the port was measured there, and its process-ancestry walk reads
``/proc``. A binary is launched only when the source digest it embeds
(``build.rs``) matches this checkout's crate source, so a build left over from
before a ``git pull`` keeps the Python stub.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Executable name of the Rust stub, and therefore the cmdline substring that
#: identifies one (the Python stub is identified by ``STUB_MODULE``).
RUST_STUB_BIN_NAME = "kirocrew-mcp-stub-rs"

_PACKAGE_DIR = Path(__file__).resolve().parent.parent

_warned: set[str] = set()


def _raw_gateway_section() -> dict:
    try:
        # circular import: config.loader imports the rewriter at module top.
        from kiro_crew.config.loader import _raw_config

        section = _raw_config().get("mcp_gateway") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _crate_dir() -> Path:
    # <checkout>/src/kiro_crew -> <checkout>/packages/kirocrew-stub-rs
    return _PACKAGE_DIR.parent.parent / "packages" / "kirocrew-stub-rs"


def _default_binary() -> Path:
    return _crate_dir() / "target" / "release" / RUST_STUB_BIN_NAME


#: What ``main.rs`` embeds: ``kirocrew-stub-rs-source:<64 hex>;``.
_SOURCE_MARKER = re.compile(rb"kirocrew-stub-rs-source:([0-9a-f]{64});")
_SOURCE_DIGEST_CACHE: dict[str, tuple[tuple, str]] = {}


#: The Python modules the crate mirrors, crate-relative. A change to any of
#: them may leave the port behind, so they are part of the digest. Keep in step
#: with ``build.rs``'s ``MIRRORED_PYTHON``.
_MIRRORED_PYTHON = (
    "../../src/kiro_crew/code_fingerprint.py",
    "../../src/kiro_crew/json_line.py",
    "../../src/kiro_crew/jsonl_util.py",
    "../../src/kiro_crew/mcp_caller.py",
    "../../src/kiro_crew/mcp_cleanup.py",
    "../../src/kiro_crew/mcp_gateway/claim.py",
    "../../src/kiro_crew/mcp_gateway/hashing.py",
    "../../src/kiro_crew/mcp_gateway/read_limits.py",
    "../../src/kiro_crew/mcp_gateway/secret_uri.py",
    "../../src/kiro_crew/mcp_gateway/shutdown_budget.py",
    "../../src/kiro_crew/mcp_gateway/stub.py",
    "../../src/kiro_crew/mcp_gateway/transport.py",
    "../../src/kiro_crew/session_pid_sig.py",
    "../../src/kiro_crew/session_token_sig.py",
)


def _source_files(crate: Path) -> list[tuple[str, Path]]:
    """``(label, path)`` per digested file: crate-relative POSIX labels."""
    files = [crate / "Cargo.toml", crate / "Cargo.lock", crate / "build.rs"]
    files.extend((crate / "src").rglob("*.rs"))
    out = [(p.relative_to(crate).as_posix(), p) for p in files]
    out.extend((m, crate / m) for m in _MIRRORED_PYTHON)
    return out


def _source_digest(crate: Optional[Path] = None) -> str:
    """``build.rs``'s digest of the crate source, or ``""`` when it is missing.

    Same algorithm as ``build.rs``: SHA-256 over each file (the crate's own
    and the mirrored Python modules) in sorted crate-relative POSIX path
    order, as ``path NUL content NUL``. Cached on the files' (path, mtime,
    size), so a rewrite pass reads the source once.
    """
    crate = crate or _crate_dir()
    try:
        files = sorted(_source_files(crate), key=lambda lp: lp[0])
        stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for _, p in files)
    except OSError:
        return ""
    cached = _SOURCE_DIGEST_CACHE.get(str(crate))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    h = hashlib.sha256()
    try:
        for label, p in files:
            h.update(label.encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    except OSError:
        return ""
    digest = h.hexdigest()
    _SOURCE_DIGEST_CACHE[str(crate)] = (stamp, digest)
    return digest


def _binary_source_digest(path: Path) -> str:
    """The source digest a built binary carries, or ``""``. Cached on (mtime, size)."""
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        cached = _SOURCE_DIGEST_CACHE.get(f"bin:{path}")
        if cached is not None and cached[0] == stamp:
            return cached[1]
        m = _SOURCE_MARKER.search(path.read_bytes())
    except OSError:
        return ""
    digest = m.group(1).decode("ascii") if m else ""
    _SOURCE_DIGEST_CACHE[f"bin:{path}"] = (stamp, digest)
    return digest


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        logger.warning(msg)


def rust_stub_binary() -> Optional[str]:
    """The Rust stub to launch, or ``None`` to keep the Python stub.

    ``auto`` (the default) uses the Rust stub whenever its binary is present
    and executable and otherwise keeps the Python stub without a warning: an
    install without the built binary is normal. ``rust`` asks for it
    explicitly, so a missing binary is logged. ``python`` never uses it.

    Either way the binary must have been built from the crate source in this
    checkout (the digest ``build.rs`` embeds): a build left over from before a
    ``git pull`` keeps the Python stub and says to rebuild. Linux only: the
    port was measured there, and its process-ancestry walk reads ``/proc``.
    """
    if not sys.platform.startswith("linux"):
        return None
    section = _raw_gateway_section()
    impl = str(section.get("stub_impl") or "auto").strip().lower()
    if impl not in ("rust", "auto"):
        return None
    path = _default_binary()
    if not (path.is_file() and os.access(path, os.X_OK)):
        if impl == "rust":
            _warn_once(
                f"mcp_gateway.stub_impl={impl} but {path} is not an executable file; "
                "keeping the Python stub"
            )
        return None
    built_from = _binary_source_digest(path)
    if not built_from or built_from != _source_digest():
        _warn_once(
            f"{path} was not built from this checkout's packages/kirocrew-stub-rs source; "
            "keeping the Python stub. Rebuild it with `cargo build --release --locked`."
        )
        return None
    return str(path)


def install_context_b64() -> str:
    """What the Python stub reads from its own interpreter, for the Rust one.

    ``hashing.install_aliases`` and ``code_fingerprint`` derive from
    ``sys.executable`` and the package location of the process that runs them.
    The Rust stub has neither, so the rewriter hands it this process's values:
    the same ones a Python stub launched from this overlay would compute.

    ``python_argv`` is the interpreter prefix the rewriter would launch a Python
    stub with. The Rust stub execs into the Python one through it for what the
    port leaves to Python: a stub without a verified session token, and the
    direct-exec fallback while the gateway cannot serve.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            dist = version("kiro-crew")
        except PackageNotFoundError:
            dist = "unknown"
    except Exception:  # pragma: no cover - importlib.metadata is stdlib
        dist = "unknown"
    from kiro_crew import platform_compat
    from kiro_crew.code_fingerprint import _PACKAGE_ROOT
    from kiro_crew.mcp_gateway.hashing import _package_dir

    ctx = {
        "interpreter": sys.executable,
        # The two spellings the Python stub uses: ``hashing`` takes abspath,
        # ``code_fingerprint`` takes resolve(); they differ under a symlink.
        "package_dir": _package_dir(),
        "fingerprint_root": str(_PACKAGE_ROOT),
        "dist_version": dist,
        "python_argv": platform_compat.isolated_python_argv(),
    }
    raw = json.dumps(ctx, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")
