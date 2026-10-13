"""The SSM port forward from this machine to a session task's bootstrap.

One ``aws ssm start-session`` child per task, forwarding a free local loopback
port to the bootstrap's port in the task. It is restarted on the next use if it
died (an idle SSM session closes after about twenty minutes), and stopped with
its whole process group, so the ``session-manager-plugin`` holding the local
port never outlives it.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
from typing import Any, Callable, Optional

from kiro_crew.session_environment import SessionEnvironmentUnavailable

_READY_SECS = 45.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PortForward:
    """Keep a local port forwarded to ``remote_port`` in an SSM target."""

    def __init__(
        self,
        target: str,
        remote_port: int,
        *,
        profile: str = "",
        region: str = "",
        spawn: Optional[Callable[[list[str]], Any]] = None,
    ) -> None:
        self.target = target
        self.remote_port = remote_port
        self.profile = profile
        self.region = region
        self.local_port = 0
        self._spawn = spawn or self._spawn_ssm
        self._proc: Any = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _spawn_ssm(argv: list[str]) -> subprocess.Popen:
        from kiro_crew.deploy.engine import aws_spawn_env

        return subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=aws_spawn_env(argv[0]),
        )

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def ensure(self) -> int:
        """The live local port, starting (or restarting) the forward if needed."""
        async with self._lock:
            if self._alive():
                return self.local_port
            await self._stop_unlocked()
            from kiro_crew.cloud import ssm

            if not ssm.session_manager_plugin_installed():
                raise SessionEnvironmentUnavailable(
                    "the AWS Session Manager plugin is not installed on the gateway machine"
                )
            port = _free_port()
            argv = ssm.build_port_forward_argv(
                self.target, self.remote_port, port, self.profile, self.region
            )
            self._proc = self._spawn(argv)
            self.local_port = port
            proc = self._proc
            ready = await asyncio.to_thread(
                lambda: ssm.wait_for_local_port(port, timeout=_READY_SECS, proc=proc)
            )
            if not ready or not self._alive():
                await self._stop_unlocked()
                raise SessionEnvironmentUnavailable(
                    "could not open the SSM port forward to the session task"
                )
            return port

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        port = await self.ensure()
        return await asyncio.open_connection("127.0.0.1", port, limit=64 * 1024 * 1024)

    async def _stop_unlocked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            from kiro_crew.cloud import ssm

            await asyncio.to_thread(ssm.kill_port_forward, proc)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()
