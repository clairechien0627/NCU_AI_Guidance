"""
monitor.py

GET /api/monitor/stats — 開發人員系統監控 API
只有 DEVELOPER_EMAILS 環境變數中列出的 email 可存取。
"""

import os
from datetime import datetime, timedelta, timezone, date as date_type
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.services import latency_store as ls
import time as _time
from app.services import session_store as ss
from app.services.access_control import developer_emails, is_developer
from app.services.auth_service import AuthUser, get_current_user

router = APIRouter()
_TW = timezone(timedelta(hours=8))
_DAYS_MAP = {"1d": 1, "7d": 7, "30d": 30}


def _require_developer(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not developer_emails():
        raise HTTPException(status_code=403, detail="開發人員專用功能（DEVELOPER_EMAILS 未設定）")
    # 名單內且 Firebase 已驗證 email 才算開發者（未驗證的 email 可被冒用）
    if not is_developer(user):
        raise HTTPException(status_code=403, detail="開發人員專用功能")
    return user


@router.get("/db-stats")
async def get_db_stats(user: AuthUser = Depends(_require_developer)):
    """回傳 PostgreSQL / Qdrant / Cloudinary 統計 + 基礎設施資訊（60 秒快取）。"""
    result = ss.get_db_stats()
    result["infra"]["server_uptime_seconds"] = ls.get_server_uptime_seconds()
    return result


@router.get("/stats")
async def get_monitor_stats(
    user: AuthUser = Depends(_require_developer),
    preset: Optional[Literal["1d", "7d", "30d"]] = Query(default=None),
    start: Optional[str] = Query(default=None),  # YYYY-MM-DD 台灣時間
    end:   Optional[str] = Query(default=None),  # YYYY-MM-DD 台灣時間
    feature: Literal["all", "course", "pdf"] = Query(default="all"),
):
    """回傳系統健康、全時期統計、時段統計、圖表資料。"""
    today = datetime.now(_TW).date()

    if start and end:
        try:
            sd = date_type.fromisoformat(start)
            ed = date_type.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=422, detail="日期格式錯誤，請用 YYYY-MM-DD")
        if sd > ed:
            raise HTTPException(status_code=422, detail="start 不可晚於 end")
        start_dt = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=_TW)
        end_dt   = datetime(ed.year, ed.month, ed.day, 23, 59, 59, tzinfo=_TW)
        cache_key = None  # 自訂日期不快取
    else:
        days = _DAYS_MAP.get(preset or "7d", 7)
        end_dt   = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=_TW)
        start_dt = (end_dt - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
        cache_key = preset or "7d"

    stats = ss.get_monitor_stats(start_dt=start_dt, end_dt=end_dt, cache_key=cache_key, feature=feature)
    stats["latency_stats"] = ls.get_latency_stats()
    stats["date_range"] = {
        "start": start_dt.strftime("%Y-%m-%d"),
        "end":   end_dt.strftime("%Y-%m-%d"),
    }
    stats["model_name"] = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "unknown")
    stats["pdf_model_name"] = "gpt-4o"
    stats["pdf_router_model_name"] = "gpt-4o-mini"
    stats["server_uptime_seconds"] = ls.get_server_uptime_seconds()
    return stats
