import os
import xxhash
from pathlib import Path
from typing import Dict, Optional


class IncompressibleCache:
    """On-disk cache of high-entropy directories.

    Entries bind the path hash to the volume serial so the same path on a
    different drive never collides.  Each entry records the directory's mtime
    at the time it was cached; a newer mtime invalidates the entry.
    """

    def __init__(self, cache_file_path: Path):
        self.cache_file_path = Path(cache_file_path)
        self.cache_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: Dict[str, Optional[int]] = {}
        self._staged: Dict[str, Optional[int]] = {}
        self._hash_cache: Dict[Path, str] = {}
        self._load()

    def _load(self):
        if not self.cache_file_path.exists():
            return
        try:
            with open(self.cache_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if not parts:
                        continue
                    hash_val = parts[0]
                    mtime = int(parts[1]) if len(parts) > 1 else None
                    self._entries[hash_val] = mtime
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
        """Path-only hash used by cache files written before volume serials."""
        h = xxhash.xxh64()
        normalized = os.path.normcase(os.path.normpath(str(path.absolute())))
        h.update(normalized.encode("utf-8"))
        return h.hexdigest()

    def _is_stale(self, path: Path, stored: Optional[int]) -> bool:
        """A cached entry is stale when the directory changed after it was cached."""
        try:
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            return False
        return stored is not None and mtime > stored

    def clear_hash_cache(self) -> None:
        self._hash_cache.clear()

    def add(self, path: Path) -> None:
        hash_val = self._compute_hash(path)
        try:
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            mtime = None
        if self._entries.get(hash_val) != mtime:
            # New entry, or a stale one being re-confirmed: refresh both the
            # in-memory value and the staged write
            self._entries[hash_val] = mtime
            self._staged[hash_val] = mtime

    def commit(self) -> None:
        if not self._staged:
            return

        try:
            with open(self.cache_file_path, "a", encoding="utf-8") as f:
                for hash_val in sorted(self._staged):
                    mtime = self._staged[hash_val]
                    if mtime is not None:
                        f.write(f"{hash_val} {mtime}\n")
                    else:
                        f.write(f"{hash_val}\n")
        except OSError:
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

    def contains(self, path: Path) -> bool:
        stored = self._entries.get(self._compute_hash(path))
        if stored is None:
            # Legacy path-only entries from older cache files.
            stored = self._entries.get(self._legacy_hash(path))
            if stored is None:
                return False
        return not self._is_stale(path, stored)
