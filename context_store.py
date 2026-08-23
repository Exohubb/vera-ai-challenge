"""Versioned, idempotent context storage.

Spec requirements (section 2 / 6.1 / 7):
- Idempotent by (scope, context_id): re-posting the same version is a no-op.
- A higher version replaces atomically, never partially.
- Concurrent writes must be guarded (lock / CAS) so a race can't corrupt state.
- Backed by Redis when available (required in production so multiple
  Gunicorn workers share state), but falls back to a thread-safe in-memory
  store when Redis isn't reachable -- e.g. local dev without `redis-server`
  installed. This keeps the exact same interface either way.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import redis as redis_lib
except ImportError:  # pragma: no cover
    redis_lib = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class StoredContext:
    scope: str
    context_id: str
    version: int
    payload: dict
    delivered_at: Optional[str]
    stored_at: str
    ack_id: str


class _InMemoryBackend:
    """Thread-safe in-process fallback. Not shared across workers -- fine
    for single-process local dev/testing, not for a multi-worker deploy."""

    def __init__(self):
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}

    def cas_write(self, key: str, version: int, record: dict) -> tuple[str, dict]:
        """Returns (status, record) where status is one of:
        "written" (new key or higher version accepted), "same_version"
        (idempotent no-op, re-posting a version already stored), or
        "stale" (version is lower than what's stored -- caller should
        reject with 409 per the official contract)."""
        with self._lock:
            existing = self._data.get(key)
            if existing is not None:
                if version < existing["version"]:
                    return "stale", existing
                if version == existing["version"]:
                    return "same_version", existing
            self._data[key] = record
            return "written", record

    def read(self, key: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(key)

    def all_by_scope(self, scope: str) -> list[dict]:
        with self._lock:
            return [v for k, v in self._data.items() if k.startswith(f"{scope}:")]

    def count_by_scope(self, scope: str) -> int:
        with self._lock:
            return sum(1 for k in self._data if k.startswith(f"{scope}:"))

    def ping(self) -> bool:
        return True


class _RedisBackend:
    """Redis-backed store. Uses a Lua-esque CAS via WATCH/MULTI to avoid
    torn writes between concurrent requests hitting different workers."""

    def __init__(self, url: str):
        self._client = redis_lib.Redis.from_url(url, decode_responses=True)
        self._client.ping()  # raises if unreachable

    def cas_write(self, key: str, version: int, record: dict) -> tuple[str, dict]:
        redis_key = f"ctx:{key}"
        with self._client.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(redis_key)
                    existing_raw = pipe.get(redis_key)
                    existing = json.loads(existing_raw) if existing_raw else None
                    if existing is not None:
                        if version < existing["version"]:
                            pipe.unwatch()
                            return "stale", existing
                        if version == existing["version"]:
                            pipe.unwatch()
                            return "same_version", existing
                    pipe.multi()
                    pipe.set(redis_key, json.dumps(record))
                    pipe.execute()
                    return "written", record
                except redis_lib.WatchError:
                    continue

    def read(self, key: str) -> Optional[dict]:
        raw = self._client.get(f"ctx:{key}")
        return json.loads(raw) if raw else None

    def all_by_scope(self, scope: str) -> list[dict]:
        keys = self._client.keys(f"ctx:{scope}:*")
        if not keys:
            return []
        return [json.loads(v) for v in self._client.mget(keys) if v]

    def count_by_scope(self, scope: str) -> int:
        return len(self._client.keys(f"ctx:{scope}:*"))

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False


class ContextStore:
    """Public interface used by the rest of the app. Picks Redis if
    REDIS_URL is set and reachable, otherwise degrades to in-memory."""

    def __init__(self, redis_url: Optional[str] = None):
        redis_url = redis_url or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.backend_name = "memory"
        self._backend = None
        if redis_lib is not None:
            try:
                self._backend = _RedisBackend(redis_url)
                self.backend_name = "redis"
            except Exception:
                self._backend = None
        if self._backend is None:
            self._backend = _InMemoryBackend()
            self.backend_name = "memory"

    def is_healthy(self) -> bool:
        try:
            return self._backend.ping()
        except Exception:
            return False

    def push_context(
        self,
        scope: str,
        context_id: str,
        version: int,
        payload: dict,
        delivered_at: Optional[str] = None,
    ) -> tuple[str, StoredContext]:
        """Returns (status, stored_record). status is "written" for a new
        key or an accepted higher-version replace, "same_version" for an
        idempotent no-op (spec: re-posting the same version is a no-op),
        or "stale" for a rejected lower-version write -- the caller
        (main.py) is responsible for turning "stale" into the spec's
        409 {accepted:false, reason:stale_version, current_version} shape.
        Either way the currently-stored record is returned."""
        key = f"{scope}:{context_id}"
        record = {
            "scope": scope,
            "context_id": context_id,
            "version": version,
            "payload": payload,
            "delivered_at": delivered_at,
            "stored_at": _now_iso(),
            "ack_id": f"ack_{uuid.uuid4().hex[:12]}",
        }
        status, final_record = self._backend.cas_write(key, version, record)
        return status, StoredContext(**final_record)

    def get(self, scope: str, context_id: str) -> Optional[StoredContext]:
        rec = self._backend.read(f"{scope}:{context_id}")
        return StoredContext(**rec) if rec else None

    def all(self, scope: str) -> list[StoredContext]:
        return [StoredContext(**rec) for rec in self._backend.all_by_scope(scope)]

    def counts(self) -> dict[str, int]:
        return {
            scope: self._backend.count_by_scope(scope)
            for scope in ("category", "merchant", "customer", "trigger")
        }
