from __future__ import annotations

import hashlib
import threading
import time

from app.core.config import Settings


class DistributedRateLimiter:
    """Redis fixed-window limiter with a bounded process-local fallback."""

    _local: dict[str, tuple[int, float]] = {}
    _lock = threading.Lock()
    _max_local_keys = 10_000

    def __init__(self, settings: Settings):
        self.settings = settings
        self._redis = None
        self._redis_unavailable = False
        self._redis_retry_at = 0.0

    def allow(self, namespace: str, subject: str, limit: int, window_seconds: int = 60) -> bool:
        if limit <= 0:
            return True
        key = self._key(namespace, subject, window_seconds)
        if self._redis_unavailable and time.monotonic() >= self._redis_retry_at:
            self._redis_unavailable = False
            self._redis = None
        if not self._redis_unavailable:
            try:
                client = self._client()
                pipeline = client.pipeline(transaction=True)
                pipeline.incr(key)
                pipeline.expire(key, window_seconds)
                count, _ = pipeline.execute()
                count = int(count)
                return count <= limit
            except Exception:
                # Authentication and chat must remain available during a Redis
                # outage, but the process-local limiter still bounds abuse.
                self._redis_unavailable = True
                self._redis_retry_at = time.monotonic() + 30.0
        return self._allow_local(key, limit, window_seconds)

    def _client(self):
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=self.settings.redis_socket_timeout_seconds,
                socket_timeout=self.settings.redis_socket_timeout_seconds,
                decode_responses=True,
            )
        return self._redis

    @classmethod
    def _allow_local(cls, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with cls._lock:
            count, expires_at = cls._local.get(key, (0, now + window_seconds))
            if expires_at <= now:
                count, expires_at = 0, now + window_seconds
            count += 1
            cls._local[key] = (count, expires_at)
            if len(cls._local) > cls._max_local_keys:
                expired = [item for item, (_, expiry) in cls._local.items() if expiry <= now]
                for item in expired:
                    cls._local.pop(item, None)
                while len(cls._local) > cls._max_local_keys:
                    cls._local.pop(next(iter(cls._local)))
            return count <= limit

    @staticmethod
    def _key(namespace: str, subject: str, window_seconds: int) -> str:
        bucket = int(time.time()) // window_seconds
        digest = hashlib.sha256(subject.encode("utf-8", errors="ignore")).hexdigest()[:32]
        return f"psych-ai:rate:{namespace}:{bucket}:{digest}"


_limiters: dict[tuple[str, float], DistributedRateLimiter] = {}
_limiters_lock = threading.Lock()


def get_rate_limiter(settings: Settings) -> DistributedRateLimiter:
    key = (settings.redis_url, float(settings.redis_socket_timeout_seconds))
    with _limiters_lock:
        limiter = _limiters.get(key)
        if limiter is None:
            limiter = DistributedRateLimiter(settings)
            _limiters[key] = limiter
        return limiter
