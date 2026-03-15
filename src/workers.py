from contextvars import ContextVar
import os
from typing import Optional

from .config import get_cpu_info

_WORKER_CAP: ContextVar[Optional[int]] = ContextVar("worker_cap", default=None)


def set_worker_cap(limit: Optional[int]) -> None:
    if limit is not None and limit < 1:
        raise ValueError("worker cap must be >= 1")
    _WORKER_CAP.set(limit)


def _apply_worker_cap(default: int) -> int:
    limit = _WORKER_CAP.get()
    if limit is None:
        return default
    return max(1, min(default, limit))


def _physical_core_baseline_workers() -> int:
    physical, logical = get_cpu_info()
    cores = physical or logical or 1
    if cores <= 2:
        return 1
    return max(1, cores - 1)


def entropy_worker_count() -> int:
    default = _physical_core_baseline_workers()
    return _apply_worker_cap(default)


def scan_worker_count() -> int:
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
    default = _physical_core_baseline_workers()
    return _apply_worker_cap(default)


def lzx_worker_count() -> int:
    physical, logical = get_cpu_info()
    cores = physical or logical
    if not cores or cores <= 4:
        return _apply_worker_cap(1)

    return _apply_worker_cap(2)