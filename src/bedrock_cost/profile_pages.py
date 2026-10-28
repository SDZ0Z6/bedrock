"""自己的资料页（/me）：显示名、头像、在线时间、登录记录；只读用户在这里改自己的密码（auth.password）。
侧边栏底下点自己的名字进来，管理员和只读用户都有。管理员在用户管理里点开别人，看到的是同一个页面
（people_pages.profile 也用 page()），只是不能替人改名字和头像。

还有在线时间的报到（POST /presence）：侧边栏那一套页面每 30 秒报一次（app.js），见 presence。
它不续登录有效期（create_app 里的 _Sessions），页面开着不动，到点照样要重新登录。
概览欢迎条右上角换背景（POST /me/scene，mascots.js 用 fetch 发）也记在自己的资料里；每个页面底下那只小家伙
出不出来也是（POST /me/pet：资料页上的开关是普通表单，右键点它「收起小家伙」是 pet.js 用 fetch 发的）；概览顶上的
小城开不开也是（POST /me/city，资料页上的开关，普通表单）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from . import avatars, login_log, presence, users, welcome
from .auth import client_ip, csrf_ok, csrf_protect, current_user, fetched, login_required
from .views import flash_result, page_meta

bp = Blueprint("profile", __name__)

LOGIN_SHOW = 30   # 资料页上列最近多少条登录记录
# 头像框旁边一排现成的表情，点一下填进去；想用别的直接在框里打
PEOPLE_EMOJIS = ("😎", "🐱", "🐶", "🦊", "🐼", "🐧", "🌟", "🍀", "☕", "🚀")


def login_entries(name: str) -> list[login_log.Entry]:
    """这个人的登录记录（登录记录里还留着的），新的在前。失败的也在：有人拿这个用户名试过密码，这里看得到。"""
    wanted = (name or "").casefold()
    return [entry for entry in login_log.recent(limit=login_log.MAX_LINES) if entry.user.casefold() == wanted]


def page(person: users.Person, own: bool, user: users.User | None = None):
    """画一个人的资料页。own：看的是自己（能改名字、头像，只读用户能改密码）；否则是管理员在看别人。"""
    entries = login_entries(person.name)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    month = [entry for entry in entries if entry.when >= since]
    days = presence.recent_days(person.name, 7)
    # 柱子按这几天里最多的那天画，至少按 1 小时：不然在线一分钟也是满满一根，看着像待了一整天
    most = max([seconds for _, seconds in days] + [3600])
    look = person.look
    return render_template(
        "profile.html", person=person, own=own, user=user,
        active_page="me" if own else "people",
        days=days, most=most, today_seconds=days[-1][1], week_seconds=sum(seconds for _, seconds in days),
        entries=entries[:LOGIN_SHOW], login_total=len(entries),
        login_summary={"ok": sum(1 for entry in month if entry.kind == "ok"),
                       "failed": sum(1 for entry in month if entry.failed)},
        my_ip=client_ip(), emojis=PEOPLE_EMOJIS, tones=range(avatars.TONES),
        auto_tone=avatars.tone_for(person.name), max_display=users.MAX_DISPLAY,
        look_color="" if look.avatar_color is None else str(look.avatar_color),
        min_password=users.MIN_PASSWORD, **page_meta(),
    )


@bp.route("/me")
@login_required
def me():
    person = current_user()
    return page(person, own=True, user=None if person.admin else users.find(person.name))


@bp.route("/me", methods=["POST"])
@login_required
@csrf_protect
def save():
    """存自己的显示名和头像。只读用户也能存（auth.VIEWER_POSTS）——这是自己的资料，不是台账。"""
    me = current_user()
    try:
        look = users.clean_look(request.form.get("display"), request.form.get("avatar_text"),
                                request.form.get("avatar_color"))
        # 挑过的背景、小家伙、小城不动
        person = users.save_look(me, replace(look, scene=me.look.scene, pet=me.look.pet, city=me.look.city))
    except users.UserError as exc:
        flash_result("资料没保存", text=str(exc), tone="error")
        return redirect(url_for("profile.me"))
    flash_result("资料已保存", person.label, "侧边栏、概览上的欢迎语换成新的名字和头像了。")
    return redirect(url_for("profile.me"))


@bp.route("/me/scene", methods=["POST"], endpoint="scene")
@login_required
def scene():
    """换概览欢迎条的背景，记在自己的资料里；空着是自动（按时间、季节挑）。回 JSON：{ok, scene, kind, label}，
    kind 是现在该画哪个（选了自动就是自动挑出来的那个）。令牌不对只回 403，不弹「表单已过期」。"""
    if not csrf_ok():
        return jsonify(ok=False), 403
    chosen = (request.form.get("scene") or "").strip()
    if chosen and chosen not in welcome.SCENES:
        return jsonify(ok=False, message="没有这个背景。"), 400
    me = current_user()
    try:
        users.save_look(me, replace(me.look, scene=chosen))
    except users.UserError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    kind = chosen or welcome.auto_scene(datetime.now())
    return jsonify(ok=True, scene=chosen, kind=kind, label=welcome.SCENES[kind])


@bp.route("/me/city", methods=["POST"], endpoint="city")
@login_required
@csrf_protect
def city():
    """概览顶上的小城开着（city=on）还是关了（city=off，换回原来的像素风景），记在自己的资料里。资料页上的开关是
    普通表单：存好回资料页，弹一句。"""
    wanted = (request.form.get("city") or "").strip()
    if wanted not in ("on", "off"):
        flash_result("没改成", text="页面可能已经过期，刷新后再试。", tone="error")
        return redirect(url_for("profile.me"))
    me = current_user()
    try:
        users.save_look(me, replace(me.look, city="" if wanted == "on" else users.CITY_OFF))
    except users.UserError as exc:
        flash_result("没改成", text=str(exc), tone="error")
        return redirect(url_for("profile.me"))
    if wanted == "on":
        flash_result("小城开回来了", text="概览最上面是每个客户的院子，有调用的仓库，小家伙在往公司楼搬箱子。")
    else:
        flash_result("小城关了", text="概览最上面换回原来的像素风景；想看了在这儿再打开。")
    return redirect(url_for("profile.me"))


@bp.route("/me/pet", methods=["POST"], endpoint="pet")
@login_required
def pet():
    """每个页面底下那只小家伙出来（pet=on）还是收起来（pet=off），记在自己的资料里。资料页上的开关是普通表单：
    存好回资料页，弹一句；右键点它「收起小家伙」是 fetch 发的：回 JSON {ok, pet}，令牌不对只回 403。"""
    if not csrf_ok():
        if fetched():
            return jsonify(ok=False), 403
        flash("表单已过期（会话可能已重启），请刷新页面后重试。", "error")
        return redirect(url_for("profile.me"))
    wanted = (request.form.get("pet") or "").strip()
    if wanted not in ("on", "off"):
        if fetched():
            return jsonify(ok=False, message="开还是关？页面可能已经过期，刷新后再试。"), 400
        flash_result("没改成", text="页面可能已经过期，刷新后再试。", tone="error")
        return redirect(url_for("profile.me"))
    me = current_user()
    try:
        users.save_look(me, replace(me.look, pet="" if wanted == "on" else users.PET_OFF))
    except users.UserError as exc:
        if fetched():
            return jsonify(ok=False, message=str(exc)), 400
        flash_result("没改成", text=str(exc), tone="error")
        return redirect(url_for("profile.me"))
    if fetched():
        return jsonify(ok=True, pet=wanted)
    if wanted == "on":
        flash_result("小家伙回来了", text="每个页面底下都有它：鼠标放上去它说话，点一下蹦一下，按住能拖着扔。")
    else:
        flash_result("小家伙收起来了", text="哪个页面都不出来了；想它了在这儿再打开。")
    return redirect(url_for("profile.me"))


@bp.route("/presence", methods=["POST"], endpoint="presence")
@login_required
def presence_ping():
    """在线时间的报到，回今天累计多久。令牌不对只回 403，不弹「表单已过期」：每个页面都在后台报，弹了莫名其妙。"""
    if not csrf_ok():
        return jsonify(ok=False), 403
    seconds = presence.touch(current_user().name)
    return jsonify(ok=True, seconds=seconds, label=presence.label(seconds))

