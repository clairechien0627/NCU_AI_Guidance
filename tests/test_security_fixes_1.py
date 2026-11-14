"""資安稽核第一組修補（event loop 阻塞與匿名可觸發的資源消耗）的回歸測試。

全部不跑 lifespan、不連資料庫／Qdrant／LLM，下游一律 mock，不會燒任何 token。

涵蓋：
(a) Request body 上限：超過上限在驗證前就回 413（有 Content-Length 與 chunked 兩種），上限內照常處理；
(b) 課程列表：課程 JSON 每個 level 只解析一次；完整清單回傳的 JSON 與 response_model 驗證結果一致；
    單課查詢保留「第一筆符合者」語意；
(c) 會做同步 I/O 的路由不再是 async def（交給 threadpool），LLM 等待期間 /health 仍能立即回應；
    課程助理串流的 ss.load 在 threadpool 執行；
(d) /api/chat/analytics：Qdrant 補查在寫入快取前完成，快取命中時不再查 Qdrant；
(e) PDF 文件 ID 快取不寫入不存在的 project_id。
"""
import asyncio
import inspect
import json
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app.body_limit import DEFAULT_MAX_BODY_BYTES, BodySizeLimitMiddleware  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import chat as chat_routes  # noqa: E402
from app.routes import courses as course_routes  # noqa: E402
from app.routes import projects as project_routes  # noqa: E402
from app.services import session_store as ss  # noqa: E402
from app.services.auth_service import AuthUser, get_current_user  # noqa: E402

USER = AuthUser(user_id="firebase:dummy", email="dummy@example.com", name="Dummy", picture="",
                email_verified=True)


def _fake_course(serial, course_id, name, college="工學院", dept="資工系", req="必修"):
    return {
        "serial_no": serial, "course_id": course_id, "course_name_zh": name, "course_name_en": name,
        "college": college, "department": dept, "instructor": "老師", "credits": 3,
        "required_elective": req, "semester_display": "上學期", "semester": "114_1",
        "full_half_year": "半", "extra_field_not_in_schema": "x",
    }


FAKE_COURSES = [
    _fake_course("1001", "CE1001-A", "計算機概論"),
    # serial_no 與第一筆的 course_id 相同，驗證「第一筆符合者」語意
    _fake_course("CE1001-A", "MA2001-A", "微積分", college="理學院", dept="數學系", req="選修"),
    _fake_course("1003", "CE1001-A", "計算機概論（重複班）"),
]


def _clear_course_caches():
    course_routes._load_courses_cached.cache_clear()
    course_routes._all_courses_json.cache_clear()
    course_routes._course_lookup.cache_clear()


# ── (a) Request body 上限 ────────────────────────────────────────────────────

def _echo_app(limit):
    async def echo(request: Request):
        body = await request.body()
        return JSONResponse({"len": len(body), "head": body[:10].decode("latin-1")})

    inner = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    return BodySizeLimitMiddleware(inner, max_bytes=limit)


async def _asgi_call(asgi_app, chunks, headers):
    """直接以 ASGI 驅動，送出沒有 Content-Length 的 chunked body。"""
    messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
                for i, c in enumerate(chunks)]
    received = []

    async def receive():
        if messages:
            return messages.pop(0)
        await asyncio.sleep(3600)

    async def send(message):
        received.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/echo", "raw_path": b"/echo", "query_string": b"",
             "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("test", 80)}
    await asgi_app(scope, receive, send)
    status = next(m["status"] for m in received if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in received if m["type"] == "http.response.body")
    return status, body, len(messages)


class BodyLimitTests(unittest.TestCase):
    def test_default_limit_is_64kb(self):
        self.assertEqual(DEFAULT_MAX_BODY_BYTES, 64 * 1024)

    def test_env_override_and_invalid_values(self):
        from app.body_limit import max_body_bytes_from_env
        with mock.patch.dict("os.environ", {"MAX_REQUEST_BODY_BYTES": "1024"}):
            self.assertEqual(max_body_bytes_from_env(), 1024)
        for bad in ("", "0", "-5", "abc", "1.5"):
            with mock.patch.dict("os.environ", {"MAX_REQUEST_BODY_BYTES": bad}):
                self.assertEqual(max_body_bytes_from_env(), DEFAULT_MAX_BODY_BYTES)

    def test_anonymous_oversized_json_rejected_before_auth(self):
        """真實 app：匿名送超過上限的 JSON 到需登入的路由，應先拿到 413，而不是解析後的 401。"""
        client = TestClient(app)
        payload = json.dumps({"question": "x" * (DEFAULT_MAX_BODY_BYTES + 10)})
        resp = client.post("/api/chat", content=payload, headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json(), {"detail": "請求內容過大。"})

    def test_small_body_still_reaches_auth(self):
        client = TestClient(app)
        resp = client.post("/api/chat", json={"question": "hello"})
        self.assertEqual(resp.status_code, 401)

    def test_413_keeps_cors_headers(self):
        client = TestClient(app)
        resp = client.post("/api/chat", content=b"x" * (DEFAULT_MAX_BODY_BYTES + 1),
                           headers={"Content-Type": "application/json", "Origin": "http://localhost:5173"})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "http://localhost:5173")

    def test_content_length_boundary(self):
        client = TestClient(_echo_app(100))
        self.assertEqual(client.post("/echo", content=b"a" * 100).json()["len"], 100)
        self.assertEqual(client.post("/echo", content=b"a" * 101).status_code, 413)

    def test_invalid_content_length(self):
        status, body, _ = asyncio.run(_asgi_call(_echo_app(100), [b"abc"], [(b"content-length", b"zz")]))
        self.assertEqual(status, 400)

    def test_chunked_over_limit_rejected_without_reading_rest(self):
        status, body, remaining = asyncio.run(
            _asgi_call(_echo_app(100), [b"a" * 60, b"b" * 60, b"c" * 60], []))
        self.assertEqual(status, 413)
        # 超過上限後就不再讀剩下的 chunk
        self.assertEqual(remaining, 1)

    def test_chunked_within_limit_is_replayed_intact(self):
        status, body, _ = asyncio.run(_asgi_call(_echo_app(100), [b"0123456789", b"abcdefghij"], []))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"len": 20, "head": "0123456789"})

    def test_get_without_body_unaffected(self):
        client = TestClient(app)
        self.assertEqual(client.get("/health").status_code, 200)


# ── (b) 課程列表快取 ─────────────────────────────────────────────────────────

class CourseCacheTests(unittest.TestCase):
    def setUp(self):
        _clear_course_caches()
        self.calls = []

        def fake_uncached(level):
            self.calls.append(level)
            return [dict(c) for c in FAKE_COURSES] if level == "undergrad" else [dict(FAKE_COURSES[1])]

        patcher = mock.patch.object(course_routes, "_load_courses_uncached", side_effect=fake_uncached)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_clear_course_caches)
        self.client = TestClient(app)

    def test_json_parsed_once_per_level(self):
        for _ in range(3):
            self.assertEqual(self.client.get("/api/courses").status_code, 200)
            self.client.get("/api/courses", params={"college": "工學院"})
            self.client.get("/api/courses/1001")
            self.client.get("/api/courses/does-not-exist")
        self.client.get("/api/courses", params={"level": "grad"})
        self.client.get("/api/courses", params={"level": "something-else"})  # 視為 undergrad
        self.assertEqual(sorted(self.calls), ["grad", "undergrad"])

    def test_full_list_matches_response_model_output(self):
        resp = self.client.get("/api/courses")
        self.assertEqual(resp.headers["content-type"], "application/json")
        data = resp.json()
        self.assertEqual([c["serial_no"] for c in data], ["1001", "CE1001-A", "1003"])
        # response_model 會丟掉 schema 外的欄位、補上預設值
        self.assertNotIn("extra_field_not_in_schema", data[0])
        self.assertIn("relevance_score", data[0])
        self.assertIsNone(data[0]["relevance_score"])
        # 與經過 limit（走 response_model 驗證路徑）的結果相同
        limited = self.client.get("/api/courses", params={"limit": 100}).json()
        self.assertEqual(data, limited)

    def test_filters_and_limit(self):
        self.assertEqual(len(self.client.get("/api/courses", params={"college": "工學院"}).json()), 2)
        self.assertEqual(len(self.client.get("/api/courses", params={"type": "選修"}).json()), 1)
        self.assertEqual(len(self.client.get("/api/courses", params={"department": "資工系", "limit": 1}).json()), 1)
        self.assertEqual(len(self.client.get("/api/courses", params={"limit": 0}).json()), 3)

    def test_course_by_id_first_match_semantics(self):
        # "CE1001-A" 是第 1 筆的 course_id，也是第 2 筆的 serial_no；原本的迴圈回傳第 1 筆
        self.assertEqual(self.client.get("/api/courses/CE1001-A").json()["serial_no"], "1001")
        self.assertEqual(self.client.get("/api/courses/MA2001-A").json()["serial_no"], "CE1001-A")
        self.assertEqual(self.client.get("/api/courses/1003").json()["course_name_zh"], "計算機概論（重複班）")
        self.assertEqual(self.client.get("/api/courses/nope").status_code, 404)

    def test_cached_objects_not_mutated_by_requests(self):
        self.client.get("/api/courses", params={"college": "工學院"})
        self.client.get("/api/courses/1001")
        cached = course_routes._load_courses_cached("undergrad")
        self.assertEqual([c["serial_no"] for c in cached], ["1001", "CE1001-A", "1003"])
        self.assertIn("extra_field_not_in_schema", cached[0])


# ── (c) 同步 I/O 不在 event loop 上 ──────────────────────────────────────────

SYNC_IO_ENDPOINTS = [
    (chat_routes, "chat"),
    (chat_routes, "list_sessions"),
    (chat_routes, "get_session"),
    (chat_routes, "delete_session"),
    (chat_routes, "get_analytics"),
    (course_routes, "get_courses"),
    (course_routes, "get_course_by_id"),
    (course_routes, "semantic_search_courses_endpoint"),
    (course_routes, "ask_course_question_endpoint"),
]


@contextmanager
def _override_user():
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _health_latency_during(path, json_body):
    """在 path 請求進行中打 /health，回傳「從送出主請求到 /health 完成」的時間與兩者狀態碼。

    從送出主請求開始計時：event loop 若被同步呼叫卡住，連 asyncio.sleep 都要等卡住結束才醒來，
    只量 /health 本身的延遲會漏掉這段。
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        t0 = time.perf_counter()
        main = asyncio.create_task(client.post(path, json=json_body))
        await asyncio.sleep(0.2)
        health = await client.get("/health")
        elapsed = time.perf_counter() - t0
        resp = await main
    return elapsed, health.status_code, resp.status_code


class EventLoopTests(unittest.TestCase):
    def test_sync_io_endpoints_are_plain_def(self):
        for module, name in SYNC_IO_ENDPOINTS:
            with self.subTest(endpoint=name):
                self.assertFalse(inspect.iscoroutinefunction(getattr(module, name)),
                                 f"{name} 做同步 I/O，必須是一般 def 才會交給 threadpool")

    def test_chat_llm_wait_does_not_block_health(self):
        def slow_generate(**kwargs):
            time.sleep(1.5)
            return {"answer": "ok", "tools_used": [], "sources": [], "model": "fake",
                    "input_tokens": 1, "output_tokens": 1}

        with _override_user(), \
                mock.patch.object(chat_routes.ss, "load", return_value=[]), \
                mock.patch.object(chat_routes.ss, "save"), \
                mock.patch.object(chat_routes.llm, "generate_with_tools", side_effect=slow_generate):
            latency, health_status, chat_status = asyncio.run(
                _health_latency_during("/api/chat", {"question": "hi"}))
        self.assertEqual(chat_status, 200)
        self.assertEqual(health_status, 200)
        self.assertLess(latency, 1.0, f"/health 在送出請求後 {latency:.2f}s 才完成，event loop 仍被 LLM 等待卡住")

    def test_course_ask_llm_wait_does_not_block_health(self):
        def slow_answer(course_id, question):
            time.sleep(1.5)
            return {"answer": "ok", "model": "fake", "warnings": []}

        with mock.patch.object(course_routes, "answer_course_question", side_effect=slow_answer):
            latency, health_status, ask_status = asyncio.run(
                _health_latency_during("/api/courses/1001/ask", {"question": "hi"}))
        self.assertEqual(ask_status, 200)
        self.assertEqual(health_status, 200)
        self.assertLess(latency, 1.0, f"/health 在送出請求後 {latency:.2f}s 才完成，event loop 仍被 LLM 等待卡住")

    def test_chat_stream_history_load_runs_in_threadpool(self):
        seen = {}

        def fake_load(sid, user_id=None):
            seen["thread"] = threading.current_thread()
            return []

        def fake_stream(**kwargs):
            yield 'data: {"type": "done", "final_answer": "ok"}\n\n'

        with _override_user(), \
                mock.patch.object(chat_routes.ss, "load", side_effect=fake_load), \
                mock.patch.object(chat_routes.ss, "save"), \
                mock.patch.object(chat_routes.llm, "stream_with_tools", side_effect=fake_stream):
            loop_thread = {}

            async def run():
                loop_thread["thread"] = threading.current_thread()
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.post("/api/chat/stream", json={"question": "hi"})

            resp = asyncio.run(run())
        self.assertEqual(resp.status_code, 200)
        self.assertIsNot(seen["thread"], loop_thread["thread"])


# ── (d) analytics 快取涵蓋 Qdrant 補查 ───────────────────────────────────────

class _FakeCursor:
    def __init__(self, one=None, many=None):
        self._one, self._many = one, many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _FakeConn:
    def __init__(self):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls += 1
        if "count(*)" in sql:
            return _FakeCursor(one={"n": 1})
        turn = {"tools_used": ["search_courses"], "course_pool": [],
                "course_cards": [{"name": "計算機概論", "dept": "資工系"}], "created_at": None}
        return _FakeCursor(many=[turn])

    def commit(self):
        pass


class AnalyticsCacheTests(unittest.TestCase):
    def test_route_passes_backfill_as_enrich(self):
        with _override_user(), \
                mock.patch("app.services.retriever._load_college_map", return_value={}), \
                mock.patch.object(chat_routes.ss, "get_analytics", return_value={"ok": True}) as ga:
            resp = TestClient(app).get("/api/chat/analytics")
        self.assertEqual(resp.json(), {"ok": True})
        self.assertIs(ga.call_args.kwargs["enrich"], chat_routes._backfill_domain_tags)

    def test_backfill_runs_before_cache_write_and_not_on_hit(self):
        cache = {}
        qdrant_calls = []

        def fake_get_courses_by_name(name):
            qdrant_calls.append(name)
            return [{"metadata": {"domain_tags": ["人工智慧::0.9", "低分::0.1"]}}]

        def cache_get(conn, user_id):
            return cache.get(user_id)

        def cache_set(conn, user_id, data):
            cache[user_id] = json.loads(json.dumps(data))

        with mock.patch.object(ss, "_ensure_schema"), \
                mock.patch.object(ss, "_connect", side_effect=lambda: _FakeConn()), \
                mock.patch.object(ss, "_analytics_cache_get", side_effect=cache_get), \
                mock.patch.object(ss, "_analytics_cache_set", side_effect=cache_set), \
                mock.patch("app.services.retriever.get_courses_by_name", side_effect=fake_get_courses_by_name):
            first = ss.get_analytics("firebase:dummy", college_map={}, enrich=chat_routes._backfill_domain_tags)
            second = ss.get_analytics("firebase:dummy", college_map={}, enrich=chat_routes._backfill_domain_tags)

        self.assertEqual(len(qdrant_calls), len(first["top_courses"][:10]))
        self.assertGreater(len(qdrant_calls), 0)
        self.assertEqual(first["top_domain_tags"], [{"tag": "人工智慧", "count": first["top_courses"][0]["count"]}])
        # 補查結果已寫進快取，第二次命中快取時不再查 Qdrant
        self.assertEqual(cache["firebase:dummy"]["top_domain_tags"], first["top_domain_tags"])
        self.assertEqual(second, cache["firebase:dummy"])
        self.assertEqual(len(qdrant_calls), len(first["top_courses"][:10]))


# ── (e) PDF 文件 ID 快取不寫入不存在的 project ────────────────────────────────

class ProjectDocCacheTests(unittest.TestCase):
    def test_unknown_project_not_cached(self):
        project_routes._project_doc_id_cache.clear()
        self.addCleanup(project_routes._project_doc_id_cache.clear)
        with mock.patch.object(project_routes, "load_projects", return_value=[{"id": "p1"}]):
            for i in range(50):
                self.assertIsNone(project_routes._get_document_id_for_project(f"unknown-{i}" + "x" * 500))
        self.assertEqual(project_routes._project_doc_id_cache, {})

    def test_known_project_without_pdf_still_cached(self):
        project_routes._project_doc_id_cache.clear()
        self.addCleanup(project_routes._project_doc_id_cache.clear)
        with mock.patch.object(project_routes, "load_projects", return_value=[{"id": "p1"}]):
            self.assertIsNone(project_routes._get_document_id_for_project("p1"))
        self.assertEqual(project_routes._project_doc_id_cache, {"p1": None})


if __name__ == "__main__":
    unittest.main()
