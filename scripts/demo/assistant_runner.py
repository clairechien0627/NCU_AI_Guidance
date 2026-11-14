"""
assistant_runner.py — 在同一個 process 內啟動後端，對兩個 AI 助理發問並記錄完整回應

給 Demo 案例錄製（record_demo_cases.py）與實證評估（scripts/evaluation/run_eval.py）共用。

做法：直接 import `app.main.app`，用 FastAPI dependency override 換掉 Firebase 登入
（也略過 LIVE_AI_ACCESS 的即時 AI gate，讓錄製帳號在 developers 模式下也能發問），
再透過 TestClient 打「真正的」SSE endpoint（/api/chat/stream、/api/projects/{id}/chat/stream）。
因此錄下來的事件格式、工具呼叫、引用來源都與正式站台完全相同，不是另外拼湊的結果。

需求：與執行後端相同的環境變數（從專案根目錄的 `.env` 讀取：模型服務、向量資料庫、PostgreSQL…），
模型供應商沿用後端目前的設定，本模組不另外指定。這會真的呼叫模型、產生費用。

資料庫：錄製會寫入對話紀錄（chat_sessions、chat_turns、pdf_conversations、pdf_agent_messages 等，
user_id 為 `demo-recorder:local`），會影響監控與使用統計。請設定 `RECORDER_DATABASE_URL`
指向獨立的資料庫（例如另開一個測試用資料庫），本模組會用它取代 `DATABASE_URL`；
未設定時會直接停止。確定要寫入 `DATABASE_URL` 指向的資料庫時，需另外設定 `RECORDER_ALLOW_MAIN_DB=1`，
錄完後請依 user_id 清除這些紀錄。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

RECORDER_USER_ID = "demo-recorder:local"

# 研究助理 stream lock／steering 分支的固定回覆（backend/app/routes/projects.py）：
# 錄到這句代表請求被當成「補充訊息」而不是新的提問，不能當成回答
STEERING_ACK = "已收到補充，將納入考量。"


# ── SSE 解析（純函式，可單元測試）────────────────────────────────────────────

def parse_sse_lines(lines: Iterable[str]) -> list[dict]:
    """把 `data: {...}` 行轉成 dict 清單；忽略空行、註解行與解析失敗的行。"""
    events: list[dict] = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def build_course_turn(question: str, events: list[dict], latency_ms: int) -> dict:
    """課程助理（/api/chat/stream）的事件 → 一輪對話紀錄。"""
    tokens: list[str] = []
    steps: list[dict] = []
    done: dict = {}
    error = None
    for ev in events:
        kind = ev.get("type")
        if kind == "token":
            tokens.append(ev.get("text", ""))
        elif kind == "tool_start":
            steps.append({"kind": "tool", "tool": ev.get("tool"), "args": ev.get("args"), "status": "running"})
        elif kind == "tool_done":
            for step in reversed(steps):
                if step.get("kind") == "tool" and step.get("tool") == ev.get("tool") and step["status"] == "running":
                    step.update(status="done", count=ev.get("count"), courses_found=ev.get("courses_found", []))
                    break
            else:
                steps.append({"kind": "tool", "tool": ev.get("tool"), "status": "done", "count": ev.get("count"),
                              "courses_found": ev.get("courses_found", [])})
        elif kind == "verify_start":
            steps.append({"kind": "verify", "pool_size": ev.get("pool_size"), "status": "running"})
        elif kind == "verify_done":
            for step in reversed(steps):
                if step.get("kind") == "verify" and step["status"] == "running":
                    step.update(status="done", method=ev.get("method"),
                                selected=ev.get("selected", []), filtered_out=ev.get("filtered_out", []))
                    break
        elif kind == "done":
            done = ev
        elif kind == "error":
            error = ev.get("message") or "unknown error"
    answer = done.get("final_answer") or "".join(tokens)
    return {
        "user": question,
        "assistant": answer,
        "steps": steps,
        "tools_used": done.get("tools_used", []),
        "course_cards": done.get("course_cards", []),
        "debug_trace": done.get("debug_trace"),
        "session_id": done.get("session_id"),
        "model": done.get("model", ""),
        "input_tokens": done.get("input_tokens", 0),
        "output_tokens": done.get("output_tokens", 0),
        "latency_ms": latency_ms,
        "error": error if error or done else "串流結束但沒有收到 done 事件",
    }


def build_research_turn(message: str, events: list[dict], latency_ms: int) -> dict:
    """研究助理（/api/projects/{id}/chat/stream）的事件 → 一輪對話紀錄。"""
    text = ""
    stages: list[str] = []
    agent = None
    sources: list = []
    thread_id = None
    error = None
    got_done = False
    for ev in events:
        if ev.get("heartbeat"):
            continue
        if ev.get("type") == "agent_start":
            agent = {"name": ev.get("agent"), "label": ev.get("label")}
            continue
        if ev.get("type") == "stage":
            if not stages or stages[-1] != ev.get("text"):
                stages.append(ev.get("text"))
            continue
        if ev.get("session_id"):
            thread_id = ev["session_id"]
        if ev.get("error"):
            error = ev["error"]
        if "replace" in ev:
            text = ev["replace"] or ""
            sources = ev.get("sources") or sources
        elif "token" in ev:
            text += ev["token"] or ""
        if ev.get("done"):
            got_done = True
            sources = ev.get("sources") or sources
            if ev.get("cancelled"):
                error = error or "cancelled"
    if not error and got_done:
        if text.strip() == STEERING_ACK:
            error = "收到 steering 確認訊息而非回答（同一 thread 仍有進行中的串流）"
        elif not text.strip():
            error = "沒有回答內容（請求可能被 stream lock 當成重複請求丟棄）"
    return {
        "user": message,
        "assistant": text,
        "agent": agent,
        "stages": stages,
        "sources": sources,
        "thread_id": thread_id,
        "latency_ms": latency_ms,
        "error": error if (error or got_done) else "串流結束但沒有收到 done 事件",
    }


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── 實際呼叫後端 ───────────────────────────────────────────────────────────────

class AssistantRunner:
    """with AssistantRunner() as r: r.ask_course(...) / r.ask_research(...)"""

    def __init__(self) -> None:
        self._client = None

    def __enter__(self) -> "AssistantRunner":
        sys.path.insert(0, str(BACKEND))
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        recorder_db = os.getenv("RECORDER_DATABASE_URL", "").strip()
        if recorder_db:
            os.environ["DATABASE_URL"] = recorder_db
            print("[runner] 使用 RECORDER_DATABASE_URL 作為資料庫")
        elif os.getenv("RECORDER_ALLOW_MAIN_DB", "").strip().lower() in {"1", "true", "yes"}:
            print(f"[runner] 注意：將寫入 DATABASE_URL 指向的資料庫（user_id={RECORDER_USER_ID}）")
        else:
            raise SystemExit(
                "[runner] 未設定 RECORDER_DATABASE_URL：錄製會把對話紀錄寫進 DATABASE_URL 指向的資料庫。"
                "請設定 RECORDER_DATABASE_URL 指向測試資料庫；"
                "確定要寫入 DATABASE_URL 時，請另外設定 RECORDER_ALLOW_MAIN_DB=1。"
            )

        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.access_control import require_live_ai
        from app.services.auth_service import AuthUser, get_current_user, get_optional_user

        recorder = AuthUser(user_id=RECORDER_USER_ID, email="demo-recorder@localhost",
                            name="Demo Recorder", picture="")
        app.dependency_overrides[get_current_user] = lambda: recorder
        app.dependency_overrides[get_optional_user] = lambda: recorder
        # LIVE_AI_ACCESS=developers 時，require_live_ai 只放行 DEVELOPER_EMAILS 內、email 已驗證的帳號，
        # 且它會自己解析 token（不經過上面 get_optional_user 的 override），錄製帳號會被擋成 403。
        # 錄製／評估是開發者在本機主動執行的離線工具，沿用 .env 的 LIVE_AI_ACCESS 不該讓它失敗，
        # 所以直接覆寫這個 gate；正式站台的請求不會經過這裡。
        app.dependency_overrides[require_live_ai] = lambda: None
        self._client = TestClient(app, raise_server_exceptions=False)
        self._client.__enter__()  # 跑 lifespan：checkpointer、RAG 預熱
        ready = self._client.get("/ready")
        print(f"[runner] /ready → {ready.status_code} {ready.text[:200]}")
        return self

    def __exit__(self, *exc) -> None:
        if self._client is not None:
            self._client.__exit__(*exc)

    def _stream(self, url: str, body: dict) -> tuple[list[dict], int]:
        assert self._client is not None, "請在 with AssistantRunner() 區塊內使用"
        t0 = time.perf_counter()
        with self._client.stream("POST", url, json=body, timeout=900) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"{url} → HTTP {resp.status_code}: {resp.read().decode('utf-8', 'replace')[:500]}")
            events = parse_sse_lines(resp.iter_lines())
        return events, int((time.perf_counter() - t0) * 1000)

    def ask_course(self, question: str, *, session_id: str | None = None,
                   college: str | None = None, dept: str | None = None) -> dict:
        body: dict[str, Any] = {"question": question, "session_id": session_id, "college": college, "dept": dept}
        events, ms = self._stream("/api/chat/stream", body)
        return build_course_turn(question, events, ms)

    def ask_research(self, project_id: str, message: str, *, thread_id: str | None = None) -> dict:
        events, ms = self._stream(f"/api/projects/{project_id}/chat/stream",
                                  {"message": message, "thread_id": thread_id})
        return build_research_turn(message, events, ms)

    def run_conversation(self, assistant: str, messages: list[str], *, project_id: str | None = None,
                         college: str | None = None, dept: str | None = None) -> list[dict]:
        """多輪對話：沿用同一個 session／thread。"""
        turns: list[dict] = []
        session = None
        for msg in messages:
            if assistant == "course":
                turn = self.ask_course(msg, session_id=session, college=college, dept=dept)
                session = turn.get("session_id") or session
            elif assistant == "research":
                if not project_id:
                    raise ValueError("research 助理需要 project_id")
                turn = self.ask_research(project_id, msg, thread_id=session)
                session = turn.get("thread_id") or session
            else:
                raise ValueError(f"未知的 assistant：{assistant}")
            turns.append(turn)
            if turn.get("error"):
                break
        return turns
