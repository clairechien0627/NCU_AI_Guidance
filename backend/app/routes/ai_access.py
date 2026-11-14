"""
ai_access.py

GET /api/ai-access — 回報目前帳號能不能使用即時 AI（對話／推理型 LLM）。
未登入也可呼叫；前端依此決定要顯示提問輸入框或說明元件。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.services.access_control import LIVE_AI_ALL, LiveAiMode, live_ai_allowed, live_ai_mode
from app.services.auth_service import bearer_scheme, get_optional_user

router = APIRouter()


class AiAccessResponse(BaseModel):
    mode: LiveAiMode
    live_ai: bool


@router.get("", response_model=AiAccessResponse)
async def get_ai_access(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    # 回應依登入身分而不同，不讓瀏覽器或中間層快取
    response.headers["Cache-Control"] = "private, no-store"
    mode = live_ai_mode()
    if mode == LIVE_AI_ALL:
        # 與 require_live_ai 一致：mode=all 不解析 token，無效／過期 token 也不會回 401
        return AiAccessResponse(mode=mode, live_ai=True)
    return AiAccessResponse(mode=mode, live_ai=live_ai_allowed(get_optional_user(request, credentials)))
