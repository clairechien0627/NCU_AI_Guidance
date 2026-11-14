import json
import os
from dataclasses import dataclass
from typing import Optional

import firebase_admin
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth, credentials


bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    email: str
    name: str
    picture: str
    # Firebase 是否已驗證這個 email；放最後並給預設值，既有的 AuthUser(...) 建構呼叫不用改。
    # 只有 email_verified=True 的帳號會被當成開發者（見 access_control.is_developer）。
    email_verified: bool = False


def verify_firebase_token(token: str) -> AuthUser:
    _ensure_firebase_app()

    try:
        decoded = auth.verify_id_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase auth token") from exc

    return AuthUser(
        user_id=f"firebase:{decoded['uid']}",
        email=decoded.get("email", ""),
        name=decoded.get("name", "") or decoded.get("email", ""),
        picture=decoded.get("picture", ""),
        email_verified=decoded.get("email_verified") is True,
    )


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> AuthUser | None:
    token = credentials.credentials if credentials else request.cookies.get("firebase_id_token")
    if not token:
        return None
    return verify_firebase_token(token)


def get_current_user(user: AuthUser | None = Depends(get_optional_user)) -> AuthUser:
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _ensure_firebase_app() -> None:
    if firebase_admin._apps:
        return

    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if service_account_json:
        try:
            info = json.loads(service_account_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail="Invalid FIREBASE_SERVICE_ACCOUNT_JSON") from exc
        firebase_admin.initialize_app(credentials.Certificate(info))
        return

    project_id = os.getenv("FIREBASE_PROJECT_ID", "").strip()
    options = {"projectId": project_id} if project_id else None
    firebase_admin.initialize_app(options=options)
