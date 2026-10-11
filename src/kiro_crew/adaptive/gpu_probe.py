"""Optional GPU memory probe for the adaptive controller.

A host that serves a local model next to the gateway can run out of GPU memory
long before it runs out of host memory: every concurrently decoding sub-agent
holds a KV-cache slot on the card. ``HostSample`` already carries host memory,
RSS and file descriptors; this leaf adds the card's free and total memory so
:func:`kiro_crew.adaptive.signals.classify` can treat a nearly full card the way
it treats nearly exhausted host memory: as a spawn gate signal.

Design rules, all pinned by ``test_adaptive_gpu_probe.py``:

* **Optional.** NVML is reached through ``pynvml`` (the ``nvidia-ml-py``
  package, the ``gpu`` extra in ``setup.cfg``). When the import or
  ``nvmlInit`` fails, the reader marks itself unavailable once and says so in
  one log line (INFO without the binding, naming the command that installs it;
  WARNING when NVML does not start), and
  every later read returns :data:`NOT_MEASURED` without touching NVML again, so
  a GPU line stays inert until a restart. A host with no NVIDIA driver pays one
  failed import per process.
* **Never raises.** Every NVML call sits inside ``try``; a failure after a
  successful init (driver reset, device fell off the bus) returns
  :data:`NOT_MEASURED` for that read and is retried on the next one.
* **Cheap.** Results are cached for ``ttl_secs`` (default 2.0 s), so at the
  controller's one-second sampling floor NVML is queried at most every other
  tick. The read is synchronous and runs on a worker thread of its own, which
  the controller's tick starts only while a GPU line is set and waits for at
  most ``GPU_READ_TIMEOUT_SECS``, one read at a time; it is never called on
  the event loop.
* **The most constrained device decides.** On a multi-GPU host the sample
  reports the device with the least free memory (its free and its total). A
  single model lives on one card, so summing free memory across cards would
  overstate the headroom that matters. For the same reason a device that cannot
  be read makes the whole read :data:`NOT_MEASURED`: it may be the fullest card.
* **-1 means not measured**, matching every other ``HostSample`` field, so the
  consumers' ``>= 0`` guards keep today's behaviour byte for byte on a host
  without a GPU.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Default cache lifetime for one NVML read, in seconds.
DEFAULT_TTL_SECS = 2.0

_MIB = 1024.0 * 1024.0


@dataclass(frozen=True)
class GpuSample:
    """One GPU read. ``-1`` = not measured (no driver, no device, or a failure)."""

    vram_free_mb: float = -1.0
    vram_total_mb: float = -1.0

    @property
    def measured(self) -> bool:
        return self.vram_free_mb >= 0.0 and self.vram_total_mb > 0.0


#: The sample every failure path returns.
NOT_MEASURED = GpuSample()


class NvmlReader:
    """TTL-cached NVML reader. One instance per process is enough; see :func:`read_gpu`."""

    def __init__(
        self,
        *,
        ttl_secs: float = DEFAULT_TTL_SECS,
        clock: Callable[[], float] = time.monotonic,
        importer: Callable[[], Any] | None = None,
    ) -> None:
        self._ttl = max(0.0, float(ttl_secs))
        self._clock = clock
        self._importer = importer or _import_pynvml
        self._lock = threading.Lock()
        self._nvml: Any = None
        self._unavailable = False
        self._cached: GpuSample | None = None
        self._cached_at = 0.0

    def read(self) -> GpuSample:
        """Return the cached sample while it is fresh, else read NVML. Never raises."""
        with self._lock:
            now = self._clock()
            if self._cached is not None and (now - self._cached_at) < self._ttl:
                return self._cached
            sample = self._read_uncached()
            self._cached = sample
            self._cached_at = now
            return sample

    def _read_uncached(self) -> GpuSample:
        if self._unavailable:
            return NOT_MEASURED
        if self._nvml is None:
            try:
                nvml = self._importer()
                nvml.nvmlInit()
            except Exception as exc:
                self._unavailable = True
                missing = isinstance(exc, ImportError)
                hint = ""
                if missing:
                    from kiro_crew import extras  # the binding is the optional ``gpu`` extra

                    hint = "; install the binding with: " + extras.install_hint("gpu")
                # Once per reader, so once per process: the latch keeps every later
                # read off NVML, and a GPU line set in config stays inert until a
                # restart. No binding is the default install (INFO, with the command
                # that adds it); a binding whose NVML does not start is a driver
                # problem (WARNING).
                logger.log(
                    logging.INFO if missing else logging.WARNING,
                    "adaptive: GPU memory probe off for this process (%s); "
                    "agent.vram_pressure_gb and agent.vram_critical_gb have no effect "
                    "until a restart%s",
                    type(exc).__name__,
                    hint,
                )
                logger.debug("adaptive: gpu probe unavailable (no NVML)", exc_info=True)
                return NOT_MEASURED
            self._nvml = nvml
        nvml = self._nvml
        try:
            count = int(nvml.nvmlDeviceGetCount())
        except Exception:
            logger.debug("adaptive: gpu probe device count failed", exc_info=True)
            return NOT_MEASURED
        if count <= 0:
            return NOT_MEASURED
        best_free: float | None = None
        best_total = -1.0
        for index in range(count):
            try:
                handle = nvml.nvmlDeviceGetHandleByIndex(index)
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                free_mb = float(mem.free) / _MIB
                total_mb = float(mem.total) / _MIB
            except Exception:
                # The unreadable card may be the one a model fills, so no other card's
                # headroom stands in for it: the read is not measured, as for any failure
                # after init, and the next read looks again.
                logger.debug(
                    "adaptive: gpu probe memory read failed for device %d", index, exc_info=True
                )
                return NOT_MEASURED
            if best_free is None or free_mb < best_free:
                best_free, best_total = free_mb, total_mb
        if best_free is None:
            return NOT_MEASURED
        return GpuSample(vram_free_mb=best_free, vram_total_mb=best_total)


def _import_pynvml() -> Any:
    import pynvml  # type: ignore[import-not-found]  # optional extra `gpu`

    return pynvml


_reader_lock = threading.Lock()
_reader: NvmlReader | None = None


def read_gpu() -> GpuSample:
    """Process-wide cached read used by the adaptive controller's tick. Never raises."""
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = NvmlReader()
        reader = _reader
    try:
        return reader.read()
    except Exception:  # pragma: no cover - the reader already catches; belt and braces
        logger.debug("adaptive: gpu probe failed", exc_info=True)
        return NOT_MEASURED
