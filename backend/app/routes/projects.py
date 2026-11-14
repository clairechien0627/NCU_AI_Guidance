import asyncio
import json as _json
import logging
import os
import re as _re
from datetime import datetime, timezone
from functools import lru_cache
from typing import List, Optional
from urllib.parse import quote

# Strict UUID4 pattern — reject any header value that doesn't match.
# Prevents clients from spoofing authenticated user_ids (which never look like UUIDs)
# and limits garbage input from reaching the quota system.
_UUID4_RE = _re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    _re.IGNORECASE,
)

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.models.schemas import Project, ChatRequest, ChatResponse
from app.services.access_control import require_live_ai
from app.services.auth_service import get_optional_user, get_current_user, AuthUser

logger = logging.getLogger(__name__)
router = APIRouter()


async def _require_pdf_chat() -> None:
    from app import app_state
    if not app_state.pdf_chat_ready:
        raise HTTPException(status_code=503, detail="PDF 問答服務目前不可用，請稍後再試。")

# Per-user stream lock: prevents duplicate concurrent requests for the same user+project.
# Key: "{project_id}:{user_id_or_anon}"
_stream_lock: dict[str, str] = {}  # key → active thread_id
_stream_lock_mutex = asyncio.Lock()  # makes check-and-set atomic within a single asyncio worker

# project_id → pdf_documents.id cache (stable for the lifetime of the process)
_project_doc_id_cache: dict[str, int | None] = {}

HEARTBEAT_INTERVAL = 15.0  # seconds between SSE heartbeats while waiting for LLM tokens

_ROUTE_LABELS = {
    "chat":      "直接回答",
    "retrieval": "搜尋文獻",
    "research":  "深度研究",
}


# ── 資料載入 ───────────────────────────────────────────────────────────────────

def load_projects():
    data_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'processed', 'projects.json')
    with open(data_path, 'r', encoding='utf-8') as f:
        projects = _json.load(f)
    for project in projects:
        pdf_path = project.get('pdfPath')
        if pdf_path:
            project['pdfUrl'] = build_pdf_url(pdf_path)
    return projects


def build_pdf_url(pdf_path: str) -> str:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    project_folder = os.getenv("CLOUDINARY_PROJECT_FOLDER")
    if cloud_name and project_folder:
        encoded_parts = [quote(part.replace("&", "and"), safe="") for part in pdf_path.split("\\")]
        encoded_path = "/".join(encoded_parts)
        encoded_folder = "/".join(quote(part, safe="") for part in project_folder.strip("/").split("/"))
        return f"https://res.cloudinary.com/{cloud_name}/raw/upload/{encoded_folder}/{encoded_path}"
    encoded_path = "/".join(quote(part, safe="") for part in pdf_path.split("\\"))
    return f"/pdfs/{encoded_path}"


# ── 既有 GET endpoints（不動）──────────────────────────────────────────────────

@router.get("", response_model=List[Project])
async def get_projects(
    department: Optional[str] = Query(None, description="科系篩選"),
    year: Optional[str] = Query(None, description="學年度篩選"),
):
    projects = load_projects()
    if department:
        projects = [p for p in projects if p['department'] == department]
    if year:
        projects = [p for p in projects if p['year'] == year]
    return projects


@lru_cache(maxsize=1)
def _build_doc_meta_map() -> dict[int, dict]:
    """document_id → {title, department, year}，從 projects.json 一次建立並快取。"""
    result: dict[int, dict] = {}
    for p in load_projects():
        doc_id = p.get("documentId")
        if doc_id is not None:
            result[int(doc_id)] = {
                "title":      p.get("title", ""),
                "department": p.get("department", ""),
                "year":       p.get("year", ""),
            }
    return result


@router.get("/analytics", dependencies=[Depends(_require_pdf_chat)])
async def get_pdf_user_analytics(user: AuthUser = Depends(get_current_user)):
    """取得目前使用者的研究計畫探索分析（需登入，15 分鐘快取）。"""
    from app.services.session_store import get_pdf_analytics
    return await asyncio.to_thread(get_pdf_analytics, user.user_id, _build_doc_meta_map())


@router.get("/recent-chats", dependencies=[Depends(_require_pdf_chat)])
async def get_recent_chats(user: AuthUser = Depends(get_current_user)):
    """最近對話過的 project，每個 document_id 取最新一條，回傳前 5 筆。"""
    from app.database_pdf import PdfSessionLocal
    from app.models.pdf_models import PdfConversation

    def _load():
        with PdfSessionLocal() as db:
            rows = (
                db.query(PdfConversation)
                .filter(
                    PdfConversation.user_id == user.user_id,
                    PdfConversation.document_id.isnot(None),
                )
                .order_by(PdfConversation.id.desc())
                .limit(50)
                .all()
            )
            seen: set[int] = set()
            result = []
            for c in rows:
                if c.document_id not in seen:
                    seen.add(c.document_id)
                    result.append({
                        "document_id": c.document_id,
                        "thread_id":   c.thread_id,
                        "title":       c.title or "未命名對話",
                        "created_at":  c.created_at.isoformat() if c.created_at else None,
                    })
                if len(result) == 5:
                    break
            return result

    return await asyncio.to_thread(_load)


@router.get("/{project_id}", response_model=Project)
async def get_project_by_id(project_id: str):
    for project in load_projects():
        if project['id'] == project_id:
            return project
    raise HTTPException(status_code=404, detail="Project not found")


# ── PDF chat 輔助函式 ──────────────────────────────────────────────────────────

def _get_document_id_for_project(project_id: str) -> int | None:
    """project_id → pdf_documents.id。優先使用 projects.json 的 documentId 欄位精確查詢；
    未填寫時 fallback 至 pdfPath 檔名 ilike 查詢。結果快取於 process 生命周期。"""
    if project_id in _project_doc_id_cache:
        return _project_doc_id_cache[project_id]
    try:
        from app.database_pdf import PdfSessionLocal
        from app.models.pdf_models import PdfDocument

        project = next((p for p in load_projects() if p["id"] == project_id), None)
        if not project:
            # 不存在的 project_id 不寫入快取：任何人都能送任意 project_id，
            # 寫入會讓這個 process 內的快取無限成長。快取筆數因此以 projects.json 的專案數為上限。
            return None

        # Fast path: projects.json already has documentId populated by the populate script.
        # Validate the row still exists and is ready (one indexed PK lookup; result is cached).
        if project.get("documentId") is not None:
            doc_id = int(project["documentId"])
            with PdfSessionLocal() as db:
                exists = db.query(PdfDocument.id).filter_by(
                    id=doc_id, status="ready"
                ).first()
            if exists:
                _project_doc_id_cache[project_id] = doc_id
                return doc_id
            # Row missing or not ready — fall through to ilike re-discovery.

        # Fallback: derive from pdfPath filename via ilike.
        if not project.get("pdfPath"):
            _project_doc_id_cache[project_id] = None
            return None
        import unicodedata as _ud
        filename = _ud.normalize("NFKC", project["pdfPath"].replace("\\", "/").split("/")[-1])
        # Escape SQL wildcard characters so literal % and _ in filenames match correctly.
        safe_fn = filename.replace("%", r"\%").replace("_", r"\_")
        with PdfSessionLocal() as db:
            doc = db.query(PdfDocument.id).filter(
                PdfDocument.filename.ilike(f"%{safe_fn}%", escape="\\"),
                PdfDocument.status == "ready",
            ).first()
        result = doc.id if doc else None
        if result is not None:
            _project_doc_id_cache[project_id] = result
        return result
    except Exception as exc:
        logger.warning("_get_document_id_for_project failed: %s", exc)
        return None


def _get_or_create_pdf_conversation(
    thread_id: str,
    user_id: str | None,
    document_id: int,
):
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="此端點需要登入或有效的匿名工作階段（X-Anon-Session header）。",
        )

    from app.database_pdf import PdfSessionLocal
    from app.models.pdf_models import PdfConversation
    from sqlalchemy.exc import IntegrityError

    for attempt in range(2):
        with PdfSessionLocal() as db:
            conv = db.query(PdfConversation).filter_by(thread_id=thread_id).first()
            if conv:
                if conv.document_id is not None and conv.document_id != document_id:
                    raise HTTPException(
                        status_code=403,
                        detail="此對話紀錄不屬於本論文，請重新開始對話。",
                    )
                if conv.user_id != user_id:
                    raise HTTPException(
                        status_code=403,
                        detail="此對話紀錄不屬於目前使用者，請重新開始對話。",
                    )
                if conv.document_id is None:
                    conv.document_id = document_id
                    db.commit()
                return conv
            new_conv = PdfConversation(
                thread_id=thread_id,
                user_id=user_id,
                document_id=document_id,
            )
            db.add(new_conv)
            try:
                db.commit()
                db.refresh(new_conv)
                return new_conv
            except IntegrityError:
                db.rollback()
                if attempt == 0:
                    continue  # race condition: re-query to get the winner's record
                raise
    raise RuntimeError("unreachable")


def _is_within_ttl(stream_started_at: datetime, ttl_seconds: int = 30) -> bool:
    return (datetime.now(timezone.utc) - stream_started_at).total_seconds() < ttl_seconds


def _mark_stream(thread_id: str, started_at: datetime | None) -> None:
    try:
        from app.database_pdf import PdfSessionLocal
        from app.models.pdf_models import PdfConversation

        with PdfSessionLocal() as db:
            conv = db.query(PdfConversation).filter_by(thread_id=thread_id).first()
            if conv:
                conv.stream_started_at = started_at
                db.commit()
    except Exception as exc:
        logger.debug("_mark_stream failed: %s", exc)


def _verify_cancel_ownership(thread_id: str, document_id: int, effective_req_id: str | None) -> None:
    from app.database_pdf import PdfSessionLocal
    from app.models.pdf_models import PdfConversation

    with PdfSessionLocal() as db:
        conv = (
            db.query(PdfConversation.document_id, PdfConversation.user_id)
            .filter_by(thread_id=thread_id)
            .first()
        )
    if not conv:
        raise HTTPException(status_code=404, detail="對話紀錄不存在。")
    if conv.document_id is not None and conv.document_id != document_id:
        raise HTTPException(status_code=403, detail="此對話紀錄不屬於本論文。")
    if conv.user_id != effective_req_id:
        raise HTTPException(status_code=403, detail="無法取消他人的對話。")


# ── 非串流 chat（向下相容原有 schema）────────────────────────────────────────────

# require_live_ai 放在 _require_pdf_chat 前面：受限帳號一律拿到 403 LIVE_AI_RESTRICTED，而不是 503
@router.post(
    "/{project_id}/chat",
    response_model=ChatResponse,
    dependencies=[Depends(require_live_ai), Depends(_require_pdf_chat)],
)
async def chat_with_project(
    project_id: str,
    request: ChatRequest,
    http_request: Request,
    user: AuthUser = Depends(get_current_user),
):
    from app.utils import new_id
    from app.agents.pdf.router_agent import route_agent_stream
    from app.agents.pdf.request_context import set_user_id
    from app.services.pdf_quota_service import check_and_reserve, exit_reservation
    from app.services.pdf_llm_gate import acquire_with_timeout, get_gate

    document_id = await asyncio.to_thread(_get_document_id_for_project, project_id)
    if document_id is None:
        raise HTTPException(status_code=409, detail="此論文的 PDF 尚未完成索引，請稍後再試。")
    thread_id = request.thread_id or new_id()
    user_id = user.user_id
    owner_id = user_id
    set_user_id(owner_id)
    _quota_id = await check_and_reserve(user_id)

    gate_acquired = False
    full_response = ""
    try:
        conv = await asyncio.to_thread(_get_or_create_pdf_conversation, thread_id, owner_id, document_id)
        gate_acquired = await acquire_with_timeout(timeout=30.0)
        if not gate_acquired:
            raise HTTPException(status_code=503, detail="服務目前繁忙，請稍後再試。")
        async for token, is_done, _ in route_agent_stream(
            request.message,
            thread_id=thread_id,
            document_id=document_id,
            previous_agent_name=conv.last_agent_name,
        ):
            if is_done == "replace":
                full_response = token
            elif not is_done:
                full_response += token
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("chat_with_project error: %s", exc)
        full_response = "抱歉，處理您的問題時發生錯誤，請稍後再試。"
    finally:
        if gate_acquired:
            get_gate().release()
        exit_reservation(_quota_id)

    return ChatResponse(reply=full_response)


# ── SSE 串流 chat（前端主要使用）──────────────────────────────────────────────────

@router.post("/{project_id}/chat/stream", dependencies=[Depends(require_live_ai), Depends(_require_pdf_chat)])
async def chat_with_project_stream(
    project_id: str,
    request: ChatRequest,
    http_request: Request,
    user: AuthUser = Depends(get_current_user),
):
    from app.utils import new_id
    from app.agents.pdf import chat_jobs, steering
    from app.agents.pdf.router_agent import route_agent_stream
    from app.agents.pdf.request_context import set_user_id
    from app.services.pdf_quota_service import check_and_reserve, exit_reservation

    document_id = await asyncio.to_thread(_get_document_id_for_project, project_id)
    if document_id is None:
        raise HTTPException(status_code=409, detail="此論文的 PDF 尚未完成索引，請稍後再試。")
    thread_id = request.thread_id or new_id()
    user_id = user.user_id
    owner_id = user_id
    set_user_id(owner_id)
    _quota_id = await check_and_reserve(user_id)

    # Per-user stream lock: if same user+project already has an active stream,
    # silently drop the duplicate request so the frontend only sees one response.
    # Mutex makes the check-and-set atomic within a single asyncio worker.
    # Authenticated users are keyed by user_id; anonymous by stable anon_id (or thread_id fallback).
    lock_key = f"{project_id}:{user_id or anon_id or thread_id}"
    async with _stream_lock_mutex:
        if lock_key in _stream_lock:
            _active = _stream_lock[lock_key]
            if request.thread_id and request.thread_id == _active:
                # Client sent back the in-flight thread_id → steering, not a duplicate.
                steering.set(_active, request.message)
                logger.info("steering accepted project=%s thread=%s", project_id, _active)
                exit_reservation(_quota_id)

                async def _steer():
                    yield f"data: {_json.dumps({'token': '已收到補充，將納入考量。'})}\n\n"
                    yield f"data: {_json.dumps({'done': True, 'session_id': _active})}\n\n"

                return StreamingResponse(_steer(), media_type="text/event-stream")

            logger.warning("duplicate stream dropped project=%s user=%s active_thread=%s",
                           project_id, user_id, _active)
            exit_reservation(_quota_id)

            async def _drop():
                yield f"data: {_json.dumps({'done': True, 'session_id': _active})}\n\n"

            return StreamingResponse(_drop(), media_type="text/event-stream")

        _stream_lock[lock_key] = thread_id

    logger.info("stream request project=%s thread=%s has_thread_in_req=%s",
                project_id, thread_id, bool(request.thread_id))

    try:
        conv = await asyncio.to_thread(_get_or_create_pdf_conversation, thread_id, owner_id, document_id)
    except Exception:
        _stream_lock.pop(lock_key, None)
        exit_reservation(_quota_id)
        raise

    # Mid-run steering：同一 thread 在串流中補送訊息
    if conv.stream_started_at and _is_within_ttl(conv.stream_started_at):
        steering.set(thread_id, request.message)
        _stream_lock.pop(lock_key, None)
        exit_reservation(_quota_id)  # steering doesn't consume this slot

        async def _ack():
            yield f"data: {_json.dumps({'token': '已收到補充，將納入考量。', 'done': True, 'session_id': thread_id})}\n\n"

        return StreamingResponse(_ack(), media_type="text/event-stream")

    await asyncio.to_thread(_mark_stream, thread_id, datetime.now(timezone.utc))
    chat_jobs.start(thread_id, request.message, user_id=user_id, title=conv.title)

    async def event_stream():
        from app.services.pdf_llm_gate import acquire_with_timeout, get_gate

        # gate_acquired and pending must be initialised before try so finally can reference them
        # regardless of which await is cancelled by a client disconnect.
        gate_acquired = False
        pending: asyncio.Task | None = None
        try:
            # Send session_id BEFORE waiting for the gate so the frontend can display
            # the cancel button during the up-to-30s queue wait.
            yield f"data: {_json.dumps({'session_id': thread_id})}\n\n"

            gate_acquired = await acquire_with_timeout(timeout=30.0)
            if not gate_acquired:
                yield f"data: {_json.dumps({'error': '服務目前繁忙，請稍後再試。', 'done': True, 'session_id': thread_id})}\n\n"
                return

            if chat_jobs.is_cancelled(thread_id):
                yield f"data: {_json.dumps({'done': True, 'session_id': thread_id, 'cancelled': True})}\n\n"
                return

            _meta_queue: asyncio.Queue = asyncio.Queue()

            def _on_stage(msg: str) -> None:
                _meta_queue.put_nowait(("stage", msg))

            def _on_route(route) -> None:
                label = _ROUTE_LABELS.get(route.agent_name, route.agent_name)
                _meta_queue.put_nowait(("agent_start", route.agent_name, label))

            aiter = route_agent_stream(
                request.message,
                thread_id=thread_id,
                document_id=document_id,
                previous_agent_name=conv.last_agent_name,
                on_stage=_on_stage,
                on_route=_on_route,
            ).__aiter__()

            pending = asyncio.create_task(aiter.__anext__())
            while True:
                try:
                    result = await asyncio.wait_for(asyncio.shield(pending), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    # drain 再 heartbeat，讓前端更快收到 stage 事件
                    while not _meta_queue.empty():
                        meta = _meta_queue.get_nowait()
                        if meta[0] == "stage":
                            yield f"data: {_json.dumps({'type': 'stage', 'text': meta[1]})}\n\n"
                        elif meta[0] == "agent_start":
                            yield f"data: {_json.dumps({'type': 'agent_start', 'agent': meta[1], 'label': meta[2]})}\n\n"
                        else:
                            logger.debug("unknown meta event type: %s", meta[0])
                    yield f"data: {_json.dumps({'heartbeat': True})}\n\n"
                    continue
                except StopAsyncIteration:
                    break

                token, is_done, sources = result
                while not _meta_queue.empty():
                    meta = _meta_queue.get_nowait()
                    if meta[0] == "stage":
                        yield f"data: {_json.dumps({'type': 'stage', 'text': meta[1]})}\n\n"
                    elif meta[0] == "agent_start":
                        yield f"data: {_json.dumps({'type': 'agent_start', 'agent': meta[1], 'label': meta[2]})}\n\n"
                    else:
                        logger.debug("unknown meta event type: %s", meta[0])
                if is_done == "replace":
                    yield f"data: {_json.dumps({'replace': token, 'sources': sources})}\n\n"
                elif is_done:
                    yield f"data: {_json.dumps({'done': True, 'session_id': thread_id, 'sources': sources, 'cancelled': chat_jobs.is_cancelled(thread_id)})}\n\n"
                    break
                else:
                    yield f"data: {_json.dumps({'token': token})}\n\n"

                pending = asyncio.create_task(aiter.__anext__())

        except Exception as exc:
            logger.error("event_stream error thread=%s: %s", thread_id, exc)
            yield f"data: {_json.dumps({'error': '處理時發生錯誤，請稍後再試。', 'done': True, 'session_id': thread_id})}\n\n"
        finally:
            if pending and not pending.done():
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                except Exception as _drain_exc:
                    logger.debug("event_stream: pending drain error: %s", _drain_exc)
            if gate_acquired:
                get_gate().release()
            chat_jobs.finish(thread_id)
            await asyncio.to_thread(_mark_stream, thread_id, None)
            _stream_lock.pop(lock_key, None)
            exit_reservation(_quota_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 取消串流 ────────────────────────────────────────────────────────────────────

@router.post("/{project_id}/chat/{thread_id}/cancel", dependencies=[Depends(_require_pdf_chat)])
async def cancel_project_chat(
    project_id: str,
    thread_id: str,
    http_request: Request,
    user: Optional[AuthUser] = Depends(get_optional_user),
):
    from app.agents.pdf import chat_jobs

    document_id = await asyncio.to_thread(_get_document_id_for_project, project_id)
    if document_id is None:
        raise HTTPException(status_code=409, detail="此論文的 PDF 尚未完成索引。")

    req_user_id = user.user_id if user else None
    _raw_anon = http_request.headers.get("X-Anon-Session", "")
    req_anon_id = _raw_anon if (not req_user_id and _UUID4_RE.match(_raw_anon)) else None
    effective_req_id = req_user_id or (f"anon:{req_anon_id}" if req_anon_id else None)

    try:
        await asyncio.to_thread(_verify_cancel_ownership, thread_id, document_id, effective_req_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("cancel ownership check failed: %s", exc)
        raise HTTPException(status_code=503, detail="暫時無法驗證對話擁有者，請稍後再試。")

    chat_jobs.request_cancel(thread_id)
    return {"ok": True}


# ── 對話管理 ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}/conversations", dependencies=[Depends(_require_pdf_chat)])
async def list_conversations(
    project_id: str,
    user: AuthUser = Depends(get_current_user),
):
    document_id = await asyncio.to_thread(_get_document_id_for_project, project_id)
    if document_id is None:
        return []

    from app.database_pdf import PdfSessionLocal
    from app.models.pdf_models import PdfConversation

    def _load():
        with PdfSessionLocal() as db:
            return (
                db.query(PdfConversation)
                .filter_by(document_id=document_id, user_id=user.user_id)
                .order_by(PdfConversation.id.desc())
                .all()
            )

    convs = await asyncio.to_thread(_load)
    return [
        {
            "thread_id": c.thread_id,
            "title": c.title or "未命名對話",
            "message_count": c.message_count or 0,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in convs
    ]


@router.get("/{project_id}/conversations/{thread_id}/messages", dependencies=[Depends(_require_pdf_chat)])
async def get_conversation_messages(
    project_id: str,
    thread_id: str,
    user: AuthUser = Depends(get_current_user),
):
    document_id = await asyncio.to_thread(_get_document_id_for_project, project_id)
    if document_id is None:
        raise HTTPException(status_code=404, detail="找不到此論文。")

    from app.database_pdf import PdfSessionLocal
    from app.models.pdf_models import PdfConversation, PdfAgentMessage

    def _load():
        with PdfSessionLocal() as db:
            conv = db.query(PdfConversation).filter_by(thread_id=thread_id).first()
            if not conv:
                return "not_found", None
            if conv.user_id != user.user_id:
                return "forbidden", None
            if conv.document_id != document_id:
                return "wrong_doc", None
            rows = (
                db.query(PdfAgentMessage)
                .filter_by(thread_id=thread_id)
                .order_by(PdfAgentMessage.created_at)
                .all()
            )
            return "ok", [(m.user_question, m.agent_answer, m.sources) for m in rows]

    status, data = await asyncio.to_thread(_load)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="對話不存在。")
    if status in ("forbidden", "wrong_doc"):
        raise HTTPException(status_code=403, detail="無法存取此對話。")

    result = []
    for user_q, agent_a, sources in data:
        if user_q:
            result.append({"role": "user", "content": user_q})
        if agent_a:
            result.append({"role": "assistant", "content": agent_a, "sources": sources or []})
    return result


@router.delete("/{project_id}/conversations/{thread_id}", dependencies=[Depends(_require_pdf_chat)])
async def delete_conversation(
    project_id: str,
    thread_id: str,
    user: AuthUser = Depends(get_current_user),
):
    document_id = await asyncio.to_thread(_get_document_id_for_project, project_id)
    if document_id is None:
        raise HTTPException(status_code=404, detail="找不到此論文。")

    from app.database_pdf import PdfSessionLocal
    from app.models.pdf_models import PdfConversation, PdfAgentMessage

    def _delete():
        with PdfSessionLocal() as db:
            conv = db.query(PdfConversation).filter_by(thread_id=thread_id).first()
            if not conv:
                return "not_found"
            if conv.user_id != user.user_id:
                return "forbidden"
            if conv.document_id != document_id:
                return "wrong_doc"
            db.query(PdfAgentMessage).filter_by(thread_id=thread_id).delete()
            db.delete(conv)
            db.commit()
            return "ok"

    result = await asyncio.to_thread(_delete)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="對話不存在。")
    if result in ("forbidden", "wrong_doc"):
        raise HTTPException(status_code=403, detail="無法刪除此對話。")
    return {"ok": True}
