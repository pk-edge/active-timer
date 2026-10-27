"""Request-body size caps — pure-ASGI middleware.

Rejects oversized request bodies with 413 BEFORE they are buffered into a
worker's memory (two 800 MB-budget workers must not be OOM-able by one
unauthenticated POST):

  1. Declared size: a Content-Length above the route's cap is refused up
     front, without reading a byte of the body.
  2. Streamed size: chunked uploads (no Content-Length) and clients that lie
     about Content-Length are caught by counting the bytes the app actually
     reads from `receive`. On overflow the 413 is sent immediately and the app
     is handed an `http.disconnect`, so it stops reading; anything it tries to
     send afterwards is swallowed.

Default cap: Settings.MAX_REQUEST_BODY_BYTES (1 MiB). Legitimately large
routes get their own cap (see `rules_from_settings`):
  POST /v1/ingest          agent event batches        MAX_INGEST_BODY_BYTES (4 MiB)
  POST /v1/groups/import   CSV import                 MAX_IMPORT_BODY_BYTES (5 MiB)
  POST /v1/installers      agent installer upload     MAX_INSTALLER_MB + 1 MiB
  POST /v1/files           admin shared-file upload   MAX_FILE_UPLOAD_BODY_MB (5121 MiB)
Screenshots do NOT go through the API: POST /v1/screenshots only returns a
presigned S3 URL (tiny JSON); the agent PUTs the image to S3 directly.

Sits INSIDE CORSMiddleware so the 413 carries CORS headers.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

_MiB = 1024 * 1024


def rules_from_settings(s) -> list[tuple[frozenset[str], re.Pattern, int]]:
    def rule(method: str, path: str, cap: int):
        return (frozenset({method}), re.compile(rf"^{re.escape(path)}/?$"), cap)

    return [
        rule("POST", "/v1/ingest",        s.MAX_INGEST_BODY_BYTES),
        rule("POST", "/v1/groups/import", s.MAX_IMPORT_BODY_BYTES),
        rule("POST", "/v1/installers",    s.MAX_INSTALLER_MB * _MiB + _MiB),
        rule("POST", "/v1/files",         s.MAX_FILE_UPLOAD_BODY_MB * _MiB),
    ]


class RequestSizeLimitMiddleware:
    def __init__(self, app, default_max: int, rules=()):
        self.app = app
        self.default_max = default_max
        self.rules = list(rules)

    def limit_for(self, method: str, path: str) -> int:
        for methods, pattern, cap in self.rules:
            if method in methods and pattern.match(path):
                return cap
        return self.default_max

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope.get("method", "GET"), scope.get("path", ""))
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        declared = None
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            await self._reject(send, limit, close=True)
            return

        total = 0
        rejected = False
        started = False

        async def wrapped_receive():
            nonlocal total, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    rejected = True
                    if not started:
                        await self._reject(send, limit, close=True)
                    return {"type": "http.disconnect"}
            return message

        async def wrapped_send(message):
            nonlocal started
            if rejected:
                return                      # 413 already sent; drop the app's output
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, wrapped_receive, wrapped_send)

    @staticmethod
    async def _reject(send, limit: int, close: bool) -> None:
        body = json.dumps({
            "detail": f"Request body too large (max {limit} bytes)",
        }).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if close:
            headers.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
