"""Quoted remote bootstrap for a Coder user's systemd bus."""

from __future__ import annotations

import pytest

from kiro_crew.coder.user_bus import user_bus_command


def test_command_resolves_the_remote_uid_before_exec() -> None:
    command = user_bus_command(["systemctl", "--user", "is-system-running"])

    assert command == (
        'user_id="$(id -u)" && export XDG_RUNTIME_DIR="/run/user/$user_id" && '
        'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus" && exec '
        "systemctl --user is-system-running"
    )


def test_arguments_are_quoted_rather_than_interpolated() -> None:
    command = user_bus_command(["systemctl", "--user", "status", "a b; touch /tmp/x"])

    assert command.endswith("systemctl --user status 'a b; touch /tmp/x'")


def test_an_empty_argv_is_refused() -> None:
    with pytest.raises(ValueError, match="requires an argv"):
        user_bus_command([])
