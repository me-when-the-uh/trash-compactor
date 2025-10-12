import logging
from pathlib import Path
from typing import Optional

from .config import savings_from_entropy
from .compression.entropy import sample_directory_entropy
from .file_utils import DirectoryDecision, should_skip_directory
from .stats import CompressionStats, DirectorySkipRecord

_CACHE_TERMINALS = (
    'cache',
    'cache2',
    'cache_data',
    'cachedata',
    'media cache',
    'code cache',
    'gpu cache',
    'cache storage',
    'cache_storage',
    'shadercache',
)

_CACHE_ROOT_MARKERS = (
    'appdata',
    'programdata',
    'locallow',
    'localcache',
    'localappdata',
    'users',
    'temp',
)

_CACHE_HINTS = (
    'chrome',
    'chromium',
    'brave',
    'edge',
    'electron',
    'discord',
    'teams',
    'steam',
    'telegram',
    'whatsapp',
    'slack',
    'vivaldi',
    'opera',
    'githubdesktop',
    'riot',
    'epic',
    'zoom',
    'spotify',
    'firefox',
    'mozilla',
)


def _relative_to_base(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _cache_directory_reason(path: Path) -> Optional[str]:
    parts = path.parts
    parts_cf = [segment.casefold() for segment in parts]
    if len(parts_cf) <= 2:
        return None

    terminal_hint = None
    for candidate in parts_cf[-2:]:
        for keyword in _CACHE_TERMINALS:
            if keyword in candidate:
                terminal_hint = keyword
                break
        if terminal_hint:
            break

    if terminal_hint is None:
        return None

    if not any(marker in parts_cf for marker in _CACHE_ROOT_MARKERS):
        return None

    hint = None
    for original, lowered in zip(parts, parts_cf):
        for token in _CACHE_HINTS:
            if token in lowered:
                hint = original
                break
        if hint:
            break

    descriptor = hint or parts[-1]
    return f"{descriptor} cache directory"


def _evaluate_cache_directory(directory: Path, base_dir: Path, collect_entropy: bool) -> Optional[DirectorySkipRecord]:
    reason = _cache_directory_reason(directory)
    if reason is None:
        return None

    average_entropy = None
    sampled_files = 0
    sampled_bytes = 0
    estimated_savings = None
    if collect_entropy:
        average_entropy, sampled_files, sampled_bytes = sample_directory_entropy(directory)
        if average_entropy is not None:
            estimated_savings = savings_from_entropy(average_entropy)

    return DirectorySkipRecord(
        path=str(directory),
        relative_path=_relative_to_base(directory, base_dir),
        reason=reason,
        category='cache',
        average_entropy=average_entropy,
        estimated_savings=estimated_savings,
        sampled_files=sampled_files,
        sampled_bytes=sampled_bytes,
    )


def evaluate_entropy_directory(
    directory: Path,
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
) -> Optional[DirectorySkipRecord]:
    if directory == base_dir:
        return None

    average_entropy, sampled_files, sampled_bytes = sample_directory_entropy(directory)
    if average_entropy is None or sampled_files == 0 or sampled_bytes < 1024:
        return None

    estimated_savings = savings_from_entropy(average_entropy)

    logging.debug(
        "Entropy sample for %s: %.2f bits/byte (~%.1f%% savings) across %s files (%s bytes)",
        directory,
        average_entropy,
        estimated_savings,
        sampled_files,
        sampled_bytes,
    )

    if estimated_savings >= min_savings_percent:
        return None

    if verbosity >= 2:
        logging.info(
            "Skipping directory %s; estimated savings %.1f%% is below threshold %.1f%%",
            directory,
            estimated_savings,
            min_savings_percent,
        )

    reason = f"High entropy (est. {estimated_savings:.1f}% savings)"
    return DirectorySkipRecord(
        path=str(directory),
        relative_path=_relative_to_base(directory, base_dir),
        reason=reason,
        category='high_entropy',
        average_entropy=average_entropy,
        estimated_savings=estimated_savings,
        sampled_files=sampled_files,
        sampled_bytes=sampled_bytes,
    )


def maybe_skip_directory(
    directory: Path,
    base_dir: Path,
    stats: CompressionStats,
    collect_entropy: bool,
    min_savings_percent: float,
    verbosity: int,
) -> DirectoryDecision:
    decision = should_skip_directory(directory)
    if decision.skip:
        reason = decision.reason or "Excluded system directory"
        record = DirectorySkipRecord(
            path=str(directory),
            relative_path=_relative_to_base(directory, base_dir),
            reason=reason,
            category='system',
        )
        append_directory_skip_record(stats, record)
        return DirectoryDecision.deny(reason)

    cache_record = _evaluate_cache_directory(directory, base_dir, collect_entropy)
    if cache_record:
        append_directory_skip_record(stats, cache_record)
        return DirectoryDecision.deny(cache_record.reason)

    if not collect_entropy:
        return DirectoryDecision.allow_path()

    entropy_record = evaluate_entropy_directory(directory, base_dir, min_savings_percent, verbosity)
    if entropy_record:
        append_directory_skip_record(stats, entropy_record)
        return DirectoryDecision.deny(entropy_record.reason)

    return DirectoryDecision.allow_path()


def append_directory_skip_record(stats: CompressionStats, record: DirectorySkipRecord) -> None:
    stats.directory_skips.append(record)
    if record.category == 'system':
        logging.debug("Skipping system directory %s: %s", record.path, record.reason)
    elif record.category == 'cache':
        logging.debug("Skipping cache directory %s: %s", record.path, record.reason)
    elif record.category == 'high_entropy':
        logging.debug("Skipping high entropy directory %s: %s", record.path, record.reason)
    else:
        logging.debug("Skipping directory %s: %s", record.path, record.reason)


def log_directory_skips(stats: CompressionStats, verbosity: int, min_savings_percent: float) -> None:
    if verbosity < 2:
        return

    buckets = {}
    for record in stats.directory_skips:
        buckets.setdefault(record.category, []).append(record)

    if not buckets:
        return

    if verbosity >= 3 and 'cache' in buckets:
        cache_records = buckets['cache']
        logging.info("Skipped %s cache directories:", len(cache_records))
        for record in cache_records:
            if record.average_entropy is not None and record.estimated_savings is not None:
                logging.info(
                    " - %s - %s (~%.1f%% savings, entropy %.2f, %s files)",
                    record.relative_path,
                    record.reason,
                    record.estimated_savings,
                    record.average_entropy,
                    record.sampled_files,
                )
            else:
                logging.info(" - %s - %s", record.relative_path, record.reason)

    if 'high_entropy' in buckets:
        entropy_records = buckets['high_entropy']
        logging.info(
            "Skipped %s directories due to low expected savings (<%.1f%%):",
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

    if verbosity >= 4 and 'system' in buckets:
        system_records = buckets['system']
        logging.info("Skipped %s protected directories:", len(system_records))
        for record in system_records:
            logging.info(" - %s - %s", record.relative_path, record.reason)