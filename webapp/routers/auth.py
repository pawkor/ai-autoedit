"""Auth routes: /api/auth/*"""

import secrets

from fastapi import APIRouter, HTTPException, Request, Body
from fastapi.responses import JSONResponse

from webapp.state import (
    ENABLE_AUTH,
    _sessions,
    _load_users,
    _save_users,
    _hash_pw,
    _verify_pw,
    _get_session_user,
)

router = APIRouter()


@router.get("/api/auth/status")
async def auth_status(request: Request):
    users = _load_users()
    user = _get_session_user(request)
    return {
        "enabled":       ENABLE_AUTH,
        "has_users":     len(users) > 0,
        "authenticated": not ENABLE_AUTH or user is not None,
        "username":      user,
    }


@router.post("/api/auth/login")
async def auth_login(request: Request, data: dict = Body(...)):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    users = _load_users()
    match = next((u for u in users if u["username"] == username and _verify_pw(password, u["password_hash"])), None)
    if not match:
        raise HTTPException(401, "Invalid credentials")
    if not match["password_hash"].startswith("pbkdf2:"):
        match["password_hash"] = _hash_pw(password)
        _save_users(users)
    token = secrets.token_hex(32)
    _sessions[token] = username
    response = JSONResponse({"ok": True, "username": username})
    response.set_cookie("ae_session", token, httponly=True, samesite="strict", max_age=86400 * 30)
    return response


@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get("ae_session")
    if token:
        _sessions.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("ae_session")
    return response


@router.get("/api/auth/users")
async def get_auth_users(request: Request):
    users = _load_users()
    if ENABLE_AUTH and users and not _get_session_user(request):
        raise HTTPException(401)
    return [{"username": u["username"]} for u in users]


@router.post("/api/auth/users")
async def create_auth_user(request: Request, data: dict = Body(...)):
    if ENABLE_AUTH:
        existing = _load_users()
        if existing and not _get_session_user(request):
            raise HTTPException(401)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    users = _load_users()
    if any(u["username"] == username for u in users):
        raise HTTPException(409, "User already exists")
    users.append({"username": username, "password_hash": _hash_pw(password)})
    _save_users(users)
    return {"ok": True}


@router.delete("/api/auth/users/{username}")
async def delete_auth_user(request: Request, username: str):
    if ENABLE_AUTH and not _get_session_user(request):
        raise HTTPException(401)
    users = _load_users()
    new_users = [u for u in users if u["username"] != username]
    if len(new_users) == len(users):
        raise HTTPException(404, "User not found")
    if not new_users:
        raise HTTPException(400, "Cannot delete last user")
    _save_users(new_users)
    return {"ok": True}


@router.patch("/api/auth/users/{username}")
async def update_auth_user(request: Request, username: str, data: dict = Body(...)):
    if ENABLE_AUTH and not _get_session_user(request):
        raise HTTPException(401)
    password = data.get("password") or ""
    if not password:
        raise HTTPException(400, "Password required")
    users = _load_users()
    user = next((u for u in users if u["username"] == username), None)
    if not user:
        raise HTTPException(404, "User not found")
    user["password_hash"] = _hash_pw(password)
    _save_users(users)
    return {"ok": True}
