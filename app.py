"""Bedrock 成本监控平台 —— Flask 入口。

启动：
    python app.py
然后浏览器打开 http://127.0.0.1:5000
"""

from __future__ import annotations

import calendar
import hmac
import sys
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

import config
import cost_explorer
from excel_source import ExcelSourceError
from report import build_report

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_HOURS),
    JSON_AS_ASCII=False,
)

# Cost Explorer 大约有 14 个月的历史数据
CE_HISTORY_MONTHS = 14


# --------------------------------------------------------------- 登录
_attempts_lock = threading.Lock()
_attempts: dict[str, list] = {}  # ip -> [失败次数, 首次失败时间]


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


def _lockout_remaining(ip: str) -> int:
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


def _record_failure(ip: str) -> None:
    with _attempts_lock:
        record = _attempts.get(ip)
        if not record or time.time() - record[1] > config.LOCKOUT_SECONDS:
            _attempts[ip] = [1, time.time()]
        else:
            record[0] += 1


def _clear_failures(ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(ip, None)


def _password_matches(candidate: str) -> bool:
    if config.AUTH_PASSWORD_HASH:
        return check_password_hash(config.AUTH_PASSWORD_HASH, candidate)
    if not config.AUTH_PASSWORD:
        return False
    return hmac.compare_digest(config.AUTH_PASSWORD, candidate)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("index"))

    ip = _client_ip()
    if request.method == "POST":
        remaining = _lockout_remaining(ip)
        if remaining:
            flash(f"登录失败次数过多，请在 {remaining} 秒后重试。", "error")
            return render_template("login.html"), 429

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        name_ok = hmac.compare_digest(config.AUTH_USERNAME, username)
        if name_ok and _password_matches(password):
            _clear_failures(ip)
            session.permanent = True
            session["user"] = username
            target = request.form.get("next") or url_for("index")
            # 只允许跳回本站路径，避免开放重定向
            if not target.startswith("/"):
                target = url_for("index")
            return redirect(target)

        _record_failure(ip)
        flash("用户名或密码错误。", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------- 日期区间
def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _month_shift(anchor: date, months: int) -> date:
    """按月偏移，落到该月 1 号。"""
    total = anchor.year * 12 + (anchor.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def preset_range(name: str, today: date) -> tuple[date, date] | None:
    if name == "mtd":
        return today.replace(day=1), today
    if name == "last_month":
        first_last = _month_shift(today, -1)
        last_day = calendar.monthrange(first_last.year, first_last.month)[1]
        return first_last, first_last.replace(day=last_day)
    if name == "last7":
        return today - timedelta(days=6), today
    if name == "last30":
        return today - timedelta(days=29), today
    if name == "ytd":
        return date(today.year, 1, 1), today
    return None


def resolve_range(today: date) -> tuple[date, date, list[str]]:
    """从查询参数解析日期区间，返回 (开始, 结束, 提示信息)。"""
    notes: list[str] = []
    preset = (request.args.get("preset") or "").strip()
    chosen = preset_range(preset, today) if preset else None

    if chosen:
        start, end = chosen
    else:
        start = _parse_date(request.args.get("start"))
        end = _parse_date(request.args.get("end"))
        if request.args.get("start") and start is None:
            notes.append("开始日期格式无法识别，已使用本月 1 号。")
        if request.args.get("end") and end is None:
            notes.append("结束日期格式无法识别，已使用今天。")
        start = start or today.replace(day=1)
        end = end or today

    if start > end:
        start, end = end, start
        notes.append("开始日期晚于结束日期，已自动调换。")
    if end > today:
        end = today
        notes.append("结束日期不能晚于今天，已调整为今天。")

    earliest = _month_shift(today, -CE_HISTORY_MONTHS)
    if start < earliest:
        start = earliest
        notes.append(
            f"Cost Explorer 仅保留约 {CE_HISTORY_MONTHS} 个月历史数据，"
            f"开始日期已调整为 {earliest.isoformat()}。"
        )
    return start, end, notes


# --------------------------------------------------------------- 主页
@app.route("/")
@login_required
def index():
    today = date.today()
    start, end, notes = resolve_range(today)
    refresh = request.args.get("refresh") == "1"

    report = None
    fatal = None
    try:
        report = build_report(start, end, refresh=refresh)
    except ExcelSourceError as exc:
        fatal = str(exc)
    except Exception as exc:  # 兜底，避免整页 500
        fatal = f"{type(exc).__name__}: {exc}"

    return render_template(
        "index.html",
        report=report,
        fatal=fatal,
        notes=notes,
        start=start,
        end=end,
        today=today,
        active_preset=(request.args.get("preset") or "").strip(),
        tag_key=config.TAG_KEY,
        cost_metric=config.COST_METRIC,
        service_scope=("、".join(config.SERVICE_FILTER) if config.SERVICE_FILTER else "账号全部服务"),
        cache_ttl_minutes=round(config.CACHE_TTL / 60, 1),
        excel_name=config.EXCEL_PATH.name,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.route("/cache/clear")
@login_required
def clear_cache():
    cost_explorer.clear_cache()
    flash("已清空成本缓存，下一次查询会重新调用 Cost Explorer。", "ok")
    return redirect(request.referrer or url_for("index"))


# --------------------------------------------------------------- 模板过滤器
@app.template_filter("money")
def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{config.CURRENCY_SYMBOL}{value:,.2f}"


@app.template_filter("pct")
def pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}%"


@app.template_filter("ratio")
def ratio(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


if __name__ == "__main__":
    # Windows 控制台默认可能是 cp1252/cp936，直接 print 中文会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    for warning in config.startup_warnings():
        print(f"[配置提醒] {warning}")
    print(f"\n  Bedrock 成本监控平台  ->  http://{config.HOST}:{config.PORT}\n")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
