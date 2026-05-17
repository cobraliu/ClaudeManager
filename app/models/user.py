from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class User(BaseModel):
    username: str
    password_hash: str = ""
    role: UserRole = UserRole.USER
    is_admin: bool = False
    google_email: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    token: str
    username: str
    role: UserRole
    is_admin: bool = False


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=4, max_length=200)
    role: UserRole = UserRole.USER


class GoogleLoginRequest(BaseModel):
    credential: str  # Google ID token


class ChangePasswordRequest(BaseModel):
    password: str = Field(min_length=4, max_length=200)


class UserInfo(BaseModel):
    username: str
    role: UserRole
    is_admin: bool = False


class SetIsAdminRequest(BaseModel):
    is_admin: bool
