from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings


def readiness_status(db: Session, settings: Settings) -> tuple[int, dict]:
    """Check dependencies that determine whether this process can accept work."""

    components: dict[str, dict[str, str]] = {}
    database_ready = False
    try:
        db.execute(text("SELECT 1"))
        database_ready = True
        components["database"] = {"status": "UP"}
    except Exception as exc:
        db.rollback()
        components["database"] = {"status": "DOWN", "reason": type(exc).__name__}

    redis_ready = False
    client = None
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
            decode_responses=True,
        )
        redis_ready = bool(client.ping())
        components["redis"] = {"status": "UP" if redis_ready else "DOWN"}
    except Exception as exc:
        components["redis"] = {"status": "DOWN", "reason": type(exc).__name__}
    finally:
        if client is not None:
            client.close()

    if not database_ready:
        return 503, {"status": "DOWN", "components": components}
    if not redis_ready:
        return 200, {
            "status": "DEGRADED",
            "components": components,
            "note": "Redis 不可用，会话短期记忆将回退到数据库历史；服务仍可用但延迟可能升高",
        }
    return 200, {"status": "UP", "components": components}
