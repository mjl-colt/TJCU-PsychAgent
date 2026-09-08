import base64
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import get_settings
from app.models.entities import UserAccount
from app.services.rate_limit import get_rate_limiter


PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
PASSWORD_MAX_BYTES = 1024


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    raw = password.encode("utf-8")
    if len(raw) > PASSWORD_MAX_BYTES:
        raise ValueError("password is too long")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw, salt, iterations)
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, hashed: str) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > PASSWORD_MAX_BYTES:
        return False
    if hashed.startswith(f"{PASSWORD_ALGORITHM}$"):
        try:
            _, iterations_raw, salt_raw, digest_raw = hashed.split("$", 3)
            iterations = int(iterations_raw)
            if iterations <= 0 or iterations > 10_000_000:
                return False
            salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_raw.encode("ascii"))
            actual = hashlib.pbkdf2_hmac("sha256", raw, salt, iterations)
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False
    # Backward-compatible verification for the former unsalted SHA-256
    # format. A successful login immediately upgrades this legacy value.
    legacy = hashlib.sha256(raw).hexdigest()
    return len(hashed) == 64 and hmac.compare_digest(legacy, hashed)


def password_hash_needs_upgrade(hashed: str) -> bool:
    if not hashed.startswith(f"{PASSWORD_ALGORITHM}$"):
        return True
    try:
        return int(hashed.split("$", 2)[1]) < PASSWORD_ITERATIONS
    except (ValueError, IndexError):
        return True


def _credentials(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Basic authorization")
    if len(header) > 4096:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authorization header is too large")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Basic authorization") from exc


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    username, password = _credentials(request)
    client_host = request.client.host if request.client else "unknown"
    settings = get_settings()
    if not get_rate_limiter(settings).allow(
        "auth",
        f"{client_host}:{username}",
        settings.auth_rate_limit_per_minute,
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many authentication attempts",
            headers={"Retry-After": "60"},
        )
    user = db.query(UserAccount).filter(UserAccount.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials")
    if password_hash_needs_upgrade(user.password_hash):
        user.password_hash = hash_password(password)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    if "ROLE_ADMIN" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user
