"""Cancel-on-disconnect middleware.

When a dashboard tab navigates away (or aborts a fetch), uvicorn keeps running
the endpoint to completion by default — a 30-day aggregation keeps burning a
pooled DB connection for a client that is gone. This middleware watches for
the ASGI ``http.disconnect`` message while the endpoint runs and CANCELS the
handler task, which propagates into SQLAlchemy/asyncpg and cancels the
in-flight Postgres statement too.

Safety rules:
  • Only GET requests are cancellable — aborting a mutation mid-commit could
    lose an acknowledged write. GETs are read-only by contract.
  • Streaming endpoints (SSE) are skipped — they manage their own disconnect
    lifecycle and legitimately outlive normal requests.
"""
import asyncio
import logging

log = logging.getLogger(__name__)


class CancelOnDisconnectMiddleware:
    def __init__(self, app, skip_paths: tuple[str, ...] = ()):
        self.app = app
        self.skip_paths = skip_paths

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "GET"
            or any(scope.get("path", "").startswith(p) for p in self.skip_paths)
        ):
            await self.app(scope, receive, send)
            return

        # The app reads request messages through a queue so we can watch the
        # real receive channel for http.disconnect at the same time.
        queue: asyncio.Queue = asyncio.Queue()
        response_complete = False

        async def wrapped_receive():
            return await queue.get()

        async def wrapped_send(message):
            nonlocal response_complete
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                # Full response delivered — a socket close after this point is
                # normal connection teardown, NOT an abandoned request.
                response_complete = True
            await send(message)

        app_task = asyncio.create_task(self.app(scope, wrapped_receive, wrapped_send))
        recv_task = asyncio.create_task(receive())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {app_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if app_task in done:
                    recv_task.cancel()
                    await app_task  # propagate handler exceptions normally
                    return
                msg = recv_task.result()
                if msg["type"] == "http.disconnect":
                    if response_complete:
                        # Let dependency teardown finish untouched.
                        await app_task
                        return
                    app_task.cancel()
                    try:
                        await app_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001 — client is gone; response is moot
                        log.debug("handler raised after client disconnect", exc_info=True)
                    log.info("request cancelled (client disconnected): %s", scope.get("path"))
                    return
                await queue.put(msg)
                recv_task = asyncio.create_task(receive())
        finally:
            for t in (app_task, recv_task):
                if not t.done():
                    t.cancel()
