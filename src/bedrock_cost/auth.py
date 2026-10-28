"""登录和权限。

两种人：
  · admin：.env 里的 AUTH_USERNAME / AUTH_PASSWORD（或 AUTH_PASSWORD_HASH），只有这一个，什么都能改，也管用户。
  · 只读用户：admin 在「用户管理」里建的（见 users）。能看概览、客户、账号的六个页签，能导出 CSV；
    改不了任何东西，看不到账号管理、用户管理和设置，也不能「刷新数据」、清空缓存。
两种人都有自己的资料页（/me：显示名、头像、在线时间、登录记录），只读用户在那里改自己的密码。

挡只读用户的是 _gate，每个请求都过一遍，以后加的页面、按钮不用一个个记得挡：不是 GET 的一律不让做（只放过
改自己的密码、改自己的资料和背景、在线时间的报到），账号管理、用户管理、设置三个蓝图和清空缓存去不了，网址上带
refresh 的拿掉再跳一次（看缓存里的数）。页面上那些按钮也藏起来（模板里的 is_admin()），这一层是兜底。

会话里存 user、role（admin / viewer），只读用户还有 stamp：重置密码、停用、删除以后对不上，当没登录。
按 IP 的失败锁定两种人共用。对外提供访问要启用 HTTPS 与 SESSION_COOKIE_SECURE。
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from functools import wraps
from urllib.parse import urlencode, urlsplit

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from . import config, excel_source, login_log, users

bp = Blueprint("auth", __name__)

ADMIN, VIEWER = "admin", "viewer"
# 只读用户去不了的：整个账号管理、用户管理、设置；GET 也会改东西的清空缓存
ADMIN_BLUEPRINTS = frozenset({"accounts", "people", "settings"})
ADMIN_ENDPOINTS = frozenset({"main.clear_cache"})
# 只读用户也能提交的：改自己的密码、改自己的资料（显示名、头像、概览的背景、页面底下的小家伙）、在线时间的报到
VIEWER_POSTS = frozenset({"auth.password", "profile.save", "profile.scene", "profile.pet", "profile.city",
                          "profile.presence"})
READ_ONLY = "只读用户不能改东西"

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


# ------------------------------------------------------------------ 谁在用
def current_user() -> users.Person | None:
    """当前登录的人：名字、是不是管理员、显示名和头像（模板里也叫 current_user()）。"""
    return g.get("user")


def is_admin() -> bool:
    """模板里藏按钮用：只有 admin 看得到改东西的按钮、账号管理和设置的入口、「刷新数据」。"""
    user = g.get("user")
    return bool(user and user.admin)


def fetched() -> bool:
    """页面脚本用 fetch 发的（要 JSON 回话），不是整页提交的表单。"""
    return request.headers.get("X-Requested-With") == "fetch"


@bp.before_app_request
def _load_user():
    """每个请求先认人：会话里的人还算不算数；是只读用户的话，这件事能不能做（_gate）。"""
    g.user = None
    name = session.get("user")
    if not name:
        return None
    role = session.get("role") or ADMIN      # 有只读用户之前登录的会话只存了 user，那时候只有 admin
    if role == ADMIN:
        person = users.admin_person(name) if _same(config.AUTH_USERNAME, name) else None
    else:
        user = users.session_user(name, session.get("stamp") or "")
        person = user.person if user else None
    if person is None:
        # 重置了密码、停用、删掉了（admin 是 .env 里换了用户名）：当没登录，login_required 带去登录页
        session.clear()
        flash("登录已失效，请重新登录。" if role == ADMIN
              else "登录已失效：密码被重置了，或者这个用户被停用、删掉了。", "warn")
        return None
    g.user = person
    return None if person.admin else _gate()


def _back() -> str:
    """挡下来以后回哪：从本站哪一页提交的就回哪一页，别的回概览。"""
    parts = urlsplit(request.referrer or "")
    if parts.netloc == request.host and parts.path:
        return safe_next(parts.path + (f"?{parts.query}" if parts.query else ""), url_for("main.index"))
    return url_for("main.index")


def _gate():
    """只读用户能做什么。返回 None 是放行。"""
    endpoint = request.endpoint or ""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if endpoint in VIEWER_POSTS:
            return None
        if fetched():
            return jsonify(ok=False, message=READ_ONLY + "。"), 403
        flash({"title": READ_ONLY, "sub": "", "text": "你登录的是只读用户：能看、能导出，改动要找管理员。"}, "error")
        return redirect(_back())
    if endpoint.partition(".")[0] in ADMIN_BLUEPRINTS or endpoint in ADMIN_ENDPOINTS:
        flash({"title": "这一页只有管理员能看", "sub": "",
               "text": "你登录的是只读用户：能看概览、客户和账号，看不到账号管理、用户管理和设置。"}, "warn")
        return redirect(url_for("main.index"))
    if "refresh" in request.args:
        # 「刷新数据」要重新查 Cost Explorer（花钱），只读用户看缓存里的数：拿掉 refresh 再来一次
        kept = [(key, value) for key, value in request.args.items(multi=True) if key != "refresh"]
        return redirect(request.path + (f"?{urlencode(kept)}" if kept else ""))
    return None


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    """只有 admin 能用的（用户管理）。放在 login_required 里侧。_gate 已经挡过只读用户，这里再挡一道。"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            abort(403)
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
        # admin 是 .env 里的那一个（用户名分大小写，和以前一样）；别的名字去只读用户里找（不分大小写）
        if _same(config.AUTH_USERNAME, username):
            result, role, stamp = ("ok" if password_matches(password) else "fail"), ADMIN, ""
        else:
            result, user = users.authenticate(username, password)
            role, stamp = VIEWER, user.stamp if user else ""
            username = user.name if user else username
        if result == "ok":
            clear_failures(ip)
            login_log.record("ok", user=username, ip=ip, agent=agent, source=source)
            session.permanent = True
            session["user"] = username
            session["role"] = role
            if stamp:
                session["stamp"] = stamp
            else:
                session.pop("stamp", None)
            # 只允许跳回本站路径，避免开放重定向
            return redirect(safe_next(request.form.get("next"), url_for("main.index")))
        if result == "disabled":
            # 密码是对的，不算失败次数；只有密码对了才说是停用了
            login_log.record("disabled", user=username, ip=ip, agent=agent, source=source)
            flash("这个用户已停用，找管理员恢复。", "error")
            return render_template("login.html"), 403

        record_failure(ip)
        login_log.record(result, user=username, ip=ip, agent=agent, source=source)
        flash("用户名或密码错误。", "error")
        # 返回 401 而不是 200：这样 Nginx 的访问日志里失败登录是可识别的，
        # fail2ban 才能据此封 IP。不带 WWW-Authenticate，所以浏览器不会弹
        # 系统认证框，照常显示这个登录页。
        return render_template("login.html"), 401

    return render_template("login.html")


@bp.route("/password", methods=["POST"])
@login_required
@csrf_protect
def password():
    """只读用户改自己的密码（自己的资料页上）。admin 的密码在 .env 里改。
    页面上是 fetch 发的，回 JSON；没开 JS 整页提交的，回原来那一页说结果。"""
    who = current_user()
    back = safe_next(request.form.get("next"), url_for("main.index"))

    def answer(ok: bool, title: str, text: str = "", status: int = 200):
        if fetched():
            return jsonify(ok=ok, message=title, text=text), status
        flash({"title": title, "sub": "", "text": text}, "ok" if ok else "error")
        return redirect(back)

    if who.admin:
        return answer(False, "admin 的密码在 .env 里改", "改 AUTH_PASSWORD（或 AUTH_PASSWORD_HASH）以后重启服务。", 400)
    ip = client_ip()
    remaining = lockout_remaining(ip)
    if remaining:
        return answer(False, f"失败次数过多，请在 {remaining} 秒后重试。", status=429)
    try:
        user = users.change_password(who.name, request.form.get("current"), request.form.get("new_password"),
                                     request.form.get("again"))
    except users.WrongPassword as exc:
        record_failure(ip)          # 和登录失败一样算次数：不让人拿这个框慢慢试
        return answer(False, str(exc), status=400)
    except users.UserError as exc:
        return answer(False, str(exc), status=400)
    session["stamp"] = user.stamp   # 这个会话接着用；别处登录着的 stamp 对不上，退出
    excel_source.audit_note(f"用户 {user.name} 改了自己的密码", user.name)
    return answer(True, "密码改好了", "下次用新密码登录；别处登录着的已经退出。")


@bp.route("/logout")
def logout():
    if session.get("user"):
        login_log.record("logout", user=session["user"], ip=client_ip(), agent=request.headers.get("User-Agent", ""))
    session.clear()
    return redirect(url_for("auth.login"))
