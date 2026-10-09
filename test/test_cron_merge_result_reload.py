"""A run record refused by a busy result merge must survive a store reload.

``_run_job_isolated`` hands a finished run to ``_merge_job_result``. When the
store lock stays contended the merge raises ``CronStoreBusy`` before it writes
anything, so the run's record exists only in memory. When another writer then
changes ``crons.json``, the next ``_sync`` replaces the job list with the disk
copies, which never received the run. The record must be re-applied to that
reload and persisted by the next save under the lock (the timer tick, or
``stop``), or an ``every`` job runs again early and a fired one-shot runs again.

Contention is simulated by making the store lock raise the exception the real
spin raises. The other writer is a second ``CronService`` on the same directory,
run in a worker thread like a separate process. The real ``_on_timer``,
``_run_job_isolated``, ``_merge_job_result``, ``_sync`` and ``_load`` run
unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.cron import CronJob, CronService, CronStoreBusy

_ADMITTED = SimpleNamespace(admitted=True, reason="")
_LONG_AGO = 7200.0


class _BusyStore:
    """Makes the store lock raise CronStoreBusy for the chosen callers.

    ``scope`` is ``"off"``, ``"merge"`` (only the result merge's lock attempt
    loses) or ``"all"`` (every attempt loses, the timer tick's included).
    """

    def __init__(self, service: CronService) -> None:
        self.scope = "off"
        self._merging = False
        real_lock = service._file_lock
        real_merge = service._merge_job_result

        def lock(*args, **kwargs):
            if self.scope == "all" or (self.scope == "merge" and self._merging):
                raise CronStoreBusy("simulated: another writer held the store lock")
            return real_lock(*args, **kwargs)

        def merge(terminal: CronJob) -> None:
            self._merging = True
            try:
                real_merge(terminal)
            finally:
                self._merging = False

        service._file_lock = lock  # type: ignore[method-assign]
        service._merge_job_result = merge  # type: ignore[method-assign]


async def _service(tmp_path, runs: list[str], on_run=None) -> CronService:
    """A started service whose timer only fires when the test ticks it."""

    async def on_job(job: CronJob) -> None:
        runs.append(job.id)
        if on_run is not None:
            await on_run()

    service = CronService(base_dir=tmp_path, on_job=on_job)
    await service.start()
    service._arm_timer = lambda: None  # type: ignore[method-assign]
    task, service._timer_task = service._timer_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
    return service


async def _tick(service: CronService, job_id: str) -> None:
    """One timer tick, then wait for the run it dispatched, if any."""
    await service._on_timer()
    claim = service._claims.get(job_id)
    if claim is not None and claim.task is not None:
        await asyncio.wait_for(claim.task, timeout=5.0)


def _other_writer(tmp_path) -> None:
    """Another process changes the store: it adds an unrelated job."""
    CronService(base_dir=tmp_path).add_job("unrelated", "msg", every_secs=86400)


async def _every_job(service: CronService) -> CronJob:
    """An interval job whose previous run was long ago, so it is due now."""
    job = await service.add_job_async("hourly", "msg", every_secs=3600, strict_schedule=True)

    def backdate() -> None:
        with service._file_lock():
            service._sync()
            for j in service._jobs:
                if j.id == job.id:
                    j.last_run_ts = time.time() - _LONG_AGO
            service._save()

    await asyncio.to_thread(backdate)
    return job


async def _one_shot(service: CronService) -> CronJob:
    """A one-shot that keeps its row after it runs (no delete_after_run)."""
    return await service.add_job_async("remind-once", "msg", at_ts=time.time() - 1)


def _stored(tmp_path, job_id: str) -> CronJob:
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


def _assert_run_recorded(tmp_path, job: CronJob) -> None:
    stored = _stored(tmp_path, job.id)
    assert stored.last_status == "ok"
    if job.schedule.kind == "every":
        assert stored.last_run_ts > time.time() - 60, "the run's last_run_ts never reached disk"
    else:
        assert stored.enabled is False, "the fired one-shot is enabled on disk"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_busy_merge_then_reload_runs_the_job_once(tmp_path, make_job):
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)  # runs; its merge cannot lock the store
            store.scope = "off"
            await asyncio.to_thread(_other_writer, tmp_path)
            await _tick(service, job.id)  # reloads the changed store
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"
    _assert_run_recorded(tmp_path, job)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_reload_by_a_reader_keeps_the_record_while_the_tick_is_busy(tmp_path, make_job):
    # The reload comes from a read path, and the tick cannot lock the store, so
    # nothing is saved: the reloaded job list itself must carry the record.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            store.scope = "off"
            await asyncio.to_thread(_other_writer, tmp_path)
            await service.list_jobs_async(include_disabled=True)  # reloads
            store.scope = "all"
            await _tick(service, job.id)  # in-memory snapshot only
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_reload_during_the_run_keeps_the_record(tmp_path, make_job):
    # The store changes while the job runs, so its finalizer holds a job object
    # the reload already replaced. The record has to reach the replacement.
    runs: list[str] = []
    holder: dict[str, CronService] = {}

    async def reload_mid_run() -> None:
        await asyncio.to_thread(_other_writer, tmp_path)
        await holder["service"].list_jobs_async(include_disabled=True)

    service = await _service(tmp_path, runs, on_run=reload_mid_run)
    holder["service"] = service
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            store.scope = "off"
            await _tick(service, job.id)
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id], f"{job.schedule.kind} job ran {len(runs)} times"
    _assert_run_recorded(tmp_path, job)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_job", [_every_job, _one_shot], ids=["every", "at"])
async def test_stop_persists_a_record_its_merge_could_not_save(tmp_path, make_job):
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await make_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()  # a gateway restart

    assert runs == [job.id]
    restarted = CronService(base_dir=tmp_path)
    stored = restarted.get_job(job.id)
    assert stored is not None
    # The due-scan in _on_timer fires a job that is enabled and due.
    assert not (
        stored.enabled and restarted._is_due(stored, time.time())
    ), "the restarted service would run the job again"
    _assert_run_recorded(tmp_path, job)


@pytest.mark.asyncio
async def test_a_newer_stored_run_wins_over_a_held_record(tmp_path):
    # Same fence as the merge: a held record never overwrites a newer run that
    # another process already stored.
    runs: list[str] = []
    service = await _service(tmp_path, runs)
    job = await _every_job(service)
    store = _BusyStore(service)
    try:
        with patch("kiro_crew.cron.admission_check", return_value=_ADMITTED):
            store.scope = "merge"
            await _tick(service, job.id)
            store.scope = "off"
            generation = service._runs.generations[job.id]

            def store_newer_run() -> None:
                other = CronService(base_dir=tmp_path)
                with other._file_lock():
                    other._sync()
                    newer = next(j for j in other._jobs if j.id == job.id)
                    newer.run_generation = generation + 1
                    newer.last_status = "error"
                    newer.last_error = "newer run"
                    newer.last_run_ts = time.time()
                    other._save()

            await asyncio.to_thread(store_newer_run)
            await _tick(service, job.id)
            await _tick(service, job.id)
    finally:
        store.scope = "off"
        await service.stop()

    assert runs == [job.id]
    stored = _stored(tmp_path, job.id)
    assert stored.last_error == "newer run"
    assert stored.run_generation == generation + 1
