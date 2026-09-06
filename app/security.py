"""
Защита и наблюдаемость HTTP-слоя (по мотивам notion2api/app/server.py + limiter.py).

Отличия в лучшую сторону:
- rate limiter — без новых зависимостей (у них slowapi): скользящее окно
  в памяти, O(1) амортизированно на запрос;
- 429/401 отдаются в OpenAI-совместимой форме {"error": {...}}, как принято
  в этом проекте, а не голой строкой;
- access-лог пропускает /healthz и /favicon.ico, чтобы не спамить.
"""

import logging
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("security")


def openai_error(status: int, message: str, etype: str, code: str = "") -> JSONResponse:
    err = {"message": message, "type": etype}
    if code:
        err["code"] = code
    return JSONResponse(status_code=status, content={"error": err})


class SlidingWindowLimiter:
    """Скользящее окно: не более `limit` запросов за `window_seconds` с одного IP."""

    def __init__(self, limit_per_minute: int = 25, window_seconds: float = 60.0):
        self.limit = max(0, int(limit_per_minute))
        self.window = max(1.0, float(window_seconds))
        self._hits: Dict[str, Deque[float]] = {}

    def check(self, ip: str) -> Tuple[bool, float]:
        """(allowed, retry_after_seconds)."""
        if self.limit <= 0:
            return True, 0.0
        now = time.time()
        dq = self._hits.get(ip)
        if dq is None:
            dq = self._hits[ip] = deque()
        while dq and dq[0] <= now - self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            return False, max(1.0, dq[0] + self.window - now)
        dq.append(now)
        # Чистим давно молчавшие IP, чтобы словарь не рос бесконечно.
        if len(self._hits) > 4096:
            for key in [k for k, v in self._hits.items() if not v]:
                del self._hits[key]
        return True, 0.0


def install_security(
    app: FastAPI,
    *,
    api_key: str = "",
    limiter: "Optional[SlidingWindowLimiter]" = None,
) -> None:
    """Регистрирует middleware: Bearer-авторизация /v1/*, access-лог, favicon."""

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next):
        if api_key and request.url.path.startswith("/v1") and request.method != "OPTIONS":
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != api_key:
                return openai_error(
                    401, "Error: API KEY doesn't match.",
                    "invalid_request_error", "invalid_api_key",
                )
        return await call_next(request)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.time()
        skip = request.url.path in ("/healthz", "/health", "/favicon.ico")
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            logger.exception("Unhandled exception")
            raise
        finally:
            if not skip:
                ip = request.client.host if request.client else "unknown"
                elapsed_ms = round((time.time() - start) * 1000, 2)
                log = logger.error if status >= 400 else logger.info
                log(
                    "Request processed",
                    extra={"request_info": {
                        "method": request.method, "path": request.url.path,
                        "ip": ip, "status_code": status,
                        "duration_ms": elapsed_ms,
                    }},
                )
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        from fastapi.responses import Response

        return Response(content=b"", media_type="image/x-icon", status_code=204)
