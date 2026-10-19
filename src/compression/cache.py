import os
import xxhash
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class _CacheRecord:
    mtime: Optional[int] = None
    min_savings: Optional[float] = None
    fingerprint: Optional[str] = None


class IncompressibleCache:
    """Cached high-entropy directories.

    Entries bind the path hash to the volume serial. The same path on a
    different drive cannot collide.
    """

    def __init__(self, cache_file_path: Path):
        self.cache_file_path = Path(cache_file_path)
        self.cache_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: Dict[str, _CacheRecord] = {}
        self._staged: Dict[str, _CacheRecord] = {}
        self._hash_cache: Dict[Path, str] = {}
        # One scan per directory per run.
        self._fingerprint_cache: Dict[Path, Optional[str]] = {}
        self._load()

    def _load(self):
        if not self.cache_file_path.exists():
            return
        try:
            with open(self.cache_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    record = _parse_cache_line(line)
                    if record is None:
                        continue
                    hash_val, parsed = record
                    self._entries[hash_val] = parsed
        except OSError:
            pass

    def _volume_serial(self, path: Path) -> str:
        """Return the volume serial of the drive holding ``path`` (or '')."""
        drive = os.path.splitdrive(str(path))[0]
        if not drive:
            return ""
        import ctypes
        from ctypes import wintypes

        try:
            serial = wintypes.DWORD()
            max_component = wintypes.DWORD()
            flags = wintypes.DWORD()
            name_buf = ctypes.create_unicode_buffer(256)
            fs_buf = ctypes.create_unicode_buffer(256)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ok = kernel32.GetVolumeInformationW(
                drive + "\\",
                name_buf,
                len(name_buf),
                ctypes.byref(serial),
                ctypes.byref(max_component),
                ctypes.byref(flags),
                fs_buf,
                len(fs_buf),
            )
            if ok:
                return f"{serial.value:08X}"
        except Exception:
            pass
        return ""

    def _compute_hash(self, path: Path) -> str:
        if path in self._hash_cache:
            return self._hash_cache[path]
        h = xxhash.xxh64()
        normalized = os.path.normcase(os.path.normpath(str(path.absolute())))
        h.update(normalized.encode("utf-8"))
        serial = self._volume_serial(path)
        if serial:
            h.update(serial.encode("utf-8"))
        digest = h.hexdigest()
        self._hash_cache[path] = digest
        return digest

    def _legacy_hash(self, path: Path) -> str:
        """Path-only hash for cache files that carry no volume serial."""
        h = xxhash.xxh64()
        normalized = os.path.normcase(os.path.normpath(str(path.absolute())))
        h.update(normalized.encode("utf-8"))
        return h.hexdigest()

    def _is_stale(self, path: Path, stored: _CacheRecord, min_savings: float) -> bool:
        """True when the cached skip must not be trusted"""
        if stored.min_savings is None or min_savings + 1e-9 < stored.min_savings:
            return True
        try:
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            # The directory is gone.
            return True
        if stored.mtime is not None and mtime > stored.mtime:
            return True
        fingerprint = self._fingerprint(path)
        if fingerprint is None or fingerprint != stored.fingerprint:
            return True
        return False

    def _fingerprint(self, path: Path) -> Optional[str]:
        if path not in self._fingerprint_cache:
            self._fingerprint_cache[path] = _directory_fingerprint(path)
        return self._fingerprint_cache[path]

    def clear_run_cache(self) -> None:
        self._hash_cache.clear()
        self._fingerprint_cache.clear()

    def add(self, path: Path, min_savings: float) -> None:
        hash_val = self._compute_hash(path)
        try:
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            mtime = None
        record = _CacheRecord(
            mtime=mtime,
            min_savings=float(min_savings),
            fingerprint=self._fingerprint(path),
        )
        if self._entries.get(hash_val) != record:
            self._entries[hash_val] = record
            self._staged[hash_val] = record

    def commit(self) -> None:
        if not self._staged:
            return

        merged = dict(self._entries)
        merged.update(self._staged)
        tmp = self.cache_file_path.with_name(self.cache_file_path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for hash_val, record in sorted(merged.items()):
                    f.write(_format_cache_line(hash_val, record))
            os.replace(tmp, self.cache_file_path)
        except OSError:
            tmp.unlink(missing_ok=True)
            for hash_val in self._staged:
                self._entries.pop(hash_val, None)
            self._staged.clear()
            return

        self._staged.clear()

    def discard_staged(self) -> None:
        for hash_val in self._staged:
            self._entries.pop(hash_val, None)
        self._staged.clear()

    def has_staged(self) -> bool:
        return bool(self._staged)

    def contains(self, path: Path, min_savings: float) -> bool:
        stored = self._entries.get(self._compute_hash(path))
        if stored is None:
            # Cache files without a volume serial use the path-only hash.
            stored = self._entries.get(self._legacy_hash(path))
            if stored is None:
                return False
        return not self._is_stale(path, stored, min_savings)


def _parse_cache_line(line: str) -> Optional[tuple[str, _CacheRecord]]:
    parts = line.strip().split()
    if not parts or len(parts[0]) != 16:
        return None
    hash_val = parts[0]
    if any(ch not in "0123456789abcdef" for ch in hash_val.lower()):
        return None
    record = _CacheRecord()
    if len(parts) == 1:
        return hash_val, record
    try:
        record.mtime = int(parts[1])
    except ValueError:
        return None
    if len(parts) == 2:
        return hash_val, record
    try:
        record.min_savings = int(parts[2]) / 10.0
    except ValueError:
        return None
    if len(parts) >= 4:
        record.fingerprint = parts[3]
    return hash_val, record


def _format_cache_line(hash_val: str, record: _CacheRecord) -> str:
    mtime = "" if record.mtime is None else str(record.mtime)
    if record.min_savings is None:
        return f"{hash_val} {mtime}\n" if mtime else f"{hash_val}\n"
    savings = str(int(round(record.min_savings * 10)))
    fingerprint = record.fingerprint or "-"
    return f"{hash_val} {mtime or 0} {savings} {fingerprint}\n"


def _directory_fingerprint(path: Path) -> Optional[str]:
    """Hash of each file's name, size and mtime in this directory only"""
    rows: list[str] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat_result = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rows.append(
                    f"{entry.name}\0{stat_result.st_size}\0{getattr(stat_result, 'st_mtime_ns', int(stat_result.st_mtime))}"
                )
    except OSError:
        return None
    digest = xxhash.xxh64()
    for row in sorted(rows):
        digest.update(row.encode("utf-8", errors="replace"))
    return digest.hexdigest()
