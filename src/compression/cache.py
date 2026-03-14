import xxhash
from pathlib import Path
from typing import Set


class IncompressibleCache:
    def __init__(self, cache_file_path: Path):
        self.cache_file_path = Path(cache_file_path)
        self.cache_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Set[str] = set()
        self._staged: Set[str] = set()
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
        h = xxhash.xxh64()
        h.update(str(path.absolute()).encode("utf-8"))
        return h.hexdigest()

    def add(self, path: Path):
        hash_val = self._compute_hash(path)
        if hash_val not in self._cache:
            self._cache.add(hash_val)
            self._staged.add(hash_val)

    def commit(self) -> None:
        if not self._staged:
            return

        try:
            with open(self.cache_file_path, "a", encoding="utf-8") as f:
                for hash_val in sorted(self._staged):
                    f.write(f"{hash_val}\n")
        except OSError:
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

