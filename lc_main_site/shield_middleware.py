# shield_middleware.py
import hmac, hashlib, base64, time
from typing import Iterable, Optional, Callable, Set
from starlette.responses import HTMLResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.datastructures import Headers

_CHALLENGE_TEMPLATE = """\
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Проверка браузера…</title>
  <style>
    html,body{height:100%;margin:0}
    body{display:flex;align-items:center;justify-content:center;background:#0d1117;color:#e6edf3;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif}
    .card{padding:32px 28px;border-radius:16px;background:#161b22;box-shadow:0 10px 30px rgba(0,0,0,.45);max-width:520px;text-align:center}
    .spinner{width:44px;height:44px;border:4px solid #30363d;border-top-color:#2f81f7;border-radius:50%;margin:18px auto;animation:spin 1s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    .muted{color:#8b949e;font-size:14px;margin-top:8px}
  </style>
  <meta http-equiv="refresh" content="1;url={redirect}">
</head>
<body>
  <div class="card">
    <h2>Идёт быстрая проверка браузера…</h2>
    <div class="spinner"></div>
    <p class="muted">Обычно занимает 1–2 секунды.<br/>Если не произошло перенаправление — обновите страницу.</p>
  </div>
</body>
</html>
"""

class Shield:
    """
    Простая защита: выставляет cookie с подписью (HMAC), основанной на IP+UA+таймштамп.
    """
    def __init__(
        self,
        *,
        secret: str,
        cookie_name: str = "lc_shield",
        ttl_seconds: int = 12 * 60 * 60,   # срок действия метки
        bypass_paths: Optional[Iterable[str]] = ("/static/", "/health", "/favicon.ico"),
        protected_prefixes: Optional[Iterable[str]] = ("/",),
        difficulty_bits: int = 18,  # зарезервировано под POW; пока не используется
    ):
        self.secret = secret.encode("utf-8")
        self.cookie_name = cookie_name
        self.ttl_seconds = ttl_seconds
        self.bypass_paths: Set[str] = set(bypass_paths or [])
        self.protected_prefixes: Set[str] = set(protected_prefixes or ["/"])
        self.difficulty_bits = difficulty_bits

    def _sign(self, msg: bytes) -> str:
        sig = hmac.new(self.secret, msg, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def _make_token(self, ip: str, ua: str) -> str:
        ts = int(time.time())
        payload = f"{ip}|{ua}|{ts}".encode("utf-8")
        sig = self._sign(payload)
        raw = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return f"{raw}.{sig}"

    def _verify(self, ip: str, ua: str, token: str) -> bool:
        try:
            raw, sig = token.split(".", 1)
            payload = base64.urlsafe_b64decode(raw + "==")
            if not hmac.compare_digest(self._sign(payload), sig):
                return False
            parts = payload.decode().split("|")
            if len(parts) != 3:
                return False
            ip2, ua2, ts_str = parts
            if ip2 != ip or ua2 != ua:
                return False
            ts = int(ts_str)
            if time.time() - ts > self.ttl_seconds:
                return False
            return True
        except Exception:
            return False

class _ShieldMiddleware:
    def __init__(self, app: ASGIApp, shield: Shield):
        self.app = app
        self.shield = shield

    def _is_bypass(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.shield.bypass_paths)

    def _is_protected(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.shield.protected_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "/") or "/"
        if self._is_bypass(path) or not self._is_protected(path):
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        cookies = headers.get("cookie") or ""
        token = None
        for part in cookies.split(";"):
            part = part.strip()
            if part.startswith(self.shield.cookie_name + "="):
                token = part.split("=", 1)[1]
                break

        ip = scope.get("client")[0] if scope.get("client") else "0.0.0.0"
        ua = headers.get("user-agent") or "unknown"

        if token and self.shield._verify(ip, ua, token):
            return await self.app(scope, receive, send)

        # Выставляем cookie и отдаём "проверку"
        new_token = self.shield._make_token(ip, ua)
        target = path or "/"
        html = _CHALLENGE_TEMPLATE.format(redirect=target)
        resp = HTMLResponse(html, status_code=200)
        resp.set_cookie(
            key=self.shield.cookie_name,
            value=new_token,
            max_age=self.shield.ttl_seconds,
            httponly=True,
            samesite="lax",
        )
        return await resp(scope, receive, send)

def install_shield(app, shield: Shield):
    """
    Пример:
        from shield_middleware import Shield, install_shield
        shield = Shield(secret="super_secret", ttl_seconds=12*60*60)
        install_shield(app, shield)
    """
    app.add_middleware(_ShieldMiddleware, shield=shield)
