"""The user-typed ``/compact`` deferred wait is held under the chat turn gate.

On a shared chat runtime a ``/compact`` whose terminal arrives late runs its
``wait_for_compaction`` receipt AFTER the ``stream_command`` turn has already
released the per-process chat turn gate. In that window a co-tenant chat can
start a turn and receive this session's OWNERLESS compaction terminal (the
status names no issuer and is fanned ``runtime_global`` to every co-tenant
runner), clearing the co-tenant's already-streamed answer before it is
persisted. ``CompactionCoordinator`` closes this with a continuous
``hold_chat_turn_gate`` around its deferred sequence; the user-typed path in
``chat_runner._run_chat`` resolves the same hold via
``_resolve_chat_turn_gate_hold`` and wraps its deferred ``wait_for_compaction``.

The behavioural test drives the real resolver + a real ``AcpSessionProvider``
per-process gate and proves a CO-TENANT cannot acquire that gate (i.e. cannot
start a turn) while the deferred wait is in flight. A source-contract test keeps
the wrap at the call site so a future edit cannot silently drop it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re

import pytest

from kiro_crew.dashboard import chat_runner

# ---- behavioural: a co-tenant is blocked for the duration of the wait --------


def _gate_backed_provider():
    """An AcpSessionProvider whose per-process gate is a real asyncio lock.

    Both co-tenants on one shared runtime acquire the SAME ``chat_turn_gate``;
    the provider's own ``_chat_turn_gate`` / ``hold_chat_turn_gate`` serialise
    on it. We model that single per-process gate directly so the test exercises
    the real contention, not a mock.
    """
    from unittest.mock import MagicMock

    from kiro_crew.acp.session_provider import AcpSessionProvider

    gate_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def chat_turn_gate():
        async with gate_lock:
            yield

    runtime = MagicMock()
    runtime.chat_turn_gate = chat_turn_gate

    handle = MagicMock()
    provider = AcpSessionProvider(handle, runtime, shared_runtime=True)
    return provider, gate_lock


@pytest.mark.asyncio
async def test_a_co_tenant_cannot_start_a_turn_during_the_deferred_wait() -> None:
    provider, gate_lock = _gate_backed_provider()

    wait_entered = asyncio.Event()
    release_wait = asyncio.Event()

    async def blocking_wait_for_compaction(timeout: float = 0.0):
        # Stand in for the real receipt drain: enter, then park until released,
        # modelling the window the ownerless terminal is in flight.
        wait_entered.set()
        await release_wait.wait()
        return {"type": "completed", "summary": ""}

    provider.wait_for_compaction = blocking_wait_for_compaction  # type: ignore[assignment]

    async def deferred_compact():
        # Exactly the call-site shape in _run_chat: resolve the hold, then run
        # the receipt drain inside it.
        async with chat_runner._resolve_chat_turn_gate_hold(provider):
            return await provider.wait_for_compaction(timeout=0.0)

    compact_task = asyncio.create_task(deferred_compact())
    await asyncio.wait_for(
        wait_entered.wait(), timeout=2.0
    )  # flake-ok: awaits the Event the deferred-compact task sets on entry, bounded only to name a hang

    # The /compact deferred wait now holds the per-process gate. A co-tenant
    # that tries to start a turn must take the SAME gate — it must NOT proceed.
    cotenant_started = asyncio.Event()

    async def cotenant_turn():
        async with provider._chat_turn_gate():
            cotenant_started.set()

    cotenant_task = asyncio.create_task(cotenant_turn())

    # Give the co-tenant every chance to run WITHOUT waiting on the clock: drain
    # the ready queue by yielding repeatedly. A task genuinely blocked on the
    # held gate never becomes runnable, so no amount of yielding advances it --
    # whereas a wall-clock timeout would make this test's result depend on timing.
    for _ in range(50):
        await asyncio.sleep(0)
    assert (
        not cotenant_started.is_set()
    ), "a co-tenant started a turn while the /compact deferred wait held the gate"
    assert not cotenant_task.done()

    # Releasing the wait frees the gate; the co-tenant then proceeds.
    release_wait.set()
    result = await asyncio.wait_for(
        compact_task, timeout=2.0
    )  # flake-ok: awaits the compact task that release_wait.set() just unblocked, bounded only to name a hang
    assert result["type"] == "completed"
    await asyncio.wait_for(
        cotenant_task, timeout=2.0
    )  # flake-ok: awaits the co-tenant task that proceeds once the gate frees, bounded only to name a hang
    assert cotenant_started.is_set(), "the co-tenant never ran after the wait released"


@pytest.mark.asyncio
async def test_a_provider_without_the_hook_is_a_null_context() -> None:
    # A placeholder client before kiro startup (or a non-ACP provider) has no
    # gate hook: the resolver yields a null context rather than raising into a
    # compaction. A co-tenant is not gated in that (non-shared) case, which is
    # correct — there is no shared runtime to protect.
    class _NoHook:
        pass

    async with chat_runner._resolve_chat_turn_gate_hold(_NoHook()):
        pass  # does not raise


# ---- source-contract: the wrap stays at the call site -----------------------


def _deferred_wait_segment() -> str:
    src = inspect.getsource(chat_runner._run_chat)
    marker = "Deferred compaction: waiting for compaction result"
    start = src.find(marker)
    assert start != -1, "deferred-compaction arm not found in _run_chat"
    return src[start:]


def test_deferred_compaction_wait_is_held_under_the_turn_gate() -> None:
    flat = " ".join(_deferred_wait_segment().split())
    held = re.search(
        r"async with _resolve_chat_turn_gate_hold\(client\): "
        r"compaction_result = await client\.wait_for_compaction\(",
        flat,
    )
    assert held, (
        "the deferred /compact wait_for_compaction must be awaited inside "
        "'async with _resolve_chat_turn_gate_hold(client):' so the turn gate is "
        "held across the receipt"
    )


def test_the_gate_is_entered_before_stream_command_for_compact() -> None:
    """The hold must be taken BEFORE stream_command, not only at the deferred wait.

    stream_command's own turn releases the per-process gate when it ends, so
    acquiring the gate only at the deferred wait leaves a release-reacquire gap:
    on a shared runtime a co-tenant turn can start in that gap and receive this
    session's ownerless compaction terminal, dropping its streamed reply. The
    call site enters the hold onto an AsyncExitStack before stream_command when
    the command is /compact, so the gate is held continuously across the whole
    turn and the inner wait-site hold re-enters it as a no-op.
    """
    flat = " ".join(inspect.getsource(chat_runner._run_chat).split())
    entered = re.search(
        r'if first_word == "/compact": '
        r"await _compact_gate_stack\.enter_async_context\("
        r"_resolve_chat_turn_gate_hold\(client\)\) "
        r"event_stream = client\.stream_command",
        flat,
    )
    assert entered, (
        "a /compact turn must enter _resolve_chat_turn_gate_hold(client) onto "
        "_compact_gate_stack immediately before stream_command, so the gate is "
        "held continuously from the command turn through the deferred wait with "
        "no gap for a co-tenant to slip a turn into"
    )
    # And it is released after the /compact arm, with a defensive close in the finally.
    assert (
        "await _compact_gate_stack.aclose()" in flat
    ), "the /compact gate hold must be closed after the deferred-compaction arm"
