from contextvars import ContextVar
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


def entropy_worker_count() -> int:
    physical, _ = get_cpu_info()
    if not physical or physical <= 1:
        return _apply_worker_cap(1)
    default = max(1, physical - 1)
    return _apply_worker_cap(default)


def xp_worker_count() -> int:
    _, logical = get_cpu_info()
    threads = logical
    if not threads:
        return _apply_worker_cap(1)

    default = max(1, threads - 1)
    return _apply_worker_cap(default)


def lzx_worker_count() -> int:
    physical, logical = get_cpu_info()
    cores = physical or logical
    if not cores or cores <= 4:
        return _apply_worker_cap(1)

    return _apply_worker_cap(2)