from typing import Optional

from ..stats import CompressionStats
from ..timer import PerformanceMonitor


def _timing_fields(
    *,
    walk_seconds: float,
    total_files: int,
    check_seconds: float = 0.0,
    entropy_seconds: float = 0.0,
    entropy_files: int = 0,
    scan_rate: Optional[float] = None,
) -> dict:
    walk_seconds = max(0.0, walk_seconds)
    check_seconds = max(0.0, check_seconds)
    entropy_seconds = max(0.0, entropy_seconds)
    combined = walk_seconds + check_seconds
    walk_rate = (total_files / walk_seconds) if walk_seconds > 0 else 0.0
    check_rate = (total_files / check_seconds) if check_seconds > 0 and total_files > 0 else 0.0
    if scan_rate is None:
        scan_rate = (total_files / combined) if combined > 0 else 0.0
    return {
        "combined_scan_seconds": combined,
        "walk_seconds": walk_seconds,
        "walk_rate": walk_rate,
        "check_seconds": check_seconds,
        "check_rate": check_rate,
        "scan_rate": scan_rate,
        "entropy_seconds": entropy_seconds,
        "entropy_rate": (entropy_files / entropy_seconds) if entropy_seconds > 0 else 0.0,
    }


def build_live_analysis_timing(
    *,
    walk_seconds: float,
    total_files: int,
    check_seconds: float = 0.0,
    entropy_seconds: float = 0.0,
    entropy_files: int = 0,
) -> dict:
    return _timing_fields(
        walk_seconds=walk_seconds,
        total_files=total_files,
        check_seconds=check_seconds,
        entropy_seconds=entropy_seconds,
        entropy_files=entropy_files,
    )


def build_analysis_timing(
    walk_seconds: float,
    candidate_files: int,
    monitor: PerformanceMonitor,
) -> dict:
    walk_seconds = max(0.0, walk_seconds)
    walk_rate = (candidate_files / walk_seconds) if walk_seconds > 0 else 0.0
    return _timing_fields(
        walk_seconds=walk_seconds,
        total_files=candidate_files,
        check_seconds=max(0.0, monitor.stats.file_scan_time),
        entropy_seconds=max(0.0, monitor.stats.entropy_analysis_time),
        entropy_files=monitor.stats.files_analyzed_for_entropy,
        scan_rate=walk_rate,
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
    target.entropy_projected_compressed_bytes += source.entropy_projected_compressed_bytes or source.total_compressed_size
    target.entropy_projected_compressed_bytes_conservative += (
        source.entropy_projected_compressed_bytes_conservative or source.total_compressed_size
    )


def make_stats_summary(
    stats: CompressionStats,
    plan_count: int,
    total_compressible_size: int,
    *,
    min_savings_percent: float,
    is_analysis: bool = True,
    analysis_timing: Optional[dict] = None,
) -> dict:
    already_compressed_files = max(0, stats.already_compressed_files)
    already_compressed_logical_size = max(0, stats.already_compressed_logical_size)
    already_compressed_physical_size = max(0, stats.already_compressed_physical_size)

    excluded_count = max(0, stats.skipped_files - already_compressed_files)
    excluded_logical_size = max(0, stats.total_skipped_size - already_compressed_logical_size)

    if is_analysis:
        current_on_disk_size = already_compressed_physical_size + excluded_logical_size + total_compressible_size

        projected_compressible_size = total_compressible_size
        if stats.entropy_projected_compressed_bytes_conservative > 0:
            projected_compressible_size = max(
                0,
                stats.entropy_projected_compressed_bytes_conservative - stats.total_skipped_physical_size,
            )

        projected_on_disk_size = already_compressed_physical_size + excluded_logical_size + projected_compressible_size
        physical_size = projected_on_disk_size
        potential_savings_bytes = max(0, current_on_disk_size - projected_on_disk_size)
        compressed_count = already_compressed_files
        compressed_logical_size = already_compressed_logical_size
        compressed_physical_size = already_compressed_physical_size
        compressible_count = plan_count
        compressible_logical_size = total_compressible_size
        compressible_physical_size = projected_compressible_size
    else:
        current_on_disk_size = max(0, stats.total_compressed_size)
        projected_on_disk_size = current_on_disk_size
        physical_size = current_on_disk_size
        potential_savings_bytes = max(0, stats.total_original_size - current_on_disk_size)
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
            current_on_disk_size - already_compressed_physical_size - excluded_logical_size,
        )

    return {
        "logical_size": stats.total_original_size,
        "physical_size": physical_size,
        "current_on_disk_size": current_on_disk_size,
        "projected_on_disk_size": projected_on_disk_size,
        "is_analysis": is_analysis,
        "min_savings_percent": min_savings_percent,
        "potential_savings_bytes": potential_savings_bytes,
        "analysis_timing": analysis_timing,
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