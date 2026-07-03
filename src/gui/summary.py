from typing import Optional

from ..stats import CompressionStats
from ..timer import PerformanceMonitor


def _timing_fields(
    *,
    scan_seconds: float,
    total_files: int,
    entropy_seconds: float = 0.0,
    entropy_directories: int = 0,
    entropy_files: int = 0,
    total_seconds: Optional[float] = None,
) -> dict:
    scan_seconds = max(0.0, scan_seconds)
    entropy_seconds = max(0.0, entropy_seconds)
    if total_seconds is None:
        total_seconds = scan_seconds + entropy_seconds
    total_seconds = max(0.0, total_seconds)
    scan_rate = (total_files / scan_seconds) if scan_seconds > 0 else 0.0
    return {
        "scan_seconds": scan_seconds,
        "scan_rate": scan_rate,
        "entropy_seconds": entropy_seconds,
        "entropy_rate": (entropy_directories / entropy_seconds) if entropy_seconds > 0 else 0.0,
        "total_seconds": total_seconds,
        "total_files": total_files,
        "entropy_directories": entropy_directories,
        "entropy_files": entropy_files,
    }


def build_live_analysis_timing(
    *,
    scan_seconds: float,
    total_files: int,
    entropy_seconds: float = 0.0,
    entropy_directories: int = 0,
    entropy_files: int = 0,
    total_seconds: Optional[float] = None,
) -> dict:
    return _timing_fields(
        scan_seconds=scan_seconds,
        total_files=total_files,
        entropy_seconds=entropy_seconds,
        entropy_directories=entropy_directories,
        entropy_files=entropy_files,
        total_seconds=total_seconds,
    )


def build_analysis_timing(
    monitor: PerformanceMonitor,
    *,
    total_seconds: Optional[float] = None,
    total_files: Optional[int] = None,
    scan_seconds: Optional[float] = None,
) -> dict:
    files = total_files if total_files is not None else monitor.stats.total_files
    if scan_seconds is None:
        scan_seconds = max(0.0, monitor.stats.file_scan_time)
    entropy_seconds = max(0.0, monitor.stats.entropy_analysis_time)
    if total_seconds is None:
        total_seconds = max(0.0, monitor.stats.total_time)
    if total_seconds <= 0:
        total_seconds = scan_seconds + entropy_seconds
    return _timing_fields(
        scan_seconds=scan_seconds,
        total_files=files,
        entropy_seconds=entropy_seconds,
        entropy_directories=monitor.stats.directories_analyzed_for_entropy,
        entropy_files=monitor.stats.files_analyzed_for_entropy,
        total_seconds=total_seconds,
    )


def accumulate_stats(target: CompressionStats, source: CompressionStats) -> None:
    target.compressed_files += source.compressed_files
    target.skipped_files += source.skipped_files
    target.already_compressed_files += source.already_compressed_files
    target.total_original_size += source.total_original_size
    target.total_compressed_size += source.total_compressed_size
    target.total_skipped_size += source.total_skipped_size
    target.total_skipped_physical_size += source.total_skipped_physical_size
    target.already_compressed_logical_size += source.already_compressed_logical_size
    target.already_compressed_physical_size += source.already_compressed_physical_size
    target.skip_extension_files += source.skip_extension_files
    target.skip_low_savings_files += source.skip_low_savings_files
    target.errors.extend(source.errors)
    target.entropy_projected_original_bytes += source.entropy_projected_original_bytes or source.total_original_size
    target.entropy_projected_size += source.entropy_projected_size or source.total_compressed_size
    target.entropy_projected_size_conservative += (
        source.entropy_projected_size_conservative or source.total_compressed_size
    )
    target.lz4_certain_incompressible_files += source.lz4_certain_incompressible_files


def make_stats_summary(
    stats: CompressionStats,
    plan_count: int,
    total_compressible_size: int,
    *,
    min_savings_percent: float,
    is_analysis: bool = True,
    analysis_timing: Optional[dict] = None,
    lzx_enabled: bool = True,
) -> dict:
    already_compressed_files = max(0, stats.already_compressed_files)
    already_compressed_logical_size = max(0, stats.already_compressed_logical_size)
    already_compressed_physical_size = max(0, stats.already_compressed_physical_size)

    excluded_count = max(0, stats.skipped_files - already_compressed_files)
    excluded_logical_size = max(0, stats.total_skipped_size - already_compressed_logical_size)

    if is_analysis:
        if stats.total_on_disk_size > 0:
            current_size_on_disk = stats.total_on_disk_size
        else:
            current_size_on_disk = (
                already_compressed_physical_size + excluded_logical_size + total_compressible_size
            )

        if lzx_enabled:
            projected_compressed = stats.entropy_projected_size
        else:
            projected_compressed = stats.entropy_projected_size_conservative

        projected_compressible_size = total_compressible_size
        if projected_compressed > 0:
            projected_compressible_size = max(
                0,
                projected_compressed - stats.total_skipped_physical_size,
            )

        if stats.total_on_disk_size > 0:
            logical_savings = max(0, total_compressible_size - projected_compressible_size)
            projected_on_disk_size = max(0, current_size_on_disk - logical_savings)
        else:
            projected_on_disk_size = (
                already_compressed_physical_size + excluded_logical_size + projected_compressible_size
            )
        physical_size = projected_on_disk_size
        potential_savings_bytes = max(0, current_size_on_disk - projected_on_disk_size)
        compressed_count = already_compressed_files
        compressed_logical_size = already_compressed_logical_size
        compressed_physical_size = already_compressed_physical_size
        compressible_count = plan_count
        compressible_logical_size = total_compressible_size
        compressible_physical_size = projected_compressible_size
    else:
        current_size_on_disk = max(0, stats.total_compressed_size)
        projected_on_disk_size = current_size_on_disk
        physical_size = current_size_on_disk
        potential_savings_bytes = max(0, stats.total_original_size - current_size_on_disk)
        compressed_count = already_compressed_files
        compressed_logical_size = already_compressed_logical_size
        compressed_physical_size = already_compressed_physical_size
        compressible_count = max(0, stats.compressed_files)
        compressible_logical_size = max(
            0,
            stats.total_original_size - already_compressed_logical_size - excluded_logical_size,
        )
        compressible_physical_size = max(
            0,
            current_size_on_disk - already_compressed_physical_size - excluded_logical_size,
        )

    return {
        "logical_size": stats.total_original_size,
        "physical_size": physical_size,
        "current_size_on_disk": current_size_on_disk,
        "projected_on_disk_size": projected_on_disk_size,
        "is_analysis": is_analysis,
        "min_savings_percent": min_savings_percent,
        "potential_savings_bytes": potential_savings_bytes,
        "analysis_timing": analysis_timing,
        "lz4_certain_incompressible_files": stats.lz4_certain_incompressible_files,
        "compressed": {
            "count": compressed_count,
            "logical_size": compressed_logical_size,
            "physical_size": compressed_physical_size,
        },
        "compressible": {
            "count": compressible_count,
            "logical_size": compressible_logical_size,
            "physical_size": compressible_physical_size,
        },
        "skipped": {
            "count": excluded_count,
            "logical_size": excluded_logical_size,
            "physical_size": excluded_logical_size,
        },
    }