"""Persistent on-disk caching for tiles, catalogs and metadata.

Uses ``diskcache`` when available and silently degrades to an in-memory
dictionary otherwise, so a missing optional dependency never breaks a download.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - exercised implicitly by the import
    from diskcache import Cache as _DiskCache

    _HAS_DISKCACHE = True
except ImportError:  # pragma: no cover
    _DiskCache = None
    _HAS_DISKCACHE = False


class _MemoryCache:
    """Thread-safe TTL dictionary used when ``diskcache`` is unavailable."""

    def __init__(self) -> None:
        """Create an empty in-memory store."""
        self._data: dict[str, tuple[float | None, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, default: Any = None) -> Any:
        """Return a live value, or ``default`` when missing or expired."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            expires_at, value = entry
            if expires_at is not None and expires_at < time.time():
                self._data.pop(key, None)
                return default
            return value

    def set(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Store a value with an optional TTL in seconds."""
        with self._lock:
            self._data[key] = (time.time() + expire if expire else None, value)
        return True

    def clear(self) -> int:
        """Drop every entry and return how many were removed."""
        with self._lock:
            count = len(self._data)
            self._data.clear()
        return count

    def close(self) -> None:
        """No-op, present for API parity with ``diskcache.Cache``."""

    def volume(self) -> int:
        """Report zero, since in-memory entries occupy no disk space."""
        return 0


class CacheStore:
    """Namespaced key/value cache with a stable hashing scheme."""

    def __init__(
        self, directory: Path, size_limit: int = 2 * 1024**3, enabled: bool = True
    ) -> None:
        """Open (or create) the cache backing store.

        ``enabled=False`` produces a store whose reads always miss and whose
        writes are discarded -- the implementation of ``--no-cache``.
        """
        self.enabled = enabled
        self._backend: Any
        if not enabled:
            self._backend = _MemoryCache()
        elif _HAS_DISKCACHE:
            directory.mkdir(parents=True, exist_ok=True)
            self._backend = _DiskCache(str(directory), size_limit=size_limit)
        else:
            logger.debug("diskcache not installed; falling back to an in-memory cache")
            self._backend = _MemoryCache()

    @staticmethod
    def make_key(namespace: str, *parts: Any) -> str:
        """Build a collision-resistant cache key from arbitrary parts."""
        raw = "|".join(str(part) for part in parts)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"{namespace}:{digest}"

    def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value for ``key`` if caching is enabled."""
        if not self.enabled:
            return default
        try:
            return self._backend.get(key, default)
        except Exception as exc:  # pragma: no cover - corrupt cache files
            logger.debug("cache read failed for %s: %s", key, exc)
            return default

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Write a value, ignoring any backend failure."""
        if not self.enabled:
            return
        try:
            self._backend.set(key, value, expire=ttl or None)
        except Exception as exc:  # pragma: no cover - disk full, permissions
            logger.debug("cache write failed for %s: %s", key, exc)

    def clear(self) -> int:
        """Remove every entry and return the number of items dropped."""
        try:
            return int(self._backend.clear() or 0)
        except Exception:  # pragma: no cover
            return 0

    @property
    def size_bytes(self) -> int:
        """Approximate on-disk size of the cache."""
        try:
            return int(self._backend.volume())
        except Exception:  # pragma: no cover
            return 0

    def close(self) -> None:
        """Release the backing store."""
        try:
            self._backend.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "CacheStore":
        """Enter a context manager that closes the store on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the store when leaving the context."""
        self.close()
