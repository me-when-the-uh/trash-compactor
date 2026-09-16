"""Windows CPU topology: P-cores, E-cores, and the extra low-power island."""

from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional, Sequence

import psutil

ERROR_INSUFFICIENT_BUFFER = 122
RelationProcessorCore = 0
CpuSetInformation = 0

# SYSTEM_CPU_SET_INFORMATION.CpuSet field offsets from the start of the record.
_CPUSET_TYPE = 4
_CPUSET_GROUP = 12
_CPUSET_CORE = 15
_CPUSET_LLC = 16
_CPUSET_EFF = 18
_CPUSET_MIN = 19


@dataclass(frozen=True)
class CpuCore:
    group: int
    core_index: int
    llc_index: int
    efficiency_class: int


@dataclass(frozen=True)
class CpuTopology:
    physical: int
    logical: int
    p_cores: int
    e_cores: int
    lp_e_cores: int

    @property
    def hybrid(self) -> bool:
        return self.e_cores > 0 or self.lp_e_cores > 0


_cached: Optional[CpuTopology] = None


def set_cpu_topology(topology: Optional[CpuTopology]) -> None:
    """Test hook. Pass None to drop the cache and probe again."""
    global _cached
    _cached = topology


def cpu_topology() -> CpuTopology:
    global _cached
    if _cached is None:
        _cached = probe_cpu_topology()
    return _cached


def probe_cpu_topology() -> CpuTopology:
    try:
        cores, logical = _query_cpu_sets()
        if cores:
            return topology_from_cores(cores, logical)
    except (OSError, ValueError, AttributeError) as exc:
        logging.debug("CPU set topology probe failed: %s", exc)
    try:
        counts, logical = _query_processor_cores()
        if counts:
            return topology_from_efficiency_counts(counts, logical)
    except (OSError, ValueError, AttributeError) as exc:
        logging.debug("Processor-core topology probe failed: %s", exc)
    return _psutil_topology()


def topology_from_cores(cores: Sequence[CpuCore], logical: Optional[int] = None) -> CpuTopology:
    if not cores:
        return _psutil_topology()
    physical = len(cores)
    logical = logical or physical
    classes = {core.efficiency_class for core in cores}
    if len(classes) <= 1:
        return CpuTopology(physical, logical, physical, 0, 0)

    p_class = max(classes)
    p_list = [core for core in cores if core.efficiency_class == p_class]
    eff_list = [core for core in cores if core.efficiency_class != p_class]
    p_llcs = {(core.group, core.llc_index) for core in p_list}
    island = [core for core in eff_list if (core.group, core.llc_index) not in p_llcs]
    shared = [core for core in eff_list if (core.group, core.llc_index) in p_llcs]
    if island and shared:
        return CpuTopology(physical, logical, len(p_list), len(shared), len(island))
    return CpuTopology(physical, logical, len(p_list), len(eff_list), 0)


def topology_from_efficiency_counts(
    class_counts: dict[int, int],
    logical: Optional[int] = None,
) -> CpuTopology:
    physical = sum(class_counts.values())
    if physical <= 0:
        return _psutil_topology()
    logical = logical or physical
    classes = sorted(class_counts)
    if len(classes) <= 1:
        return CpuTopology(physical, logical, physical, 0, 0)
    p_cores = class_counts[classes[-1]]
    if len(classes) >= 3:
        lp_e = class_counts[classes[0]]
        e_cores = physical - p_cores - lp_e
        return CpuTopology(physical, logical, p_cores, e_cores, lp_e)
    return CpuTopology(physical, logical, p_cores, physical - p_cores, 0)


def baseline_workers(topo: CpuTopology) -> int:
    if topo.physical <= 2:
        return 1
    if not topo.hybrid:
        return max(1, topo.physical - 1)
    p_work = topo.p_cores if topo.p_cores <= 3 else topo.p_cores - 1
    e_work = max(0, topo.e_cores - 1)
    return max(1, p_work + e_work)


def _psutil_topology() -> CpuTopology:
    physical = psutil.cpu_count(logical=False) or 0
    logical = psutil.cpu_count(logical=True) or 0
    n = physical or logical or 1
    return CpuTopology(n, logical or n, n, 0, 0)


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _query_cpu_sets() -> tuple[list[CpuCore], int]:
    k32 = _kernel32()
    fn = k32.GetSystemCpuSetInformation
    fn.argtypes = [
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    fn.restype = wintypes.BOOL
    needed = wintypes.ULONG(0)
    if fn(None, 0, ctypes.byref(needed), None, 0):
        return [], 0
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or needed.value == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = (ctypes.c_ubyte * needed.value)()
    got = wintypes.ULONG(0)
    if not fn(buf, needed.value, ctypes.byref(got), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return _parse_cpu_set_buffer(bytes(buf[: got.value]))


def _parse_cpu_set_buffer(data: bytes) -> tuple[list[CpuCore], int]:
    by_core: dict[tuple[int, int], CpuCore] = {}
    logical = 0
    offset = 0
    length = len(data)
    while offset + 8 <= length:
        size = struct.unpack_from("<I", data, offset)[0]
        if size < 8 or offset + size > length:
            break
        rec = data[offset : offset + size]
        offset += size
        if size < _CPUSET_MIN:
            continue
        if struct.unpack_from("<I", rec, _CPUSET_TYPE)[0] != CpuSetInformation:
            continue
        logical += 1
        group = struct.unpack_from("<H", rec, _CPUSET_GROUP)[0]
        core_index = rec[_CPUSET_CORE]
        llc_index = rec[_CPUSET_LLC]
        efficiency = rec[_CPUSET_EFF]
        key = (group, core_index)
        if key not in by_core:
            by_core[key] = CpuCore(group, core_index, llc_index, efficiency)
    return list(by_core.values()), logical


def _query_processor_cores() -> tuple[dict[int, int], int]:
    k32 = _kernel32()
    fn = k32.GetLogicalProcessorInformationEx
    fn.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.BOOL
    needed = wintypes.DWORD(0)
    if fn(RelationProcessorCore, None, ctypes.byref(needed)):
        return {}, 0
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or needed.value == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = (ctypes.c_ubyte * needed.value)()
    got = wintypes.DWORD(needed.value)
    if not fn(RelationProcessorCore, buf, ctypes.byref(got)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _parse_processor_core_buffer(bytes(buf[: got.value]))


def _parse_processor_core_buffer(data: bytes) -> tuple[dict[int, int], int]:
    counts: dict[int, int] = {}
    logical = 0
    offset = 0
    length = len(data)
    while offset + 8 <= length:
        relationship, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > length:
            break
        rec = data[offset : offset + size]
        offset += size
        if relationship != RelationProcessorCore or size < 10:
            continue
        flags = rec[8]
        efficiency = rec[9]
        counts[efficiency] = counts.get(efficiency, 0) + 1
        # LTP_PC_SMT: two logical processors on this core.
        logical += 2 if flags & 1 else 1
    return counts, logical
