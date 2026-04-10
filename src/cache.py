import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional


class TTLCache:
    def __init__(self, capacity: int = 500) -> None:
        self.capacity = capacity
        self._store: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._start_eviction_thread()

    def _start_eviction_thread(self) -> None:
        thread = threading.Thread(target=self._eviction_loop, daemon=True)
        thread.start()

    def _eviction_loop(self) -> None:
        while True:
            time.sleep(300)
            self._evict_expired()

    def _evict_expired(self) -> None:
        now = time.time()
        with self._lock:
            expired_keys = [key for key, (_, exp) in self._store.items() if exp < now]
            for key in expired_keys:
                self._store.pop(key, None)

    def _evict_one(self) -> None:
        if not self._store:
            return
        oldest_key = min(self._store.items(), key=lambda item: item[1][1])[0]
        self._store.pop(oldest_key, None)

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                self._misses += 1
                return None
            value, expires_at = entry
            if expires_at < now:
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        with self._lock:
            if len(self._store) >= self.capacity:
                self._evict_one()
            self._store[key] = (value, expires_at)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._store),
                "capacity": self.capacity,
                "hits": self._hits,
                "misses": self._misses,
            }


DEFAULT_CACHE = TTLCache()


def make_cache_key(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    params = params or {}
    payload = {"url": url, "params": dict(sorted(params.items()))}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:32]
