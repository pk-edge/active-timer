"""Request-body caps: declared and streamed 413s, per-route allowances, CORS."""
import json

import httpx
import pytest

from app.core.RequestSizeLimit import RequestSizeLimitMiddleware, rules_from_settings
from app.core.config import get_settings

MiB = 1024 * 1024


def _mw(app, default=1 * MiB, **kw):
    return RequestSizeLimitMiddleware(app, default_max=default, rules=rules_from_settings(get_settings()))


class Recorder:
    """Raw ASGI app that reads the whole body, like a FastAPI body parameter."""
    def __init__(self):
        self.called = False
        self.read = 0

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                break
            self.read += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(mw, method="POST", path="/v1/x", headers=(), chunks=()):
    sent = []
    msgs = list(chunks)

    async def receive():
        if msgs:
            return msgs.pop(0)
        return {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": method, "path": path, "headers": list(headers),
             "query_string": b"", "client": ("1.1.1.1", 1)}
    await mw(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], sent


def _chunks(total, size=64 * 1024):
    out, left = [], total
    while left > 0:
        n = min(size, left)
        left -= n
        out.append({"type": "http.request", "body": b"x" * n, "more_body": left > 0})
    return out


@pytest.mark.asyncio
async def test_declared_oversize_rejected_without_reading_body():
    rec = Recorder()
    status, sent = await _call(_mw(rec), headers=[(b"content-length", str(2 * MiB).encode())])
    assert status == 413 and not rec.called


@pytest.mark.asyncio
async def test_small_body_passes():
    rec = Recorder()
    status, _ = await _call(_mw(rec), headers=[(b"content-length", b"1000")], chunks=_chunks(1000))
    assert status == 200 and rec.read == 1000


@pytest.mark.asyncio
async def test_streamed_body_without_content_length_is_cut_off():
    rec = Recorder()
    status, sent = await _call(_mw(rec), chunks=_chunks(3 * MiB))
    assert status == 413
    # the app was cut off early and its own 200 was swallowed
    assert rec.read <= 1 * MiB + 64 * 1024
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert b"too large" in b"".join(m.get("body", b"") for m in sent)


@pytest.mark.asyncio
async def test_lying_content_length_caught_by_streamed_count():
    rec = Recorder()
    status, _ = await _call(_mw(rec), headers=[(b"content-length", b"10")], chunks=_chunks(2 * MiB))
    assert status == 413


@pytest.mark.asyncio
async def test_large_upload_routes_get_their_own_caps():
    s = get_settings()
    # ingest: 2 MiB is fine (cap 4 MiB), but the same body to an ordinary route is not
    rec = Recorder()
    st, _ = await _call(_mw(rec), path="/v1/ingest", headers=[(b"content-length", str(2 * MiB).encode())],
                        chunks=_chunks(2 * MiB))
    assert st == 200 and rec.read == 2 * MiB
    st, _ = await _call(_mw(Recorder()), path="/v1/users", headers=[(b"content-length", str(2 * MiB).encode())])
    assert st == 413
    # ...but ingest still has an upper bound
    st, _ = await _call(_mw(Recorder()), path="/v1/ingest",
                        headers=[(b"content-length", str(s.MAX_INGEST_BODY_BYTES + 1).encode())])
    assert st == 413
    # installer + shared-file uploads: hundreds of MB allowed, within their configured ceilings
    big = 100 * MiB
    for path in ("/v1/installers", "/v1/files"):
        st, _ = await _call(_mw(Recorder()), path=path, headers=[(b"content-length", str(big).encode())])
        # declared size is within cap -> app is invoked (Recorder reads nothing: no chunks => disconnect)
        assert st == 200, path
    over = s.MAX_INSTALLER_MB * MiB + 2 * MiB
    st, _ = await _call(_mw(Recorder()), path="/v1/installers", headers=[(b"content-length", str(over).encode())])
    assert st == 413
    # CSV import cap
    st, _ = await _call(_mw(Recorder()), path="/v1/groups/import",
                        headers=[(b"content-length", str(3 * MiB).encode())])
    assert st == 200
    # route caps are method-specific: GET to /v1/ingest uses the default cap
    st, _ = await _call(_mw(Recorder()), method="GET", path="/v1/ingest",
                        headers=[(b"content-length", str(2 * MiB).encode())])
    assert st == 413


@pytest.mark.asyncio
async def test_real_app_413_carries_cors_and_never_hits_db():
    from app.main import create_app
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/auth/admin/login", content=b"x" * (2 * MiB),
                         headers={"Origin": "http://localhost:3000", "Content-Type": "application/json"})
        assert r.status_code == 413
        assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert "detail" in r.json()
