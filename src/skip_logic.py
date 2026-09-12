import logging
import os
from pathlib import Path
from typing import Optional

from .i18n import _
from .config import savings_from_entropy
from .compression.entropy import sample_directory_entropy
from .compression.cache import IncompressibleCache
from .exclusions import app_data_dir
from .file_utils import DirectoryDecision, should_skip_directory
from .stats import CompressionStats, DirectorySkipRecord, EntropySampleRecord


_cache: Optional[IncompressibleCache] = None

def get_incompressible_cache() -> IncompressibleCache:
    global _cache
    if _cache is None:
        configured = os.getenv("TRASH_COMPACTOR_CACHE_PATH")
        cache_path = Path(configured) if configured else app_data_dir() / "incompressible.db"
        _cache = IncompressibleCache(cache_path)
    return _cache


def commit_incompressible_cache() -> None:
    cache = get_incompressible_cache()
    cache.commit()


def discard_staged_incompressible_cache() -> None:
    cache = get_incompressible_cache()
    cache.discard_staged()

def _relative_to_base(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def entropy_records_from_probe(
    directory: Path,
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
    average_entropy: float,
    sampled_files: int,
    sampled_bytes: int,
    lz4_certain_files: int,
    *,
    has_lz4_certain: bool = False,
    sampled_paths: Optional[list[str]] = None,
    lz4_certain_paths: Optional[list[str]] = None,
) -> tuple[Optional[DirectorySkipRecord], Optional[EntropySampleRecord]]:
    if directory == base_dir:
        return None, None

    if average_entropy < 0 or sampled_files == 0 or sampled_bytes < 1024:
        return None, None

    estimated_savings = savings_from_entropy(average_entropy)

    logging.debug(
        "Entropy sample for %s: %.2f bits/byte (~%.1f%% savings) across %s files (%s bytes)",
        directory,
        average_entropy,
        estimated_savings,
        sampled_files,
        sampled_bytes,
    )

    # Callers that probe without path collection (fast_walk) still need the
    # directory-level LZ4 verdict: use a sentinel when every sampled file was
    # certainly incompressible.
    if has_lz4_certain and not lz4_certain_paths:
        lz4_certain_paths = ["*"]

    sample_record = EntropySampleRecord(
        path=str(directory),
        relative_path=_relative_to_base(directory, base_dir),
        average_entropy=average_entropy,
        estimated_savings=estimated_savings,
        sampled_files=sampled_files,
        sampled_bytes=sampled_bytes,
        lz4_certain_files=lz4_certain_files,
        total_bytes=0,
        sampled_paths=sampled_paths or [],
        lz4_certain_paths=lz4_certain_paths or [],
    )

    if estimated_savings >= min_savings_percent:
        return None, sample_record

    if verbosity >= 1:
        logging.info(
            _("Skipping directory %s; estimated savings %.1f%% is below threshold %.1f%%"),
            directory,
            estimated_savings,
            min_savings_percent,
        )

    reason = _("High entropy (est. {savings:.1f}% savings)").format(savings=estimated_savings)
    skip_record = DirectorySkipRecord(
        path=str(directory),
        relative_path=_relative_to_base(directory, base_dir),
        reason=reason,
        category='high_entropy',
        average_entropy=average_entropy,
        estimated_savings=estimated_savings,
        sampled_files=sampled_files,
        sampled_bytes=sampled_bytes,
    )
    return skip_record, sample_record


def evaluate_entropy_directory(
    directory: Path,
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
) -> tuple[Optional[DirectorySkipRecord], Optional[EntropySampleRecord]]:
    average_entropy, sampled_files, sampled_bytes, lz4_certain_files, sampled_paths, lz4_certain_paths = sample_directory_entropy(directory)
    if average_entropy is None:
        return None, None

    return entropy_records_from_probe(
        directory,
        base_dir,
        min_savings_percent,
        verbosity,
        average_entropy,
        sampled_files,
        sampled_bytes,
        lz4_certain_files,
        sampled_paths=sampled_paths,
        lz4_certain_paths=lz4_certain_paths,
    )


def maybe_skip_directory(
    directory: str | Path,
    base_dir: Path,
    stats: CompressionStats,
) -> DirectoryDecision:
    dir_path = directory if isinstance(directory, Path) else Path(directory)
    decision = should_skip_directory(directory)
    if decision.skip:
        reason = decision.reason or _("Excluded system directory")
        category = decision.category or "system"
        record = DirectorySkipRecord(
            path=str(dir_path),
            relative_path=_relative_to_base(dir_path, base_dir),
            reason=reason,
            category=category,
        )
        append_directory_skip_record(stats, record)
        return DirectoryDecision.deny(reason, category=category)

    # Entropy decisions and the cache live in the planner, after the scan.
    return DirectoryDecision.allow_path()


def append_directory_skip_record(stats: CompressionStats, record: DirectorySkipRecord) -> None:
    stats.directory_skips.append(record)
    prefix = {
        'system': 'system directory',
        'user': 'user-excluded directory',
        'high_entropy': 'high entropy directory',
        'directstorage': 'DirectStorage directory',
    }.get(record.category, 'directory')
    logging.debug("Skipping %s %s: %s", prefix, record.path, record.reason)


def log_directory_skips(stats: CompressionStats, verbosity: int, min_savings_percent: float) -> None:
    buckets = {}
    for record in stats.directory_skips:
        buckets.setdefault(record.category, []).append(record)

    if not buckets:
        return

    if 'directstorage' in buckets:
        ds_records = buckets['directstorage']
        logging.info(_("Skipped %s DirectStorage game directories:"), len(ds_records))
        for record in ds_records:
            logging.info(" - %s", record.format_line())

    if verbosity < 1:
        return

    if 'high_entropy' in buckets:
        entropy_records = buckets['high_entropy']
        logging.info(
            _("Skipped %s directories due to low expected savings (<%.1f%%):"),
            len(entropy_records),
            min_savings_percent,
        )
        for record in entropy_records:
            logging.info(
                " - %s - %s (~%.1f%% savings, entropy %.2f, %s files)",
                record.relative_path,
                record.reason,
                record.estimated_savings if record.estimated_savings is not None else 0.0,
                record.average_entropy if record.average_entropy is not None else 0.0,
                record.sampled_files,
            )

    if 'user' in buckets:
        user_records = buckets['user']
        logging.info(_("Skipped %s user-excluded directories:"), len(user_records))
        for record in user_records:
            logging.info(" - %s - %s", record.relative_path, record.reason)

    if verbosity >= 3 and 'system' in buckets:
        system_records = buckets['system']
        logging.info(_("Skipped %s protected directories:"), len(system_records))
        for record in system_records:
            logging.info(" - %s - %s", record.relative_path, record.reason)