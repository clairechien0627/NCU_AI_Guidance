"""LIVE_AI_ACCESS 開關（即時 AI 只開放給開發者）的測試。

全部不跑 lifespan、不連資料庫／Qdrant，下游的模型呼叫一律 mock 掉，不會燒任何 token。

驗證：
(a) mode=all（預設）時所有端點行為與加入開關前相同，不需要開發者身分；
(b) mode=developers 時，未登入、非開發者呼叫對話／推理型 LLM 端點 → 403 LIVE_AI_RESTRICTED；
    開發者（email 大小寫／空白變化）可以通過 gate；
(c) 路由掃描：每一條路由都必須明確歸類為「掛 require_live_ai」或「不呼叫對話 LLM」，
    新增路由沒有歸類時測試會失敗；
(d) GET /api/ai-access 在各模式、各身分的回傳；
(e) 不認得的 LIVE_AI_ACCESS 值會讓啟動失敗（LIVE_AI_ACCESS_INVALID），請求時也不會悄悄放行；
(f) mode=all 時 gate 與 /api/ai-access 完全不解析 token（無效 Bearer／cookie 不會變成 401）；
(g) 開發者還必須是 Firebase 已驗證的 email（email_verified=True），未驗證一律不算開發者。
"""
import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402
from starlette.routing import Mount, WebSocketRoute  # noqa: E402

from app import app_state  # noqa: E402
from app.main import app, lifespan  # noqa: E402
from app.routes import monitor  # noqa: E402
from app.services import access_control  # noqa: E402
from app.services.access_control import require_live_ai  # noqa: E402
from app.services import auth_service  # noqa: E402
from app.services.auth_service import AuthUser, get_optional_user  # noqa: E402

DEVELOPER_EMAILS = " dev@example.com ,Second.Dev@Example.com,, "
DEV_USER = AuthUser(user_id="firebase:dev", email="  DEV@Example.COM ", name="Dev", picture="",
                    email_verified=True)
DEV_USER_2 = AuthUser(user_id="firebase:dev2", email="second.dev@example.com", name="Dev2", picture="",
                      email_verified=True)
OTHER_USER = AuthUser(user_id="firebase:guest", email="guest@example.com", name="Guest", picture="",
                      email_verified=True)
# email 在名單內，但 Firebase 尚未驗證這個 email（email_verified 預設 False）
UNVERIFIED_DEV_USER = AuthUser(user_id="firebase:unverified", email="dev@example.com", name="Fake", picture="")
INVALID_TOKEN_HEADERS = (
    ("bearer", {"Authorization": "Bearer not-a-valid-token"}),
    ("cookie", {"Cookie": "firebase_id_token=not-a-valid-token"}),
)

# ── 路由分類表 ────────────────────────────────────────────────────────────────
# 會呼叫對話／推理型 LLM、必須掛 require_live_ai 的端點
LIVE_AI_ROUTES = {
    ("POST", "/api/chat"): "課程助理：llm_service.generate_with_tools",
    ("POST", "/api/chat/stream"): "課程助理串流：llm_service.stream_with_tools",
    ("POST", "/api/courses/{course_id}/ask"): "單課問答：course_info_service.answer_course_question",
    ("POST", "/api/projects/{project_id}/chat"): "PDF 問答：router_agent.route_agent_stream",
    ("POST", "/api/projects/{project_id}/chat/stream"): "PDF 問答串流：router_agent.route_agent_stream",
}

# 已確認不呼叫對話／推理型 LLM、不掛 gate 的端點（每項附理由）
NON_LLM_ROUTES = {
    ("GET", "/"): "回傳靜態 dict",
    ("GET", "/health"): "回傳靜態 dict",
    ("GET", "/ready"): "只讀 app_state 旗標",
    ("GET", "/api/ai-access"): "只回報存取模式",
    ("GET", "/api/assessment/questions"): "讀本地題目 JSON",
    ("POST", "/api/assessment/submit"): "本地規則計分",
    ("GET", "/api/assessment/admission-criteria"): "讀本地資料",
    ("POST", "/api/assessment/filter-by-subjects"): "本地資料篩選",
    ("GET", "/api/auth/me"): "回傳已驗證的使用者欄位",
    ("GET", "/api/courses"): "讀本地課程 JSON",
    ("POST", "/api/courses/semantic-search"): "只呼叫 embedding（使用者決定對所有人開放）",
    ("GET", "/api/courses/{course_id}"): "讀本地課程 JSON",
    ("POST", "/api/course-search"): "本地關鍵字比對",
    ("GET", "/api/projects"): "讀本地 projects.json",
    ("GET", "/api/projects/analytics"): "DB 統計",
    ("GET", "/api/projects/recent-chats"): "DB 查詢",
    ("GET", "/api/projects/{project_id}"): "讀本地 projects.json",
    ("POST", "/api/projects/{project_id}/chat/{thread_id}/cancel"): "只設定記憶體取消旗標",
    ("GET", "/api/projects/{project_id}/conversations"): "DB 查詢",
    ("GET", "/api/projects/{project_id}/conversations/{thread_id}/messages"): "DB 查詢",
    ("DELETE", "/api/projects/{project_id}/conversations/{thread_id}"): "DB 刪除",
    ("GET", "/api/graph/search"): "讀本地圖譜 JSON",
    ("GET", "/api/graph/neighborhood"): "讀本地圖譜 JSON",
    ("GET", "/api/graph/stats"): "讀本地圖譜 JSON",
    ("POST", "/api/chat/course_detail"): "查本地 course_index.json",
    ("GET", "/api/chat/sessions"): "DB 查詢",
    ("GET", "/api/chat/session/{session_id}"): "DB 查詢",
    ("DELETE", "/api/chat/session/{session_id}"): "DB 刪除",
    ("GET", "/api/chat/analytics"): "DB 統計＋Qdrant payload 查詢（不 embed）",
    ("GET", "/api/chat/tools"): "回傳靜態工具定義",
    ("GET", "/api/curriculum/tree"): "讀本地 JSON",
    ("GET", "/api/curriculum/dept/{dept_id}"): "讀本地 JSON",
    ("GET", "/api/curriculum/notes"): "讀本地 JSON",
    ("GET", "/api/curriculum/notes/by-id/{dept_id}"): "讀本地 JSON",
    ("GET", "/api/curriculum/pdf/{dept_id}"): "回傳本地 PDF 檔",
    ("GET", "/api/monitor/db-stats"): "開發者專用 DB 統計",
    ("GET", "/api/monitor/stats"): "開發者專用統計",
}

# FastAPI 內建的文件路由（不是 APIRoute）
BUILTIN_DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

GENERIC_BODY = {"question": "hi", "message": "hi"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _api_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield method, route.path, route


def _has_require_live_ai(dependant) -> bool:
    for sub in dependant.dependencies:
        if sub.call is require_live_ai or _has_require_live_ai(sub):
            return True
    return False


class _EnvMixin:
    """每個測試都在乾淨的 LIVE_AI_ACCESS / DEVELOPER_EMAILS 環境下執行。"""

    def set_env(self, mode: str | None, developers: str | None = DEVELOPER_EMAILS):
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("LIVE_AI_ACCESS", "DEVELOPER_EMAILS"):
            os.environ.pop(key, None)
        if mode is not None:
            os.environ["LIVE_AI_ACCESS"] = mode
        if developers is not None:
            os.environ["DEVELOPER_EMAILS"] = developers

    def as_user(self, user: AuthUser | None):
        # 端點自己的登入依賴（get_current_user／get_optional_user）走 dependency override；
        # require_live_ai 與 /api/ai-access 只在 mode=developers 時才直接呼叫 get_optional_user
        # 解析 token（mode=all 完全不解析），所以另外 patch 這兩個模組裡的名稱。
        app.dependency_overrides[get_optional_user] = lambda: user
        self.addCleanup(app.dependency_overrides.pop, get_optional_user, None)
        for target in ("app.services.access_control.get_optional_user", "app.routes.ai_access.get_optional_user"):
            patcher = mock.patch(target, side_effect=lambda request, credentials: user)
            patcher.start()
            self.addCleanup(patcher.stop)

    def reject_any_token(self):
        """讓任何 token 驗證都回 401（模擬過期／無效 token）；回傳 mock 以確認有沒有被呼叫。"""
        patcher = mock.patch.object(
            auth_service, "verify_firebase_token",
            side_effect=HTTPException(status_code=401, detail="Invalid Firebase auth token"),
        )
        verify = patcher.start()
        self.addCleanup(patcher.stop)
        return verify


class _ClientMixin(_EnvMixin):
    def setUp(self):
        # 不用 with：不觸發 lifespan（不連 DB／Qdrant、不預熱 RAG）
        self.client = TestClient(app, raise_server_exceptions=False)
        # 安全網：下游模型呼叫一律 mock，測試若意外穿過 gate 也不會真的打模型
        self.answer = mock.patch(
            "app.routes.courses.answer_course_question",
            return_value={"answer": "mock answer", "model": "mock", "warnings": []},
        ).start()
        self.generate = mock.patch(
            "app.routes.chat.llm.generate_with_tools", side_effect=AssertionError("不應呼叫 LLM")
        ).start()
        self.stream = mock.patch(
            "app.routes.chat.llm.stream_with_tools", side_effect=AssertionError("不應呼叫 LLM")
        ).start()
        # PDF 問答：讓端點在呼叫 agent 之前就以 409（PDF 未索引）結束
        self.doc_id = mock.patch(
            "app.routes.projects._get_document_id_for_project", return_value=None
        ).start()
        self.addCleanup(mock.patch.stopall)
        ready = app_state.pdf_chat_ready
        app_state.pdf_chat_ready = True
        self.addCleanup(setattr, app_state, "pdf_chat_ready", ready)

    def assert_restricted(self, resp):
        self.assertEqual(resp.status_code, 403, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "LIVE_AI_RESTRICTED")
        self.assertEqual(detail["message"], "即時 AI 問答目前只開放給開發團隊")

    def assert_not_restricted(self, resp):
        if resp.status_code == 403:
            detail = resp.json().get("detail")
            self.assertFalse(
                isinstance(detail, dict) and detail.get("code") == "LIVE_AI_RESTRICTED",
                f"不應被 LIVE_AI gate 擋下：{resp.text}",
            )

    def call_gated_endpoints(self, *, valid_question: bool):
        """依序呼叫五個 LLM 端點；valid_question=False 時用空白問題讓端點在模型呼叫前結束。"""
        q = "這門課在學什麼？" if valid_question else "   "
        return {
            "chat": self.client.post("/api/chat", json={"question": "   "}),
            "chat_stream": self.client.post("/api/chat/stream", json={"question": "   "}),
            "course_ask": self.client.post("/api/courses/CS101/ask", json={"question": q}),
            "pdf_chat": self.client.post("/api/projects/p1/chat", json={"message": "hi"}),
            "pdf_chat_stream": self.client.post("/api/projects/p1/chat/stream", json={"message": "hi"}),
        }


# ── access_control 單元測試 ──────────────────────────────────────────────────

class AccessControlUnitTests(_EnvMixin, unittest.TestCase):
    def test_default_mode_is_all(self):
        self.set_env(None)
        self.assertEqual(access_control.live_ai_mode(), "all")
        self.set_env("  ")
        self.assertEqual(access_control.live_ai_mode(), "all")

    def test_mode_is_case_and_space_insensitive(self):
        self.set_env("  Developers ")
        self.assertEqual(access_control.live_ai_mode(), "developers")
        self.set_env("ALL")
        self.assertEqual(access_control.live_ai_mode(), "all")

    def test_unknown_mode_raises(self):
        for bad in ("developer", "dev", "none", "true", "off"):
            self.set_env(bad)
            with self.assertRaises(RuntimeError) as ctx:
                access_control.live_ai_mode()
            self.assertIn("LIVE_AI_ACCESS_INVALID", str(ctx.exception))

    def test_is_developer_email_normalizes(self):
        self.set_env("developers")
        self.assertTrue(access_control.is_developer_email("dev@example.com"))
        self.assertTrue(access_control.is_developer_email("  DEV@EXAMPLE.COM  "))
        self.assertTrue(access_control.is_developer_email("second.dev@example.com"))
        self.assertFalse(access_control.is_developer_email("guest@example.com"))
        self.assertFalse(access_control.is_developer_email(""))
        self.assertFalse(access_control.is_developer_email(None))

    def test_no_developer_emails_means_nobody_is_developer(self):
        self.set_env("developers", developers=None)
        self.assertEqual(access_control.developer_emails(), frozenset())
        self.assertFalse(access_control.is_developer_email("dev@example.com"))
        self.set_env("developers", developers=" , ,")
        self.assertFalse(access_control.is_developer_email(""))

    def test_live_ai_allowed_matrix(self):
        self.set_env("all")
        for user in (None, OTHER_USER, DEV_USER):
            self.assertTrue(access_control.live_ai_allowed(user))
        self.set_env("developers")
        self.assertFalse(access_control.live_ai_allowed(None))
        self.assertFalse(access_control.live_ai_allowed(OTHER_USER))
        self.assertTrue(access_control.live_ai_allowed(DEV_USER))
        self.assertTrue(access_control.live_ai_allowed(DEV_USER_2))
        self.assertFalse(access_control.live_ai_allowed(UNVERIFIED_DEV_USER))

    def test_is_developer_requires_verified_email(self):
        self.set_env("developers")
        self.assertTrue(access_control.is_developer(DEV_USER))
        self.assertFalse(access_control.is_developer(UNVERIFIED_DEV_USER))
        self.assertFalse(access_control.is_developer(OTHER_USER))
        self.assertFalse(access_control.is_developer(None))

    def _gate(self, user):
        """直接呼叫 require_live_ai；回傳 get_optional_user 的 mock 以確認有沒有解析 token。"""
        with mock.patch.object(access_control, "get_optional_user", return_value=user) as resolver:
            require_live_ai(request=mock.sentinel.request, credentials=None)
        return resolver

    def test_require_live_ai_dependency(self):
        self.set_env("developers")
        for user in (None, OTHER_USER, UNVERIFIED_DEV_USER):
            with self.subTest(user=user and user.user_id):
                with self.assertRaises(HTTPException) as ctx:
                    self._gate(user)
                self.assertEqual(ctx.exception.status_code, 403)
                self.assertEqual(ctx.exception.detail["code"], "LIVE_AI_RESTRICTED")
        self._gate(DEV_USER).assert_called_once_with(mock.sentinel.request, None)

    def test_require_live_ai_does_not_resolve_token_in_all_mode(self):
        for mode in (None, "all"):
            self.set_env(mode)
            self._gate(None).assert_not_called()

    def test_verify_firebase_token_reads_email_verified(self):
        cases = [({"email_verified": True}, True), ({"email_verified": False}, False),
                 ({}, False), ({"email_verified": "true"}, False)]
        for extra, expected in cases:
            decoded = {"uid": "u1", "email": "dev@example.com", **extra}
            with self.subTest(decoded=extra), \
                    mock.patch.object(auth_service, "_ensure_firebase_app"), \
                    mock.patch.object(auth_service.auth, "verify_id_token", return_value=decoded):
                user = auth_service.verify_firebase_token("token")
            self.assertIs(user.email_verified, expected)
            self.assertEqual(user.email, "dev@example.com")


class MonitorDeveloperCheckTests(_EnvMixin, unittest.TestCase):
    """monitor 改用 is_developer_email 後行為不變。"""

    def test_unset_developer_emails(self):
        self.set_env(None, developers=None)
        with self.assertRaises(HTTPException) as ctx:
            monitor._require_developer(DEV_USER)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "開發人員專用功能（DEVELOPER_EMAILS 未設定）")

    def test_non_developer(self):
        self.set_env(None)
        with self.assertRaises(HTTPException) as ctx:
            monitor._require_developer(OTHER_USER)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "開發人員專用功能")

    def test_developer_case_insensitive(self):
        self.set_env(None)
        self.assertIs(monitor._require_developer(DEV_USER), DEV_USER)
        self.assertIs(monitor._require_developer(DEV_USER_2), DEV_USER_2)

    def test_unverified_developer_email_is_rejected(self):
        self.set_env(None)
        with self.assertRaises(HTTPException) as ctx:
            monitor._require_developer(UNVERIFIED_DEV_USER)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "開發人員專用功能")


# ── 路由掃描 ─────────────────────────────────────────────────────────────────

class RouteClassificationTests(unittest.TestCase):
    def test_tables_do_not_overlap(self):
        self.assertFalse(set(LIVE_AI_ROUTES) & set(NON_LLM_ROUTES))
        for key, reason in {**LIVE_AI_ROUTES, **NON_LLM_ROUTES}.items():
            self.assertTrue(reason.strip(), f"{key} 缺少歸類理由")

    def test_every_route_is_classified(self):
        current = {(m, p) for m, p, _ in _api_routes()}
        self.assertTrue(current, "應該至少有一條 APIRoute")
        classified = set(LIVE_AI_ROUTES) | set(NON_LLM_ROUTES)
        unclassified = sorted(current - classified)
        self.assertFalse(
            unclassified,
            "有新路由尚未歸類；確認它會不會呼叫對話／推理型 LLM，"
            f"會的話掛 require_live_ai 並加入 LIVE_AI_ROUTES，否則加入 NON_LLM_ROUTES：{unclassified}",
        )
        stale = sorted(classified - current)
        self.assertFalse(stale, f"分類表裡有已不存在的路由，請移除：{stale}")

    def test_llm_routes_have_gate_and_others_do_not(self):
        missing, unexpected = [], []
        for method, path, route in _api_routes():
            gated = _has_require_live_ai(route.dependant)
            if (method, path) in LIVE_AI_ROUTES and not gated:
                missing.append((method, path))
            if (method, path) in NON_LLM_ROUTES and gated:
                unexpected.append((method, path))
        self.assertFalse(missing, f"這些 LLM 端點沒有掛 require_live_ai：{missing}")
        self.assertFalse(unexpected, f"這些端點被誤掛 require_live_ai（會擋到不燒 token 的功能）：{unexpected}")

    def test_no_unreviewed_mounts_websockets_or_plain_routes(self):
        mounts = {r.path for r in app.routes if isinstance(r, Mount)}
        self.assertLessEqual(mounts, {"/pdfs"}, f"出現未審查的 Mount：{mounts}")
        self.assertFalse([r for r in app.routes if isinstance(r, WebSocketRoute)], "出現未審查的 websocket 路由")
        plain = {
            getattr(r, "path", "")
            for r in app.routes
            if not isinstance(r, (APIRoute, Mount, WebSocketRoute))
        }
        self.assertLessEqual(plain, BUILTIN_DOC_PATHS, f"出現未審查的非 APIRoute 路由：{plain - BUILTIN_DOC_PATHS}")


# ── 端點行為 ─────────────────────────────────────────────────────────────────

class ModeAllUnchangedTests(_ClientMixin, unittest.TestCase):
    """mode=all（含未設定）時行為與加入開關前相同。"""

    def _check_unchanged(self, mode):
        self.set_env(mode)

        # 未登入：原本需要登入的端點仍是 401；單課問答原本就公開，照樣可以問
        self.as_user(None)
        resp = self.call_gated_endpoints(valid_question=True)
        for name in ("chat", "chat_stream", "pdf_chat", "pdf_chat_stream"):
            self.assertEqual(resp[name].status_code, 401, f"{name}: {resp[name].text}")
        self.assertEqual(resp["course_ask"].status_code, 200, resp["course_ask"].text)
        self.assertEqual(resp["course_ask"].json()["answer"], "mock answer")

        # 非開發者登入：全部通過 gate，由端點本身的邏輯回應
        self.as_user(OTHER_USER)
        resp = self.call_gated_endpoints(valid_question=False)
        self.assertEqual(resp["chat"].status_code, 400, resp["chat"].text)
        self.assertEqual(resp["chat_stream"].status_code, 400, resp["chat_stream"].text)
        self.assertEqual(resp["course_ask"].status_code, 400, resp["course_ask"].text)
        self.assertEqual(resp["pdf_chat"].status_code, 409, resp["pdf_chat"].text)
        self.assertEqual(resp["pdf_chat_stream"].status_code, 409, resp["pdf_chat_stream"].text)
        self.generate.assert_not_called()
        self.stream.assert_not_called()

    def test_unset_mode(self):
        self._check_unchanged(None)

    def test_explicit_all(self):
        self._check_unchanged("all")

    def test_all_mode_ignores_missing_developer_list(self):
        self.set_env("all", developers=None)
        self.as_user(OTHER_USER)
        resp = self.client.post("/api/courses/CS101/ask", json={"question": "這門課在學什麼？"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_all_mode_ignores_invalid_token_on_public_course_ask(self):
        """單課問答原本就公開：mode=all 時無效 token 不能讓它變成 401，要照常進到 handler。"""
        verify = self.reject_any_token()
        for mode in (None, "all"):
            self.set_env(mode)
            for kind, headers in INVALID_TOKEN_HEADERS:
                with self.subTest(mode=mode, token=kind):
                    self.answer.reset_mock()
                    resp = self.client.post(
                        "/api/courses/CS101/ask", json={"question": "這門課在學什麼？"}, headers=headers
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(resp.json()["answer"], "mock answer")
                    self.answer.assert_called_once()
        verify.assert_not_called()

    def test_developers_mode_rejects_invalid_token(self):
        """對照組：mode=developers 才解析 token，無效 token 回 401，不會進到 handler。"""
        verify = self.reject_any_token()
        self.set_env("developers")
        for kind, headers in INVALID_TOKEN_HEADERS:
            with self.subTest(token=kind):
                resp = self.client.post(
                    "/api/courses/CS101/ask", json={"question": "這門課在學什麼？"}, headers=headers
                )
                self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(verify.call_count, len(INVALID_TOKEN_HEADERS))
        self.answer.assert_not_called()

    def test_non_llm_endpoint_unaffected_in_developers_mode(self):
        """不燒 token 的端點（這裡用不需外部服務的 course_detail）在 developers 模式下照常可用。"""
        self.set_env("developers")
        self.as_user(None)
        resp = self.client.post("/api/chat/course_detail", json={"name": "不存在的課", "code": ""})
        self.assert_not_restricted(resp)
        self.assertEqual(resp.status_code, 200, resp.text)


class ModeDevelopersTests(_ClientMixin, unittest.TestCase):
    def test_anonymous_is_restricted(self):
        self.set_env("developers")
        self.as_user(None)
        for name, resp in self.call_gated_endpoints(valid_question=True).items():
            with self.subTest(endpoint=name):
                self.assert_restricted(resp)
        self.answer.assert_not_called()
        self.doc_id.assert_not_called()

    def test_non_developer_is_restricted(self):
        self.set_env("developers")
        self.as_user(OTHER_USER)
        for name, resp in self.call_gated_endpoints(valid_question=True).items():
            with self.subTest(endpoint=name):
                self.assert_restricted(resp)
        self.answer.assert_not_called()
        self.doc_id.assert_not_called()

    def test_restricted_even_when_developer_list_is_empty(self):
        self.set_env("developers", developers=None)
        self.as_user(DEV_USER)
        for name, resp in self.call_gated_endpoints(valid_question=True).items():
            with self.subTest(endpoint=name):
                self.assert_restricted(resp)

    def test_unverified_developer_email_is_restricted(self):
        """email 在 DEVELOPER_EMAILS 名單內但 Firebase 未驗證 → 一樣 403。"""
        self.set_env("developers")
        self.as_user(UNVERIFIED_DEV_USER)
        for name, resp in self.call_gated_endpoints(valid_question=True).items():
            with self.subTest(endpoint=name):
                self.assert_restricted(resp)
        self.answer.assert_not_called()
        self.doc_id.assert_not_called()

    def test_restricted_before_pdf_chat_readiness_check(self):
        """PDF 問答服務未就緒時，受限帳號仍拿到 403 LIVE_AI_RESTRICTED（不是 503）。"""
        self.set_env("developers")
        self.as_user(OTHER_USER)
        app_state.pdf_chat_ready = False
        self.assert_restricted(self.client.post("/api/projects/p1/chat/stream", json={"message": "hi"}))

    def test_developer_passes_gate(self):
        self.set_env("developers")
        for user in (DEV_USER, DEV_USER_2):
            with self.subTest(email=user.email):
                self.as_user(user)
                resp = self.call_gated_endpoints(valid_question=False)
                self.assertEqual(resp["chat"].status_code, 400, resp["chat"].text)
                self.assertEqual(resp["chat_stream"].status_code, 400, resp["chat_stream"].text)
                self.assertEqual(resp["course_ask"].status_code, 400, resp["course_ask"].text)
                self.assertEqual(resp["pdf_chat"].status_code, 409, resp["pdf_chat"].text)
                self.assertEqual(resp["pdf_chat_stream"].status_code, 409, resp["pdf_chat_stream"].text)

                ok = self.client.post("/api/courses/CS101/ask", json={"question": "這門課在學什麼？"})
                self.assertEqual(ok.status_code, 200, ok.text)
                self.assertEqual(ok.json()["answer"], "mock answer")
        self.generate.assert_not_called()
        self.stream.assert_not_called()

    def test_every_classified_llm_route_is_blocked_dynamically(self):
        """逐一打 LIVE_AI_ROUTES 的每條路由，確認真的回 403（不只是依賴樹裡有 gate）。"""
        self.set_env("developers")
        self.as_user(OTHER_USER)
        for method, path in LIVE_AI_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.assert_restricted(self.client.request(method, _concrete(path), json=GENERIC_BODY))

    def test_restricted_response_keeps_cors_header(self):
        cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
        origins = cors[0].kwargs.get("allow_origins", []) if cors else []
        origin = next((o for o in origins if o != "*"), None)
        if not origin:
            self.skipTest("沒有設定可用的 CORS origin")
        self.set_env("developers")
        self.as_user(None)
        resp = self.client.post("/api/courses/CS101/ask", json={"question": "hi"}, headers={"Origin": origin})
        self.assert_restricted(resp)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), origin)


class InvalidModeTests(_ClientMixin, unittest.TestCase):
    def test_startup_fails_on_unknown_value(self):
        self.set_env("developer")  # 少一個 s 的常見錯字

        async def run():
            async with lifespan(app):
                pass

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(run())
        self.assertIn("LIVE_AI_ACCESS_INVALID", str(ctx.exception))

    def test_request_does_not_silently_allow(self):
        self.set_env("everyone")
        self.as_user(OTHER_USER)
        resp = self.client.post("/api/courses/CS101/ask", json={"question": "這門課在學什麼？"})
        self.assertEqual(resp.status_code, 500)
        self.answer.assert_not_called()
        self.assertEqual(self.client.get("/api/ai-access").status_code, 500)


class AiAccessEndpointTests(_ClientMixin, unittest.TestCase):
    def _get(self, user):
        self.as_user(user)
        resp = self.client.get("/api/ai-access")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("no-store", resp.headers.get("cache-control", ""))
        return resp.json()

    def test_all_mode(self):
        for mode in (None, "all"):
            self.set_env(mode)
            for user in (None, OTHER_USER, DEV_USER):
                with self.subTest(mode=mode, user=user and user.email):
                    self.assertEqual(self._get(user), {"mode": "all", "live_ai": True})

    def test_developers_mode(self):
        self.set_env("developers")
        self.assertEqual(self._get(None), {"mode": "developers", "live_ai": False})
        self.assertEqual(self._get(OTHER_USER), {"mode": "developers", "live_ai": False})
        self.assertEqual(self._get(DEV_USER), {"mode": "developers", "live_ai": True})
        self.assertEqual(self._get(DEV_USER_2), {"mode": "developers", "live_ai": True})
        self.assertEqual(self._get(UNVERIFIED_DEV_USER), {"mode": "developers", "live_ai": False})

    def test_all_mode_ignores_invalid_token(self):
        """mode=all 時不解析 token：無效 Bearer／cookie 也直接回 {mode: all, live_ai: true}，不回 401。"""
        verify = self.reject_any_token()
        for mode in (None, "all"):
            self.set_env(mode)
            for kind, headers in INVALID_TOKEN_HEADERS:
                with self.subTest(mode=mode, token=kind):
                    resp = self.client.get("/api/ai-access", headers=headers)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(resp.json(), {"mode": "all", "live_ai": True})
                    self.assertIn("no-store", resp.headers.get("cache-control", ""))
        verify.assert_not_called()

    def test_developers_mode_invalid_token_is_401(self):
        """對照組：mode=developers 才解析 token，無效 token 回 401。"""
        self.reject_any_token()
        self.set_env("developers")
        for kind, headers in INVALID_TOKEN_HEADERS:
            with self.subTest(token=kind):
                self.assertEqual(self.client.get("/api/ai-access", headers=headers).status_code, 401)

    def test_developers_mode_without_list(self):
        self.set_env("developers", developers=None)
        self.assertEqual(self._get(DEV_USER), {"mode": "developers", "live_ai": False})

    def test_anonymous_without_override(self):
        """不覆寫 get_optional_user、也不帶 token 時，未登入呼叫照樣回 200。"""
        self.set_env("developers")
        resp = self.client.get("/api/ai-access")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"mode": "developers", "live_ai": False})


if __name__ == "__main__":
    unittest.main()
