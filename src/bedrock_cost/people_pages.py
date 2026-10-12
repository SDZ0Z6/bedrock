"""用户管理（/users/）：只有管理员进得来（只读用户在 auth._gate 就挡掉了）。

列表里是所有人：管理员（.env 里的那一个，列在第一行）和只读用户，每人一行：头像、显示名、用户名、备注、
今天在线多久、最近一次登录。点名字打开这个人的资料页（在线时间、登录记录，和本人看到的一样，只是不能替人
改名字、头像），在那里也能重置密码、停用、删除。

新建、重置密码：密码可以自己填（至少 users.MIN_PASSWORD 位），不填就由系统生成；都只在弹窗里显示这一次
（页面脚本 fetch 的，回 JSON，密码不进网址、不进会话、不进 flash）。停用 / 恢复、删除是普通表单。
这些都记进操作日志（「用户」类），密码哪儿都不记。
"""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from . import config, excel_source, login_log, presence, users
from .auth import admin_required, csrf_protect, current_user, login_required, safe_next
from .profile_pages import page
from .views import flash_result, page_meta

bp = Blueprint("people", __name__, url_prefix="/users")


def _actor() -> str:
    who = current_user()
    return who.name if who else ""


def _chosen_password() -> str | None:
    """弹窗里选了「自己填」就用填的（空的也交给 users 去说「至少 8 位」）；选「系统生成」返回 None。"""
    return (request.form.get("password") or "") if request.form.get("password_mode") == "custom" else None


def _last_logins() -> dict[str, datetime]:
    """每个人最近一次登录成功（登录记录里还留着的那些）：用户名（小写）-> 时间。"""
    out: dict[str, datetime] = {}
    for entry in login_log.recent(limit=login_log.MAX_LINES):
        if entry.kind == "ok":
            out.setdefault(entry.user.casefold(), entry.when)
    return out


def _back(default: str) -> str:
    return safe_next(request.form.get("next"), default)


@bp.route("/")
@login_required
@admin_required
def index():
    people = users.load()
    return render_template(
        "people.html", active_page="people", people=people, admin=users.admin_person(config.AUTH_USERNAME),
        last_logins=_last_logins(), online=presence.today_all(), presence_label=presence.label,
        disabled_count=sum(1 for user in people if user.disabled), name_rule=users.NAME_RULE,
        note_limit=users.MAX_NOTE, min_password=users.MIN_PASSWORD, **page_meta(),
    )


@bp.route("/", methods=["POST"], endpoint="create")
@login_required
@admin_required
@csrf_protect
def create():
    """新建只读用户，回 JSON：{ok, name, password, custom}。密码只在这一次回话里。"""
    custom = _chosen_password()
    try:
        user, password = users.create(request.form.get("name"), request.form.get("note"), custom)
    except users.UserError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    excel_source.audit_note(f"新建用户 {user.name}（只读）" + (f"，备注：{user.note}" if user.note else ""), _actor())
    return jsonify(ok=True, name=user.name, password=password, custom=custom is not None,
                   profile=url_for("people.profile", name=user.name))


@bp.route("/<name>")
@login_required
@admin_required
def profile(name: str):
    """管理员看一个人的资料页。看的是自己就去 /me；没有这个用户回列表说一声。"""
    if name.strip().casefold() == (config.AUTH_USERNAME or "").casefold():
        return redirect(url_for("profile.me"))
    user = users.find(name)
    if user is None:
        flash_result("没有这个用户", name, "可能已经删掉了。", tone="error")
        return redirect(url_for("people.index"))
    return page(user.person, own=False, user=user)


@bp.route("/<name>/reset", methods=["POST"])
@login_required
@admin_required
@csrf_protect
def reset(name: str):
    """重置密码，回 JSON：{ok, name, password, custom}。这个用户在别处登录着的全退出。"""
    custom = _chosen_password()
    try:
        user, password = users.reset_password(name, custom)
    except users.UserError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    excel_source.audit_note(f"重置用户 {user.name} 的密码", _actor())
    return jsonify(ok=True, name=user.name, password=password, custom=custom is not None)


@bp.route("/<name>/toggle", methods=["POST"])
@login_required
@admin_required
@csrf_protect
def toggle(name: str):
    """停用（登录着的马上退出）/ 恢复（用原来的密码登录）。做完回原来那一页（列表或者这个人的资料页）。"""
    disable = request.form.get("action") == "disable"
    back = _back(url_for("people.index"))
    try:
        user = users.set_disabled(name, disable)
    except users.UserError as exc:
        flash_result("没改成", text=str(exc), tone="error")
        return redirect(back)
    if disable:
        excel_source.audit_note(f"停用用户 {user.name}", _actor())
        flash_result("已停用", user.name, "登录着的已经退出，登不进来了。恢复以后用原来的密码登录。")
    else:
        excel_source.audit_note(f"恢复用户 {user.name}", _actor())
        flash_result("已恢复", user.name, "用原来的密码就能登录。")
    return redirect(back)


@bp.route("/<name>/delete", methods=["POST"])
@login_required
@admin_required
@csrf_protect
def delete(name: str):
    try:
        user = users.delete(name)
    except users.UserError as exc:
        flash_result("没删掉", text=str(exc), tone="error")
        return redirect(url_for("people.index"))
    presence.forget(user.name)
    excel_source.audit_note(f"删除用户 {user.name}", _actor())
    flash_result("已删除", user.name, "登录着的已经退出。登录记录、操作日志里以前的记录还在。")
    return redirect(url_for("people.index"))
