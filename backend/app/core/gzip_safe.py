"""
GZip compression that is safe for Server-Sent Events.

Starlette's stock GZipMiddleware compresses *streaming* responses by feeding
each chunk through a gzip compressor, which buffers — that delays and can break
SSE (`text/event-stream`), the dashboard's realtime device-push channel.

This middleware instead compresses ONLY single-shot responses — the JSON the API
actually returns (device lists, users, logs, reports, tech usage, …) — and lets
every streaming response pass through untouched. A single-shot response is one
delivered in a single `http.response.body` message (`more_body=False`), which is
exactly how ORJSONResponse emits its payload. SSE and any other StreamingResponse
set `more_body=True` on the first chunk and are therefore never touched.

Net effect: the big JSON payloads (the ones worth compressing — often 5-20× over
the wire) are gzipped, while realtime push stays fully real-time.
"""

from __future__ import annotations

import asyncio
import gzip

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Above this size, compression is pushed to a worker thread so it never blocks
# the event loop. Below it, gzip is sub-millisecond and the thread-hop overhead
# would cost more than it saves, so we compress inline.
_OFFLOAD_THRESHOLD = 16 * 1024


class SSESafeGZipMiddleware:
    def __init__(self, app: ASGIApp, minimum_size: int = 600, compresslevel: int = 5) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def _compress(self, body: bytes) -> bytes:
        """gzip `body`, keeping CPU-bound work off the event loop for large
        payloads. A synchronous gzip.compress() of a multi-hundred-KB response
        (e.g. a full device/user list) stalls the single async worker's loop —
        every other in-flight request sits 'pending' until it finishes. Large
        bodies are compressed in a thread; small ones inline (cheaper than the
        thread hop)."""
        if len(body) >= _OFFLOAD_THRESHOLD:
            return await asyncio.to_thread(gzip.compress, body, self.compresslevel)
        return gzip.compress(body, self.compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        accept = Headers(scope=scope).get("accept-encoding", "")
        if "gzip" not in accept.lower():
            await self.app(scope, receive, send)
            return

        start_message: Message | None = None
        sent = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message, sent

            if message["type"] == "http.response.start":
                # Defer the start until the first body chunk: we need to see
                # `more_body` and the body size before deciding to compress.
                start_message = message
                return

            if message["type"] != "http.response.body":
                await send(message)
                return

            assert start_message is not None
            headers          = Headers(raw=start_message["headers"])
            content_type     = headers.get("content-type", "")
            already_encoded  = "content-encoding" in headers
            body             = message.get("body", b"")
            more_body        = message.get("more_body", False)

            # Compress only a single-shot, uncompressed, non-SSE response that is
            # big enough to be worth it. Everything else streams through as-is.
            compressible = (
                not sent
                and not already_encoded
                and not more_body
                and "text/event-stream" not in content_type
                and len(body) >= self.minimum_size
            )

            if compressible:
                packed = await self._compress(body)
                new_headers = MutableHeaders(raw=list(start_message["headers"]))
                new_headers["Content-Encoding"] = "gzip"
                new_headers["Content-Length"]   = str(len(packed))
                vary = new_headers.get("Vary")
                new_headers["Vary"] = f"{vary}, Accept-Encoding" if vary else "Accept-Encoding"
                start_message["headers"] = new_headers.raw
                await send(start_message)
                await send({"type": "http.response.body", "body": packed, "more_body": False})
                sent = True
                return

            # Pass-through: streaming/SSE, already-encoded, or too small.
            if not sent:
                await send(start_message)
                sent = True
            await send(message)

        await self.app(scope, receive, send_wrapper)
