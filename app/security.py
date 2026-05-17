from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Annotated, NamedTuple

import jwt
from fastapi import Depends, Header, HTTPException, status

from app.config import get_jwt_secret
from app.models.user import UserRole


class RateLimiter:
    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self._events[key]
        while q and now - q[0] > self.window_seconds:
            q.popleft()
        if len(q) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
            )
        q.append(now)


rate_limiter = RateLimiter(max_requests=600, window_seconds=60)
login_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)


def create_jwt(username: str, role: UserRole, is_admin: bool = False) -> str:
    payload = {
        "sub": username,
        "role": role.value,
        "is_admin": is_admin,
        "iat": int(time.time()),
        "exp": int(time.time()) + 86400 * 7,  # 7 days
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


def require_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    token = authorization.removeprefix("Bearer ").strip()
    claims = _decode_token(token)
    username = claims.get("sub", "")
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    rate_limiter.check(username)
    return username


def require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    token = authorization.removeprefix("Bearer ").strip()
    claims = _decode_token(token)
    username = claims.get("sub", "")
    role = claims.get("role", "")
    is_admin = claims.get("is_admin", False)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    if role != UserRole.ADMIN.value and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    rate_limiter.check(username)
    return username


class UserInfo(NamedTuple):
    username: str
    is_admin: bool


def require_user_info(
    authorization: Annotated[str | None, Header()] = None,
) -> UserInfo:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    token = authorization.removeprefix("Bearer ").strip()
    claims = _decode_token(token)
    username = claims.get("sub", "")
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    rate_limiter.check(username)
    role = claims.get("role", "")
    is_admin = claims.get("is_admin", False) or role == UserRole.ADMIN.value
    return UserInfo(username=username, is_admin=bool(is_admin))


CurrentUser = Annotated[str, Depends(require_user)]
CurrentUserInfo = Annotated[UserInfo, Depends(require_user_info)]
AdminUser = Annotated[str, Depends(require_admin)]
