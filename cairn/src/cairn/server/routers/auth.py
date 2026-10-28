from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from cairn.server import auth

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("username", "password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=auth.MIN_NEW_PASSWORD_LENGTH, max_length=1024)

    @field_validator("current_password", "new_password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


@router.post("/auth/login")
def login(request: Request, body: LoginRequest):
    auth.check_login_rate_limit(request)
    if not auth.authenticate_password(body.username, body.password):
        raise HTTPException(401, "Invalid username or password")
    auth.clear_login_failures(request)
    return auth.login_response(request, body.username)


@router.post("/auth/logout")
def logout(request: Request):
    return auth.logout_response(request)


@router.get("/auth/me")
def me(request: Request):
    identity = auth.authenticate_request(request)
    if identity is None or identity.via != "session":
        raise HTTPException(401, "Not authenticated")
    return {"username": identity.username, "csrf_token": identity.csrf_token}


@router.post("/auth/password")
def change_password(request: Request, body: ChangePasswordRequest):
    identity = getattr(request.state, "identity", None)
    if identity is None:
        identity = auth.authenticate_request(request)
    if identity is None:
        raise HTTPException(401, "Not authenticated")
    if identity.via != "session":
        raise HTTPException(403, "Password can only be changed from a logged-in session")
    auth.change_password(
        identity.username,
        body.current_password,
        body.new_password,
        keep_session_id=identity.session_id,
    )
    return {"ok": True}
