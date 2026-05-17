from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status

from app.config import get_google_client_id
from app.models.user import (
    ChangePasswordRequest,
    CreateUserRequest,
    GoogleLoginRequest,
    LoginRequest,
    LoginResponse,
    SetIsAdminRequest,
    UserInfo,
    UserRole,
)
from app.security import AdminUser, CurrentUser, create_jwt, login_rate_limiter
from app.services.user_store import UserStore

router = APIRouter(prefix="/api/auth", tags=["auth"])

_user_store: UserStore | None = None

# Autologin token TTL: tokens older than this are rejected even if file matches.
_AUTOLOGIN_TTL_SECONDS = 300  # 5 minutes


def configure(user_store: UserStore) -> None:
    global _user_store
    _user_store = user_store


def _get_store() -> UserStore:
    assert _user_store is not None
    return _user_store


@router.get("/autologin")
def autologin(token: str = Query(...)) -> LoginResponse:
    """Exchange a one-time autologin token (written by the CLI) for a JWT.

    The CLI writes the token to ~/.claudemanager/autologin.token before opening
    the browser.  The file is deleted after one successful use.
    Only works when CLAUDEMANAGER_DATA_DIR is set (i.e. packaged / managed mode).
    """
    from app.config import _data_dir
    dd = _data_dir()
    if dd is None:
        raise HTTPException(status_code=403, detail="autologin not available in dev mode")

    token_file = dd / "autologin.token"
    if not token_file.exists():
        raise HTTPException(status_code=403, detail="autologin token not found or already used")

    try:
        content = token_file.read_text().strip()
        parts = content.split(":", 1)
        stored_token = parts[0]
        written_at = float(parts[1]) if len(parts) > 1 else 0.0
    except Exception:
        raise HTTPException(status_code=403, detail="invalid autologin token file")

    if token != stored_token:
        raise HTTPException(status_code=403, detail="invalid autologin token")

    if time.time() - written_at > _AUTOLOGIN_TTL_SECONDS:
        token_file.unlink(missing_ok=True)
        raise HTTPException(status_code=403, detail="autologin token expired")

    # Consume the token (one-time use)
    token_file.unlink(missing_ok=True)

    import os as _os
    import secrets as _secrets

    store = _get_store()
    local_username = _os.environ.get("USER") or _os.environ.get("USERNAME") or "local"

    user = store.get(local_username)
    if user is None:
        user = store.create_user(local_username, _secrets.token_urlsafe(32), UserRole.ADMIN)
        store.set_is_admin(local_username, True)

    jwt_token = create_jwt(user.username, user.role, is_admin=True)
    return LoginResponse(token=jwt_token, username=user.username, role=user.role, is_admin=True)


@router.get("/google-client-id")
def google_client_id() -> dict[str, str]:
    return {"client_id": get_google_client_id()}


@router.post("/google")
def google_login(body: GoogleLoginRequest) -> LoginResponse:
    client_id = get_google_client_id()
    if not client_id:
        raise HTTPException(status_code=501, detail="Google login not configured")

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        idinfo = id_token.verify_oauth2_token(body.credential, google_requests.Request(), client_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid Google token: {e}")

    email: str = idinfo.get("email", "")
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No email in Google token")

    store = _get_store()

    # Find existing user linked to this Google email
    user = store.find_by_google_email(email)
    if user is None:
        # Auto-create: first Google user becomes admin, rest are regular users
        username = email.split("@")[0].replace(".", "_").replace("+", "_")
        # Ensure unique username
        base = username
        i = 2
        while store.get(username) is not None:
            username = f"{base}{i}"
            i += 1
        is_first = store.is_empty()
        role = UserRole.ADMIN if is_first else UserRole.USER
        user = store.create_google_user(username, email, role)
        if is_first:
            store.set_is_admin(username, True)

    token = create_jwt(user.username, user.role, user.is_admin)
    return LoginResponse(token=token, username=user.username, role=user.role, is_admin=user.is_admin)


@router.post("/login")
def login(body: LoginRequest) -> LoginResponse:
    login_rate_limiter.check(body.username)
    store = _get_store()
    user = store.verify_password(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    token = create_jwt(user.username, user.role, user.is_admin)
    return LoginResponse(token=token, username=user.username, role=user.role, is_admin=user.is_admin)


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUserRequest, admin: AdminUser) -> UserInfo:
    store = _get_store()
    try:
        user = store.create_user(body.username, body.password, body.role)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return UserInfo(username=user.username, role=user.role)


@router.get("/users")
def list_users(admin: AdminUser) -> list[UserInfo]:
    store = _get_store()
    return [UserInfo(username=u.username, role=u.role, is_admin=u.is_admin) for u in store.list_users()]


@router.put("/users/{username}/is_admin", status_code=200)
def set_is_admin(username: str, body: SetIsAdminRequest, admin: AdminUser) -> UserInfo:
    store = _get_store()
    if not store.set_is_admin(username, body.is_admin):
        raise HTTPException(status_code=404, detail="user not found")
    user = store.get(username)
    assert user is not None
    return UserInfo(username=user.username, role=user.role, is_admin=user.is_admin)


@router.put("/users/{username}/password")
def change_password(username: str, body: ChangePasswordRequest, current_user: CurrentUser) -> dict[str, str]:
    store = _get_store()
    # Users can change their own password; admins can change anyone's
    user = store.get(current_user)
    if user is None:
        raise HTTPException(status_code=404)
    if current_user != username and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not allowed")
    if not store.change_password(username, body.password):
        raise HTTPException(status_code=404, detail="user not found")
    return {"status": "ok"}


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(username: str, admin: AdminUser) -> None:
    store = _get_store()
    if username == admin:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    if not store.delete_user(username):
        raise HTTPException(status_code=404, detail="user not found")
