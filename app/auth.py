"""访问口令保护：公网暴露时的单口令登录（HMAC 签名 Cookie，无第三方依赖）。"""
from __future__ import annotations

import hashlib
import hmac
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE_NAME = "pr_session"
SESSION_TTL = 30 * 86400          # 30 天免登录
PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = ("/login", "/favicon.ico", "/api/health")   # health 供容器健康检查使用，不含密钥


def _secret(password: str) -> bytes:
    return hashlib.sha256(("paper-reader|" + password).encode("utf-8")).digest()


def make_token(password: str, ttl: int = SESSION_TTL) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(_secret(password), str(exp).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(password: str, token: str | None) -> bool:
    if not token or token.count(".") != 1:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expect = hmac.new(_secret(password), exp_s.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)


_LOGIN_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f5f1;
     font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;color:#22201c}
.card{width:min(360px,92vw);background:#fff;border:1px solid #e5e1d8;border-radius:14px;
      padding:30px 28px;box-shadow:0 10px 34px rgba(0,0,0,.07)}
h1{margin:0 0 6px;font-size:19px;letter-spacing:.2px}
p.sub{margin:0 0 22px;color:#8b857a;font-size:13px}
input{width:100%;padding:11px 13px;border:1px solid #ddd8cd;border-radius:9px;font-size:15px;
      background:#fbfaf7;outline:none}
input:focus{border-color:#3f6f52;background:#fff}
button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:9px;background:#2f5d45;
       color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#274e3a}
.err{margin:12px 0 0;color:#b4322a;font-size:13px}
.hint{margin:18px 0 0;color:#a8a294;font-size:12px;text-align:center}
"""


def login_page(error: bool = False, next_url: str = "/") -> HTMLResponse:
    err = '<p class="err">口令不正确，请重试。</p>' if error else ""
    safe_next = next_url if next_url.startswith("/") else "/"
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Reader · 登录</title><style>{_LOGIN_CSS}</style></head>
<body><form class="card" method="post" action="/login">
  <h1>Paper Reader</h1>
  <p class="sub">论文双语阅读器 · 需要口令访问</p>
  <input type="password" name="password" placeholder="访问口令" autofocus autocomplete="current-password">
  <input type="hidden" name="next" value="{safe_next}">
  <button type="submit">进入</button>
  {err}
  <p class="hint">口令由站点所有者在 .env 的 APP_PASSWORD 中设置</p>
</form></body></html>"""
    return HTMLResponse(html, status_code=401 if error else 200)


class AuthMiddleware(BaseHTTPMiddleware):
    """未登录时：页面请求跳登录页，接口请求返回 401。"""

    def __init__(self, app, password: str) -> None:
        super().__init__(app)
        self.password = (password or "").strip()

    async def dispatch(self, request, call_next):
        if not self.password:
            return await call_next(request)
        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_PATHS:
            return await call_next(request)
        if check_token(self.password, request.cookies.get(COOKIE_NAME)):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录，请先通过 /login 输入口令"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
