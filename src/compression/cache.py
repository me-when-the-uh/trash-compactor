import os
import xxhash
from pathlib import Path
from typing import Set


class IncompressibleCache:
    def __init__(self, cache_file_path: Path):
        self.cache_file_path = Path(cache_file_path)
        self.cache_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Set[str] = set()
        self._staged: Set[str] = set()
        self._hash_cache: dict[Path, str] = {}
        self._load()

    def _load(self):
        if not self.cache_file_path.exists():
            return
        try:
            with open(self.cache_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    hash_val = line.strip()
                    if hash_val:
                        self._cache.add(hash_val)
        except OSError:
            pass

    def _compute_hash(self, path: Path) -> str:
        if path in self._hash_cache:
            return self._hash_cache[path]
        h = xxhash.xxh64()
        normalized = os.path.normcase(os.path.normpath(str(path.absolute())))
        h.update(normalized.encode("utf-8"))
        digest = h.hexdigest()
        self._hash_cache[path] = digest
        return digest

    def clear_hash_cache(self) -> None:
        self._hash_cache.clear()

    def add(self, path: Path):
        hash_val = self._compute_hash(path)
        if hash_val not in self._cache:
            self._cache.add(hash_val)
            self._staged.add(hash_val)

    def commit(self) -> None:
        if not self._staged:
            return

        staged = sorted(self._staged)
        try:
            with open(self.cache_file_path, "a", encoding="utf-8") as f:
                for hash_val in staged:
                    f.write(f"{hash_val}\n")
        except OSError:
            for hash_val in staged:
                self._cache.discard(hash_val)
            self._staged.clear()
            return

        self._staged.clear()

    def discard_staged(self) -> None:
        if not self._staged:
            return

        for hash_val in self._staged:
            self._cache.discard(hash_val)
        self._staged.clear()

    def has_staged(self) -> bool:
        return bool(self._staged)

    def add_and_persist(self, path: Path):
        self.add(path)
        self.commit()

    def contains(self, path: Path) -> bool:
        return self._compute_hash(path) in self._cache