"""Shared key/value state backend: Redis-backed when reachable, in-memory
thread-safe fallback otherwise. Used by context_store, suppression, and
conversation modules so they all share one connection/degradation policy
instead of each reinventing it.

Redis is required in production (section 6.1: 2 Gunicorn workers must
share state), but local dev/testing without `redis-server` running should
still work correctly for a single process.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional

try:
    import redis as redis_lib
except ImportError:  # pragma: no cover
    redis_lib = None

_shared_redis_client = None
_shared_redis_lock = threading.Lock()
_redis_unavailable = False

# In-memory fallback must be shared *per namespace* across all StateBackend
# instances in the same process (mirrors how Redis is shared across
# workers) -- otherwise two independently-constructed StateBackend objects
# for the same namespace (e.g. suppression created in main.py vs inside
# ConversationManager) would silently diverge, corrupting state.
_shared_memory_stores: dict[str, dict] = {}
_shared_memory_lock = threading.RLock()


def _get_shared_memory_dict(namespace: str) -> dict:
    with _shared_memory_lock:
        if namespace not in _shared_memory_stores:
            _shared_memory_stores[namespace] = {}
        return _shared_memory_stores[namespace]


def _get_shared_redis_client(redis_url: str):
    global _shared_redis_client, _redis_unavailable
    if _redis_unavailable or redis_lib is None:
        return None
    if _shared_redis_client is not None:
        return _shared_redis_client
    with _shared_redis_lock:
        if _shared_redis_client is not None:
            return _shared_redis_client
        try:
            client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
            client.ping()
            _shared_redis_client = client
            return client
        except Exception:
            _redis_unavailable = True
            return None


class StateBackend:
    """Namespaced key/value store with JSON values."""

    def __init__(self, namespace: str, redis_url: Optional[str] = None):
        self.namespace = namespace
        redis_url = redis_url or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self._client = _get_shared_redis_client(redis_url)
        self.backend_name = "redis" if self._client is not None else "memory"
        if self._client is None:
            self._mem_lock = _shared_memory_lock
            self._mem: dict[str, dict] = _get_shared_memory_dict(namespace)

    def _k(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def get(self, key: str) -> Optional[dict]:
        if self._client is not None:
            raw = self._client.get(self._k(key))
            return json.loads(raw) if raw else None
        with self._mem_lock:
            val = self._mem.get(self._k(key))
            return json.loads(json.dumps(val)) if val is not None else None

    def set(self, key: str, value: dict, ttl_seconds: Optional[int] = None) -> None:
        if self._client is not None:
            self._client.set(self._k(key), json.dumps(value), ex=ttl_seconds)
            return
        with self._mem_lock:
            self._mem[self._k(key)] = value
            # TTL not enforced for in-memory fallback (dev-only path); good
            # enough since it's never used for the production deployment.

    def delete(self, key: str) -> None:
        if self._client is not None:
            self._client.delete(self._k(key))
            return
        with self._mem_lock:
            self._mem.pop(self._k(key), None)

    def keys(self, prefix: str = "") -> list[str]:
        pattern = self._k(prefix) + "*"
        if self._client is not None:
            full_keys = self._client.keys(pattern)
            plen = len(self.namespace) + 1
            return [k[plen:] for k in full_keys]
        with self._mem_lock:
            plen = len(self.namespace) + 1
            full_prefix = self._k(prefix)
            return [k[plen:] for k in self._mem if k.startswith(full_prefix)]

    def ping(self) -> bool:
        if self._client is not None:
            try:
                return bool(self._client.ping())
            except Exception:
                return False
        return True
