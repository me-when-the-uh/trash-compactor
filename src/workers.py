from contextvars import ContextVar
import logging
import os
from typing import Optional

from .cpu_topology import CpuTopology, baseline_workers, cpu_topology
from .i18n import _

_WORKER_CAP: ContextVar[Optional[int]] = ContextVar("worker_cap", default=None)
_HDD_MODE: ContextVar[bool] = ContextVar("hdd_mode", default=False)
_TOPOLOGY_LOGGED = False


def set_worker_cap(limit: Optional[int]) -> None:
    if limit is not None and limit < 1:
        raise ValueError("worker cap must be >= 1")
    _WORKER_CAP.set(limit)


def set_hdd_mode(active: bool = True) -> None:
    """Engage HDD-friendly settings: single worker everywhere.

    Parallel reads on a spinning drive are random access - every seek costs
    more IOPS than a sequential read saves, so scan, entropy, and compression
    all run single-worker, with the walker visiting directories in discovery
    order so the disk head moves forward instead of jumping.
    """
    _HDD_MODE.set(active)


def hdd_mode() -> bool:
    return _HDD_MODE.get()


def _apply_worker_cap(default: int) -> int:
    limit = _WORKER_CAP.get()
    if limit is None:
        return default
    return max(1, min(default, limit))


def _log_topology_once(topo: CpuTopology, workers: int) -> None:
    global _TOPOLOGY_LOGGED
    if _TOPOLOGY_LOGGED or not topo.hybrid:
        return
    _TOPOLOGY_LOGGED = True
    if topo.lp_e_cores:
        logging.info(
            _("Hybrid CPU: %s P-cores, %s E-cores, %s low-power E-cores left idle. Using %s workers."),
            topo.p_cores,
            topo.e_cores,
            topo.lp_e_cores,
            workers,
        )
    else:
        logging.info(
            _("Hybrid CPU: %s P-cores, %s E-cores. Using %s workers."),
            topo.p_cores,
            topo.e_cores,
            workers,
        )


def _physical_core_baseline_workers() -> int:
    topo = cpu_topology()
    workers = baseline_workers(topo)
    _log_topology_once(topo, workers)
    return workers


def entropy_worker_count() -> int:
    if _HDD_MODE.get():
        return 1
    default = _physical_core_baseline_workers()
    return _apply_worker_cap(default)


def scan_worker_count() -> int:
    if _HDD_MODE.get():
        return 1
    default = _physical_core_baseline_workers()
    env_value = os.getenv("TRASH_COMPACTOR_SCAN_WORKERS")
    if env_value:
        try:
            requested = int(env_value)
            default = max(1, requested)
        except ValueError:
            pass
    return _apply_worker_cap(default)


def xp_worker_count() -> int:
    if _HDD_MODE.get():
        return 1
    default = _physical_core_baseline_workers()
    return _apply_worker_cap(default)


_LZX_CORES_PER_WORKER = 6
_LZX_MAX_WORKERS = 4


def lzx_worker_count() -> int:
    """LZX compresses multi-threaded inside compact.exe, spreading over about
    six cores per process."""
    if _HDD_MODE.get():
        return 1
    p_cores = cpu_topology().p_cores
    workers = (p_cores + _LZX_CORES_PER_WORKER - 1) // _LZX_CORES_PER_WORKER
    return _apply_worker_cap(min(_LZX_MAX_WORKERS, workers))
