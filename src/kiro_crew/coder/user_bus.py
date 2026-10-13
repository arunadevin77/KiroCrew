"""Remote shell bootstrap for a Coder user's systemd bus."""

from __future__ import annotations

import shlex
from collections.abc import Sequence

_USER_BUS_BOOTSTRAP = (
    'user_id="$(id -u)" && export XDG_RUNTIME_DIR="/run/user/$user_id" && '
    'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus" && exec '
)


def user_bus_command(argv: Sequence[str]) -> str:
    """Return a safely quoted command connected to the remote user's bus.

    Noninteractive Coder SSH sessions do not consistently inherit the PAM
    variables that systemd's user tools need. Resolve them from the remote UID
    so templates remain independent of distro-specific user numbering.
    """
    if not argv:
        raise ValueError("a user-bus command requires an argv")
    return _USER_BUS_BOOTSTRAP + shlex.join(argv)
