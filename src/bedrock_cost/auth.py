"""登录。

单账号口令 + 按 IP 的失败锁定，按本地自用场景设计。要对外提供访问需要换成
真正的用户体系，并启用 HTTPS 与 SESSION_COOKIE_SECURE。
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from . import config, login_log

bp = Blueprint("auth", __name__)

# ip -> [失败次数, 首次失败时间]
_attempts_lock = threading.Lock()
_attempts: dict[str, list] = {}


def client_ip() -> str:
    """登录锁定按这个 IP 计数。

    直接读 X-Forwarded-For 是不安全的：那个头可以由客户端伪造，每次换一个值
    就能绕开锁定。这里一律用 remote_addr——本地直连时它本来就是对的，跑在
    反向代理后面时由 ProxyFix 按 TRUST_PROXY 还原（见 create_app）。
    """
    return request.remote_addr or "?"


def lockout_remaining(ip: str) -> int:
    """还需锁定多少秒，0 表示未锁定。"""
    with _attempts_lock:
        record = _attempts.get(ip)
        if not record:
            return 0
        count, first_ts = record
        if count < config.MAX_LOGIN_ATTEMPTS:
            return 0
        elapsed = time.time() - first_ts
        if elapsed >= config.LOCKOUT_SECONDS:
            _attempts.pop(ip, None)
            return 0
        return int(config.LOCKOUT_SECONDS - elapsed)


def record_failure(ip: str) -> None:
    with _attempts_lock:
        record = _attempts.get(ip)
        if not record or time.time() - record[1] > config.LOCKOUT_SECONDS:
            _attempts[ip] = [1, time.time()]
        else:
            record[0] += 1


def clear_failures(ip: str | None = None) -> None:
    """清掉失败计数；不传 ip 就全清（测试用）。"""
    with _attempts_lock:
        if ip is None:
            _attempts.clear()
        else:
            _attempts.pop(ip, None)


def _same(expected: str, given: str) -> bool:
    """定时比较。按 UTF-8 字节比：str 直接比遇到非 ASCII（有人在框里输了中文）会抛 TypeError，登录页就 500 了。"""
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))


def password_matches(candidate: str) -> bool:
    if config.AUTH_PASSWORD_HASH:
        return check_password_hash(config.AUTH_PASSWORD_HASH, candidate)
    if not config.AUTH_PASSWORD:
        return False
    return _same(config.AUTH_PASSWORD, candidate)


def safe_next(target: str | None, default: str) -> str:
    """登录后、改完东西后跳去哪：只认本站路径。「//别的站」和反斜杠开头的，浏览器会当成另一个网站，不认。"""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return default


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


# ------------------------------------------------------------------ CSRF
# 会话 cookie 是 SameSite=Lax，浏览器本来就不会把它带上跨站 POST，所以这层是
# 冗余保护。留着的理由是账号管理页能改额度、停用账号，值得多一道；成本也就是
# 表单里多一个隐藏域。
def csrf_token() -> str:
    """取本会话的令牌，没有就现生成一个（模板里当全局函数用）。"""
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_ok() -> bool:
    expected = session.get("csrf") or ""
    return bool(expected) and _same(expected, request.form.get("csrf") or "")


def csrf_protect(view):
    """给 POST 视图用。放在 login_required 里侧，先认人再验令牌。"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not csrf_ok():
            flash("表单已过期（会话可能已重启），请刷新页面后重试。", "error")
            return redirect(request.referrer or url_for("main.index"))
        return view(*args, **kwargs)

    return wrapper


def login_source() -> str:
    """登录表单是从哪个网站提交过来的：别的平台「一键登录」替用户提交时，浏览器带的 Origin（没有就看 Referer）
    是那个平台；本站登录页提交的返回空串。这个头能伪造（curl 想写什么写什么），只当参考——IP 才靠得住。"""
    raw = request.headers.get("Origin") or request.headers.get("Referer") or ""
    host = urlsplit(raw).netloc if raw and raw != "null" else ""
    return "" if not host or host == request.host else host


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("main.index"))

    ip = client_ip()
    if request.method == "POST":
        # 每次提交都记一行（设置页的「登录记录」）：不记密码；用户名照记，失败的也记输了什么
        agent = request.headers.get("User-Agent", "")
        source = login_source()
        username = (request.form.get("username") or "").strip()
        remaining = lockout_remaining(ip)
        if remaining:
            login_log.record("locked", user=username, ip=ip, agent=agent, source=source)
            flash(f"登录失败次数过多，请在 {remaining} 秒后重试。", "error")
            return render_template("login.html"), 429

        password = request.form.get("password") or ""
        right_user = _same(config.AUTH_USERNAME, username)
        if right_user and password_matches(password):
            clear_failures(ip)
            login_log.record("ok", user=username, ip=ip, agent=agent, source=source)
            session.permanent = True
            session["user"] = username
            # 只允许跳回本站路径，避免开放重定向
            return redirect(safe_next(request.form.get("next"), url_for("main.index")))

        record_failure(ip)
        login_log.record("fail" if right_user else "user", user=username, ip=ip, agent=agent, source=source)
        flash("用户名或密码错误。", "error")
        # 返回 401 而不是 200：这样 Nginx 的访问日志里失败登录是可识别的，
        # fail2ban 才能据此封 IP。不带 WWW-Authenticate，所以浏览器不会弹
        # 系统认证框，照常显示这个登录页。
        return render_template("login.html"), 401

    return render_template("login.html")


@bp.route("/logout")
def logout():
    if session.get("user"):
        login_log.record("logout", user=session["user"], ip=client_ip(), agent=request.headers.get("User-Agent", ""))
    session.clear()
    return redirect(url_for("auth.login"))
