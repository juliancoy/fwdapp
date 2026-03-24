import os
import time
from typing import Any, Dict, Optional

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Wix Thin Bridge")

# --- config ---
WIX_TOKEN_INFO_URL = "https://www.wixapis.com/oauth2/token-info"
APP_JWT_SECRET = os.environ.get("APP_JWT_SECRET", "change-me")
APP_JWT_AUDIENCE = os.environ.get("APP_JWT_AUDIENCE", "my-attached-app")
APP_JWT_ISSUER = os.environ.get("APP_JWT_ISSUER", "my-thin-bridge")

# Optional: only enforce these if you know them for your Wix setup.
EXPECTED_WIX_CLIENT_ID = os.environ.get("WIX_CLIENT_ID")          # optional
EXPECTED_WIX_INSTANCE_ID = os.environ.get("WIX_INSTANCE_ID")      # optional

# Demo in-memory "DB"
USERS_BY_EXTERNAL_KEY: dict[str, dict[str, Any]] = {}


class BridgeLoginResponse(BaseModel):
    ok: bool
    app_token: str
    user: Dict[str, Any]


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    return parts[1].strip()


async def wix_token_info(wix_access_token: str) -> Dict[str, Any]:
    """
    Validate the Wix token by asking Wix directly.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            WIX_TOKEN_INFO_URL,
            headers={
                "Authorization": f"Bearer {wix_access_token}",
                "Content-Type": "application/json",
            },
            json={},
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail=f"Wix token validation failed ({resp.status_code})",
        )

    data = resp.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=401, detail="Unexpected Wix token response")

    return data


def _pick_subject(info: Dict[str, Any]) -> str:
    """
    Token Info fields can vary by token/context.
    Prefer common subject-like fields.
    """
    for key in ("sub", "subject", "memberId", "userId", "visitorId"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value

    raise HTTPException(status_code=401, detail="No subject found in Wix token info")


def _pick_instance_id(info: Dict[str, Any]) -> Optional[str]:
    value = info.get("instanceId")
    if isinstance(value, str) and value:
        return value
    return None


def _pick_client_id(info: Dict[str, Any]) -> Optional[str]:
    for key in ("clientId", "aud"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def enforce_expected_wix_values(info: Dict[str, Any]) -> None:
    """
    Optional hardening checks.
    """
    token_client_id = _pick_client_id(info)
    token_instance_id = _pick_instance_id(info)

    if EXPECTED_WIX_CLIENT_ID and token_client_id and token_client_id != EXPECTED_WIX_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Wix client mismatch")

    if EXPECTED_WIX_INSTANCE_ID and token_instance_id and token_instance_id != EXPECTED_WIX_INSTANCE_ID:
        raise HTTPException(status_code=401, detail="Wix instance mismatch")


def find_or_create_local_user(info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map Wix identity -> your local app user.
    """
    subject = _pick_subject(info)
    instance_id = _pick_instance_id(info) or "unknown-instance"

    external_key = f"wix:{instance_id}:{subject}"
    user = USERS_BY_EXTERNAL_KEY.get(external_key)

    if user is None:
        user = {
            "id": f"user_{len(USERS_BY_EXTERNAL_KEY) + 1}",
            "external_key": external_key,
            "provider": "wix",
            "wix_subject": subject,
            "wix_instance_id": instance_id,
            "created_at": int(time.time()),
        }
        USERS_BY_EXTERNAL_KEY[external_key] = user

    return user


def mint_app_token(user: Dict[str, Any]) -> str:
    now = int(time.time())
    payload = {
        "iss": APP_JWT_ISSUER,
        "aud": APP_JWT_AUDIENCE,
        "sub": user["id"],
        "iat": now,
        "exp": now + 60 * 60 * 24 * 7,  # 7 days
        "provider": "wix",
        "wix_subject": user["wix_subject"],
        "wix_instance_id": user["wix_instance_id"],
    }
    return jwt.encode(payload, APP_JWT_SECRET, algorithm="HS256")


@app.post("/auth/wix/login", response_model=BridgeLoginResponse)
async def auth_wix_login(authorization: Optional[str] = Header(default=None)):
    wix_access_token = _extract_bearer_token(authorization)

    # 1) Validate the token with Wix
    token_info = await wix_token_info(wix_access_token)

    # 2) Optional hardening
    enforce_expected_wix_values(token_info)

    # 3) Map Wix identity to your local user
    user = find_or_create_local_user(token_info)

    # 4) Mint your app token
    app_token = mint_app_token(user)

    return BridgeLoginResponse(ok=True, app_token=app_token, user=user)


@app.get("/me")
async def me(authorization: Optional[str] = Header(default=None)):
    token = _extract_bearer_token(authorization)
    try:
        payload = jwt.decode(
            token,
            APP_JWT_SECRET,
            algorithms=["HS256"],
            audience=APP_JWT_AUDIENCE,
            issuer=APP_JWT_ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid app token: {exc}") from exc

    return {"ok": True, "claims": payload}