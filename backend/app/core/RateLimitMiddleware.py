"""Global API rate limiting — pure-ASGI middleware.

Pure ASGI (NOT BaseHTTPMiddleware) so SSE and other streaming responses are
never buffered: the limiter only looks at the request, charges its buckets,
and either short-circuits with a 429 or hands the untouched `send` straight
through (a thin wrapper only appends X-RateLimit-* headers to the response
start). A streaming response therefore counts ONCE, at connect.

Identity (cheap, no DB — HS256 verify via the same `decode_token` the auth
dependencies use, so signature AND expiry are enforced):
  • device JWT (type=device)  -> one bucket per DEVICE token subject. Never
    per-IP: many agents sit behind one office NAT. Any route, so ingest,
    heartbeats, /agent/config, /policy, /updates/* are all covered.
  • admin JWT (type=admin)    -> per-user buckets + a per-company bucket.
  • expired token with a VALID signature -> its own per-subject bucket (an
    office of users whose sessions all lapsed overnight must not share, and
    exhaust, one IP bucket on the way to the login page).
  • missing / forged token -> per client IP (anonymous bucket).
  • SSE endpoints (path ends /stream) that EventSource can't give a header to
    (/v1/devices/stream?token=) are keyed by the ?token= query JWT.

Client IP is `scope["client"]`, i.e. whatever uvicorn resolved. uvicorn only
honours X-Forwarded-For from peers listed in FORWARDED_ALLOW_IPS (env, read by
uvicorn itself; see the Dockerfile) — NEVER set that to '*' on a host that is
reachable without the trusted proxy, or the header (and so this limit) is
spoofable.

Policies (all env-overridable, see Settings.RATE_LIMIT_*):
  burst   20 / 5 s   per user      (a page load fires ~8-10 parallel GETs)
  read    120 / min  per user      GET, HEAD
  write   30 / min   per user      POST, PUT, PATCH, DELETE
  search  30 / min   per user      GET with a non-empty ?search= or ?q= (extra)
  report  2 / min    per user      POST /v1/reports/jobs (extra)
  company 6000 / min per company   all admin users combined (~100 RPS = the box's
                                   burst ceiling: stops one tenant monopolising
                                   the server, it does NOT police users)
  anon    300 / min  per client IP unauthenticated / forged token
  device  1200 / min per device    agent traffic

Exempt: /health*, OPTIONS (CORS preflight), /docs /redoc /openapi.json, and
the credential endpoints that already carry their own stricter limiter
(POST /v1/auth/admin/login, /register, /enroll, /v1/setup/reset-admin) — an
office behind one NAT must not lock itself out of login through the shared
anonymous IP bucket. Google OAuth start/callback have no limiter of their own,
so they get a separate per-IP "oauth" bucket (same size as anon) that expired
or garbage traffic cannot drain.

On exceed: 429 {"detail": ...} + Retry-After and X-RateLimit-Limit/-Remaining/
-Reset (seconds until the window rolls over). The middleware sits INSIDE
CORSMiddleware (see main.py), so the 429 carries CORS headers and the browser
can read it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs

import jwt

from app.core.rate_limit import Bucket, BucketResult, MultiWindowLimiter

log = logging.getLogger(__name__)

_READ_METHODS = frozenset({"GET", "HEAD"})
_EXEMPT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json")
_REPORT_JOB_PATH = "/v1/reports/jobs"

# Endpoints with their OWN stricter limiter (app/core/rate_limit.DistributedLimiter
# in auth.py / setup.py): skipped by the global limiter entirely.
_OWN_LIMITER_ROUTES = frozenset({
    ("POST", "/v1/auth/admin/login"),
    ("POST", "/v1/auth/register"),
    ("POST", "/v1/auth/enroll"),
    ("POST", "/v1/setup/reset-admin"),
})
# No limiter of their own: charged to a dedicated per-IP bucket, not "anon".
_OAUTH_ROUTES = frozenset({
    ("GET", "/v1/auth/google"),
    ("GET", "/v1/auth/google/callback"),
})


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool = True
    burst_max: int = 20
    burst_window: int = 5
    read_per_min: int = 120
    write_per_min: int = 30
    search_per_min: int = 30
    report_job_per_min: int = 2
    company_per_min: int = 6000
    anon_per_min: int = 300
    device_per_min: int = 1200
    redis_retry_secs: float = 5.0
    redis_fail_threshold: int = 3

    @classmethod
    def from_settings(cls, s) -> "RateLimitConfig":
        return cls(
            enabled=s.RATE_LIMIT_ENABLED,
            burst_max=s.RATE_LIMIT_BURST_MAX,
            burst_window=s.RATE_LIMIT_BURST_WINDOW_SECS,
            read_per_min=s.RATE_LIMIT_READ_PER_MIN,
            write_per_min=s.RATE_LIMIT_WRITE_PER_MIN,
            search_per_min=s.RATE_LIMIT_SEARCH_PER_MIN,
            report_job_per_min=s.RATE_LIMIT_REPORT_JOB_PER_MIN,
            company_per_min=s.RATE_LIMIT_COMPANY_PER_MIN,
            anon_per_min=s.RATE_LIMIT_ANON_PER_MIN,
            device_per_min=s.RATE_LIMIT_DEVICE_PER_MIN,
            redis_retry_secs=s.RATE_LIMIT_REDIS_RETRY_SECS,
            redis_fail_threshold=s.RATE_LIMIT_REDIS_FAIL_THRESHOLD,
        )


def _bearer_token(headers: list[tuple[bytes, bytes]], path: str, query_string: bytes) -> str:
    for k, v in headers:
        if k == b"authorization":
            if v[:7].lower() == b"bearer ":
                return v[7:].strip().decode("latin-1")
            break
    # EventSource cannot set headers, so SSE endpoints take the JWT as ?token=.
    if path.endswith("/stream") and b"token=" in query_string:
        qs = parse_qs(query_string.decode("latin-1"), keep_blank_values=False)
        return (qs.get("token") or [""])[0].strip()
    return ""


def _identity(headers: list[tuple[bytes, bytes]], client_ip: str,
              path: str = "", query_string: bytes = b"") -> tuple[str, str, str | None]:
    """-> (kind, subject, company_id). kind in {"device", "admin", "anon"}."""
    token = _bearer_token(headers, path, query_string)
    if token:
        try:
            from app.core.security import decode_token
            payload = decode_token(token)
            sub = payload.get("sub")
            ttype = payload.get("type")
            if sub and ttype == "device":
                return "device", str(sub), None
            if sub and ttype == "admin":
                cid = payload.get("company_id")
                return "admin", str(sub), str(cid) if cid else None
        except jwt.ExpiredSignatureError:
            # Signature is valid, only exp has passed: the subject is trustworthy
            # enough to key an anonymous bucket on, so lapsed sessions do not
            # pile into (and exhaust) the shared per-IP bucket. Never grants
            # authenticated treatment (no user/company buckets).
            try:
                from app.core.config import get_settings
                s = get_settings()
                payload = jwt.decode(token, s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM],
                                     options={"verify_exp": False})
                if payload.get("sub"):
                    return "anon", f"sub:{payload['sub']}", None
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — invalid/forged => anonymous (by IP)
            pass
    return "anon", client_ip, None


def _has_search(query_string: bytes) -> bool:
    if b"search=" not in query_string and b"q=" not in query_string:
        return False
    qs = parse_qs(query_string.decode("latin-1"), keep_blank_values=False)
    return bool((qs.get("search") or [""])[0].strip() or (qs.get("q") or [""])[0].strip())


class RateLimitMiddleware:
    def __init__(self, app, config: RateLimitConfig | None = None,
                 limiter: MultiWindowLimiter | None = None):
        from app.core.config import get_settings
        self.app = app
        self.cfg = config or RateLimitConfig.from_settings(get_settings())
        self.limiter = limiter or MultiWindowLimiter(
            redis_retry_secs=self.cfg.redis_retry_secs,
            redis_fail_threshold=self.cfg.redis_fail_threshold)

    # ------------------------------------------------------------------ policy
    def _buckets(self, kind: str, subject: str, company_id: str | None,
                 method: str, path: str, query_string: bytes) -> list[Bucket]:
        c = self.cfg
        if kind == "device":
            return [Bucket("device", f"d:{subject}", c.device_per_min, 60)]
        if kind == "anon":
            key = subject if subject.startswith("sub:") else f"ip:{subject}"
            name = "oauth" if (method, path.rstrip("/")) in _OAUTH_ROUTES else "anon"
            return [Bucket(name, key, c.anon_per_min, 60)]
        u = f"u:{subject}"
        b = [Bucket("burst", u, c.burst_max, c.burst_window)]
        if method in _READ_METHODS:
            b.append(Bucket("read", u, c.read_per_min, 60))
            if _has_search(query_string):
                b.append(Bucket("search", u, c.search_per_min, 60))
        else:
            b.append(Bucket("write", u, c.write_per_min, 60))
            if method == "POST" and path.rstrip("/") == _REPORT_JOB_PATH:
                b.append(Bucket("report", u, c.report_job_per_min, 60))
        if company_id:
            b.append(Bucket("company", f"c:{company_id}", c.company_per_min, 60, gate=False))
        return b

    # -------------------------------------------------------------------- ASGI
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.cfg.enabled:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if method == "OPTIONS" or path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        if (method, path.rstrip("/")) in _OWN_LIMITER_ROUTES:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        query_string = scope.get("query_string", b"")
        kind, subject, company_id = _identity(scope.get("headers") or [], client_ip,
                                              path, query_string)
        buckets = self._buckets(kind, subject, company_id, method, path, query_string)
        try:
            results = await self.limiter.consume(buckets)
        except Exception:  # noqa: BLE001 — a limiter bug must not take the API down
            log.exception("rate_limit: limiter error — request allowed")
            await self.app(scope, receive, send)
            return

        violated = [r for r in results if r.exceeded]
        if violated:
            worst = max(violated, key=lambda r: r.reset)
            log.debug("rate limited %s %s (%s:%s policy=%s)", method, path, kind, subject, worst.name)
            await self._reject(send, worst)
            return

        tight = min(results, key=lambda r: r.remaining / r.limit if r.limit else 0.0)
        extra = [
            (b"x-ratelimit-limit", str(tight.limit).encode()),
            (b"x-ratelimit-remaining", str(tight.remaining).encode()),
            (b"x-ratelimit-reset", str(tight.reset).encode()),
        ]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(extra)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    async def _reject(send, r: BucketResult) -> None:
        body = json.dumps({
            "detail": f"Too many requests ({r.name} limit). Retry in {r.reset}s.",
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(r.reset).encode()),
                (b"x-ratelimit-limit", str(r.limit).encode()),
                (b"x-ratelimit-remaining", b"0"),
                (b"x-ratelimit-reset", str(r.reset).encode()),
                (b"x-ratelimit-policy", r.name.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
