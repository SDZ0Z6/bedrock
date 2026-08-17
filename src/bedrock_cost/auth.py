"""登录。

单账号口令 + 按 IP 的失败锁定，按本地自用场景设计。要对外提供访问需要换成
真正的用户体系，并启用 HTTPS 与 SESSION_COOKIE_SECURE。
"""

from __future__ import annotations

import hmac
import threading
import time
from functools import wraps

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

from . import config

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


def password_matches(candidate: str) -> bool:
    if config.AUTH_PASSWORD_HASH:
        return check_password_hash(config.AUTH_PASSWORD_HASH, candidate)
    if not config.AUTH_PASSWORD:
        return False
    return hmac.compare_digest(config.AUTH_PASSWORD, candidate)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("main.index"))

    ip = client_ip()
    if request.method == "POST":
        remaining = lockout_remaining(ip)
        if remaining:
            flash(f"登录失败次数过多，请在 {remaining} 秒后重试。", "error")
            return render_template("login.html"), 429

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if hmac.compare_digest(config.AUTH_USERNAME, username) and password_matches(password):
            clear_failures(ip)
            session.permanent = True
            session["user"] = username
            target = request.form.get("next") or url_for("main.index")
            # 只允许跳回本站路径，避免开放重定向
            if not target.startswith("/"):
                target = url_for("main.index")
            return redirect(target)

        record_failure(ip)
        flash("用户名或密码错误。", "error")
        # 返回 401 而不是 200：这样 Nginx 的访问日志里失败登录是可识别的，
        # fail2ban 才能据此封 IP。不带 WWW-Authenticate，所以浏览器不会弹
        # 系统认证框，照常显示这个登录页。
        return render_template("login.html"), 401

    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
