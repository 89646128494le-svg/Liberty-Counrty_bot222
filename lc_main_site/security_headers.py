# security_headers.py
from starlette.types import ASGIApp, Receive, Scope, Send
from typing import Callable, Optional

class _SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, https_only: bool = False):
        self.app = app
        self.https_only = https_only

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))

                def set_header(name: str, value: str):
                    headers[name.lower().encode()] = value.encode()

                # Минимальный набор безопасных заголовков
                set_header("x-frame-options", "DENY")
                set_header("x-content-type-options", "nosniff")
                set_header("referrer-policy", "strict-origin-when-cross-origin")
                set_header("permissions-policy", "camera=(), microphone=(), geolocation=()")
                set_header("cache-control", "no-store")

                # CSP: разрешаем статику и инлайн-стили (если у тебя есть nonce — лучше заменить на него)
                csp = (
                    "default-src 'self'; "
                    "img-src 'self' data: https:; "
                    "style-src 'self' 'unsafe-inline'; "
                    "script-src 'self'; "
                    "font-src 'self' data:; "
                    "connect-src 'self'; "
                    "frame-ancestors 'none'"
                )
                set_header("content-security-policy", csp)

                # HSTS только если реально за прокси с HTTPS
                if self.https_only:
                    set_header("strict-transport-security", "max-age=31536000; includeSubDomains; preload")

                message["headers"] = list(headers.items())
            await send(message)

        await self.app(scope, receive, send_wrapper)

def install_security_headers(app, *, https_only: bool = False):
    """
    Установить middleware заголовков безопасности.
    Пример:
        from security_headers import install_security_headers
        install_security_headers(app, https_only=False)
    """
    app.add_middleware(_SecurityHeadersMiddleware, https_only=https_only)
