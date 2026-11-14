"""
access_control.py

即時 AI（對話／推理型 LLM）的存取控制，以及開發者名單判斷。

環境變數：
- LIVE_AI_ACCESS：all（預設，所有人都能用即時 AI，與加入開關前行為相同）
                  developers（只有 DEVELOPER_EMAILS 名單內的帳號能用即時 AI）
  不認得的值一律拋出 LIVE_AI_ACCESS_INVALID，不會悄悄退回 all。
- DEVELOPER_EMAILS：逗號分隔的 email 名單（去空白、不分大小寫），/monitor 也共用這份名單。
  帳號還必須是 Firebase 已驗證的 email（email_verified=True）才算開發者。

只擋會呼叫對話／推理型 LLM 的端點；只呼叫 embedding 的功能（例如課程語意搜尋）不受影響。
"""

import os
from typing import Literal, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.services.auth_service import AuthUser, bearer_scheme, get_optional_user

LiveAiMode = Literal["all", "developers"]

LIVE_AI_ALL: LiveAiMode = "all"
LIVE_AI_DEVELOPERS: LiveAiMode = "developers"
SUPPORTED_LIVE_AI_MODES: tuple[LiveAiMode, ...] = (LIVE_AI_ALL, LIVE_AI_DEVELOPERS)

LIVE_AI_RESTRICTED_CODE = "LIVE_AI_RESTRICTED"
LIVE_AI_RESTRICTED_MESSAGE = "即時 AI 問答目前只開放給開發團隊"


def developer_emails() -> frozenset[str]:
    """解析 DEVELOPER_EMAILS；未設定或全是空白時回傳空集合。"""
    raw = os.getenv("DEVELOPER_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_developer_email(email: str | None) -> bool:
    """email 是否在 DEVELOPER_EMAILS 名單內（去空白、不分大小寫；空 email 一律 False）。"""
    normalized = (email or "").strip().lower()
    if not normalized:
        return False
    return normalized in developer_emails()


def is_developer(user: AuthUser | None) -> bool:
    """這個帳號是不是開發者：email 在 DEVELOPER_EMAILS 名單內，且 Firebase 已驗證該 email。

    只比對 email 字串不夠：未驗證的 email（例如自行註冊、尚未完成驗證的帳號）可以被冒用。
    """
    return user is not None and user.email_verified is True and is_developer_email(user.email)


def live_ai_mode() -> LiveAiMode:
    """回傳目前的 LIVE_AI_ACCESS 模式；未設定為 all，不認得的值直接報錯。"""
    raw = os.getenv("LIVE_AI_ACCESS", "").strip().lower()
    if not raw:
        return LIVE_AI_ALL
    if raw == LIVE_AI_ALL:
        return LIVE_AI_ALL
    if raw == LIVE_AI_DEVELOPERS:
        return LIVE_AI_DEVELOPERS
    raise RuntimeError(
        f"LIVE_AI_ACCESS_INVALID: LIVE_AI_ACCESS 只能是 {' 或 '.join(SUPPORTED_LIVE_AI_MODES)}，收到 {raw!r}。"
    )


def live_ai_allowed(user: AuthUser | None) -> bool:
    """這個使用者（可為 None＝未登入）目前能不能使用即時 AI。"""
    if live_ai_mode() == LIVE_AI_ALL:
        return True
    return is_developer(user)


def require_live_ai(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    """FastAPI dependency：掛在會呼叫對話／推理型 LLM 的端點上。

    mode=all 時直接放行，完全不解析 token（不能用 Depends(get_optional_user)，否則公開端點
    例如單課問答會因為過期／無效的 token 變成 401，行為與加入開關前不同）；端點原本的登入依賴照舊生效。
    mode=developers 時才用與 get_optional_user 相同的邏輯解析 token：無效 token 回 401，
    未登入或不是開發者（含 email 未驗證）回 403 LIVE_AI_RESTRICTED。
    """
    if live_ai_mode() == LIVE_AI_ALL:
        return
    user = get_optional_user(request, credentials)
    if not live_ai_allowed(user):
        raise HTTPException(
            status_code=403,
            detail={"code": LIVE_AI_RESTRICTED_CODE, "message": LIVE_AI_RESTRICTED_MESSAGE},
        )
