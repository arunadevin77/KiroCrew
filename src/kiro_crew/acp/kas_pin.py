"""The one KAS engine build this Kiro Crew release runs on the bundle route.

Each Kiro Crew version pins exactly one engine version, and the pin is the
sha256 of that version's published ``manifest.json``. The manifest in turn pins
every artifact (engine tarball and Node build, per platform) by sha256 and size,
so this one digest fixes every byte the installer can lay down: a republished or
tampered manifest is refused before any artifact is fetched.

Why a pin rather than ``latest.json``: the engine speaks a wire protocol Kiro
Crew adapts to (``acp/kas_wire.py``, ``acp/harness/kas.py``), and an engine
newer than the adapter was tested against is a silent behavior change on the
operator's next install. Pinning makes the engine version a property of the Kiro
Crew version, the same contract ``packaging/kiro-cli-version`` holds for the
bundled kiro-cli: an update of Kiro Crew is what moves the engine, and a
rollback of Kiro Crew moves it back (each version is its own directory under
``engines/kas/``, so both stay installed).

Bumping: publish with ``scripts/kas_bundle_publish.py``, test against it, then
change BOTH constants below in one commit. The publish script prints the
digest. ``test_kas_pin.py`` refuses a version without a well-formed digest.

``KIROCREW_KAS_VERSION`` overrides the version the harness looks for (a
developer testing another engine installed with ``kirocrew kas install
--version X``). It is read from the gateway's own environment, never from
config, which is agent-readable and not where a code-selection switch belongs.
"""

from __future__ import annotations

import os

__all__ = [
    "KAS_PIN_MANIFEST_SHA256",
    "KAS_PIN_VERSION",
    "KAS_VERSION_OVERRIDE_ENV",
    "pinned_version",
]

#: Engine version this release runs.
KAS_PIN_VERSION = "0.66.33"

#: sha256 of ``<base>/kas/<KAS_PIN_VERSION>/manifest.json`` as published.
KAS_PIN_MANIFEST_SHA256 = "3d830401627799540a56483d01e1fae30b7b610ab4c314ede8a8b7d333252c33"

#: Operator/developer override of the version the harness resolves.
KAS_VERSION_OVERRIDE_ENV = "KIROCREW_KAS_VERSION"


def pinned_version() -> str:
    """The engine version to run: the override when set, else the pin."""
    override = (os.environ.get(KAS_VERSION_OVERRIDE_ENV) or "").strip()
    return override or KAS_PIN_VERSION
