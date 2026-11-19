"""Give every HTTP request a correlation id and log the ones that failed.

Only the method, the route *template* (``/api/invitations/{token}``, never the real path, so
invitation tokens and ids stay out), the status and the duration are logged: no query string, no
headers, no cookies, no body, no client address. Successful requests are not logged. The id is
generated here (a client-supplied value is ignored) and returned as ``X-Request-ID`` so a user can
quote it when reporting a problem.
"""

import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rehearse_ai.operational_log import bound
from rehearse_ai.operational_log import event as log_event

LOGGER = logging.getLogger(__name__)


def route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        status = 0

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with bound(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                log_event(
                    LOGGER,
                    "http.request_failed",
                    level=logging.ERROR,
                    exc_info=True,
                    method=scope["method"],
                    route=route_template(scope),
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                raise
            if status >= 400:
                level = (
                    logging.ERROR
                    if status >= 500
                    else logging.WARNING
                    if status == 429
                    else logging.INFO
                )
                log_event(
                    LOGGER,
                    "http.request_failed" if status >= 500 else "http.request_rejected",
                    level=level,
                    method=scope["method"],
                    route=route_template(scope),
                    status=status,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
