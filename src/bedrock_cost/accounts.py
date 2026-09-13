"""账号管理页：新增 / 修改 / 停用。

写台账的逻辑全在 excel_source 里，这里只负责表单进出和提示文案。

凭证是单向的：只有新建时收一次 AK/SK，之后任何地方都不回显、也不可改。
要换凭证的做法是「停用旧账号 + 新建一条」——AWS 换 AK 本来就是签发新的、
作废旧的，硬做原地编辑反而容易出现半新半旧的状态。

删除是软删（ENABLED 列置 FALSE）。停用的账号从四个查询页彻底消失，但行还在，
随时可以恢复，历史记录和凭证也都留着。
"""

from __future__ import annotations

from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from . import config, excel_source
from .auth import csrf_protect, login_required
from .excel_source import Account, ExcelSourceError, load_accounts
from .views import page_meta

bp = Blueprint("accounts", __name__, url_prefix="/accounts")


def _actor() -> str:
    return session.get("user") or ""


def _load_all() -> tuple[list[Account], str | None]:
    """读全量账号（含已停用）。

    这里一律 force=True：这一页是编辑入口，必须看到文件此刻的真实内容，
    不能因为 mtime 缓存把别人刚改的结果藏起来。读的是本地文件，不花钱。
    """
    try:
        return load_accounts(force=True, include_disabled=True), None
    except ExcelSourceError as exc:
        return [], str(exc)
    except Exception as exc:  # 兜底，避免整页 500
        return [], f"{type(exc).__name__}: {exc}"


def _render(**extra):
    """列表页统一入口：正常打开和「提交失败回填」都走这里。"""
    accounts, fatal = _load_all()
    context = {
        "active_page": "accounts",
        "accounts": accounts,
        "tag_key": config.TAG_KEY,  # TAG 列的占位提示
        "today": date.today(),      # 启用日期输入框的 max，挡住未来日期
        "enabled_count": sum(1 for a in accounts if a.enabled),
        "fatal": fatal,
        "notes": [],
        "errors": [],
        # 新建表单的回填值
        "create_form": {},
        "open_create": False,
        # 某一行编辑失败时的回填值
        "edit_key": "",
        "edit_form": {},
    }
    context.update(extra)
    context.update(page_meta())
    return render_template("accounts.html", **context)


@bp.route("/")
@login_required
def index():
    return _render()


@bp.route("/create", methods=["POST"])
@login_required
@csrf_protect
def create():
    existing, fatal = _load_all()
    if fatal:
        flash(fatal, "error")
        return redirect(url_for("accounts.index"))

    # 查重要带上已停用的账号：停用不等于账号 ID 可以被别人占用
    data, errors = excel_source.validate(request.form, existing, creating=True)
    if errors:
        return _render(errors=errors, create_form=request.form, open_create=True), 400

    try:
        note = excel_source.create_account(data, actor=_actor())
    except ExcelSourceError as exc:
        return _render(errors=[str(exc)], create_form=request.form, open_create=True), 409

    flash(f"{note}。", "ok")
    return redirect(url_for("accounts.index"))


@bp.route("/update", methods=["POST"])
@login_required
@csrf_protect
def update():
    key = (request.form.get("key") or "").strip()
    existing, fatal = _load_all()
    if fatal:
        flash(fatal, "error")
        return redirect(url_for("accounts.index"))

    if not any(a.key == key for a in existing):
        flash("这个账号已经不在台账里了，页面可能已过期。已重新加载。", "error")
        return redirect(url_for("accounts.index"))

    others = [a for a in existing if a.key != key]
    data, errors = excel_source.validate(request.form, others, creating=False)
    if errors:
        return _render(errors=errors, edit_key=key, edit_form=request.form), 400

    try:
        note = excel_source.update_account(key, data, actor=_actor())
    except ExcelSourceError as exc:
        return _render(errors=[str(exc)], edit_key=key, edit_form=request.form), 409

    if note:
        flash(f"{note}。", "ok")
    else:
        flash("没有任何字段发生变化，台账未改动。", "warn")
    return redirect(url_for("accounts.index"))


@bp.route("/toggle", methods=["POST"])
@login_required
@csrf_protect
def toggle():
    key = (request.form.get("key") or "").strip()
    enabled = request.form.get("enabled") == "1"

    try:
        note = excel_source.set_enabled(key, enabled, actor=_actor())
    except ExcelSourceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.index"))

    flash(f"{note}。" if note else "状态本来就是这样，没有改动。", "ok" if note else "warn")
    return redirect(url_for("accounts.index"))
