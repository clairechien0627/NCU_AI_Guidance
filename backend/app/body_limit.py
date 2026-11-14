"""
Request body 大小上限（純 ASGI 中介層）。

FastAPI 會在執行 dependency（包含登入驗證）之前，先在 event loop 上把整個 body 讀進記憶體並解析 JSON，
所以沒有上限時，匿名請求就能用大型 body 換到數十倍的記憶體與 event loop 時間。

- 有 Content-Length：超過上限直接回 413，不讀 body。
- 沒有 Content-Length（例如 Transfer-Encoding: chunked）：由這裡自己讀 body 並累計位元組，
  超過上限就回 413；沒超過才把已讀的 body 重播給下游 app。
  不在包裝後的 receive 裡拋例外，因為 FastAPI 會把 body 讀取期間的例外轉成 400，就回不了 413。

必須是純 ASGI 中介層（不能用 BaseHTTPMiddleware），並放在任何會讀 body 的層之外。
"""

import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_MAX_BODY_BYTES = 64 * 1024
_TOO_LARGE_DETAIL = "請求內容過大。"


def max_body_bytes_from_env() -> int:
    """讀取 MAX_REQUEST_BODY_BYTES；未設定或不是正整數時使用預設 64 KB。"""
    raw = os.getenv("MAX_REQUEST_BODY_BYTES", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_MAX_BODY_BYTES


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes if max_bytes is not None else max_body_bytes_from_env()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                content_length = value
                break

        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                await JSONResponse({"detail": "Content-Length 格式錯誤。"}, status_code=400)(scope, receive, send)
                return
            if declared > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            # 宣告的長度在上限內；uvicorn（h11/httptools）會強制實際 body 與 Content-Length 一致
            await self.app(scope, receive, send)
            return

        # 沒有 Content-Length：自己讀完 body（有上限），再重播給下游
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": _TOO_LARGE_DETAIL}, status_code=413)
        await response(scope, receive, send)
