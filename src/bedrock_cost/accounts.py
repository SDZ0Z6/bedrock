"""账号管理页：新增 / 修改 / 停用。

写台账的逻辑全在 excel_source 里，这里只负责表单进出和提示文案。

凭证是单向的：只有新建时收一次 AK/SK，之后任何地方都不回显、也不可改。
要换凭证的做法是「停用旧账号 + 新建一条」——AWS 换 AK 本来就是签发新的、
作废旧的，硬做原地编辑反而容易出现半新半旧的状态。

停用是软删（ENABLED 列置 FALSE）。停用的账号从四个查询页彻底消失，但行还在，
随时可以恢复，历史记录和凭证也都留着。删除是把整行清空、不能恢复，所以要重新输一遍
登录密码确认（输错和登录页共用同一个失败锁定）。

新增、停用、恢复账号之后，按 TG 开关给这个账号的群发一张通知卡片（_notify →
alerts.notify_account）。台账先写好，通知发不出去只另起一条警告，不回滚改动。

告警邮箱（平台 / 地址 / 密码）也在弹窗里填，开关在表格的「邮件告警」列，和 TG 一样。
邮箱密码和 SK 一样不回显：修改时留空表示不改，页面上只说「已保存」。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from . import alerts, config, excel_source, mail_inbox, mail_rules, telegram
from .auth import (
    clear_failures,
    client_ip,
    csrf_protect,
    lockout_remaining,
    login_required,
    password_matches,
    record_failure,
)
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


def _form_values(form) -> dict:
    """把提交上来的表单变成模板能直接回填的 dict。

    群组 ID 是一行一个框、同名字段有多个；MultiDict.get 只给第一个，直接把
    request.form 交给模板会在校验失败回填时丢掉后面几行。
    """
    values = {key: form.get(key) for key in form.keys()}
    values["tg_chat_ids"] = excel_source.form_chat_ids(form)
    # 邮箱密码不回填：和 SK 一样，只在提交的那一下经过服务器，不再写回页面
    values.pop("mail_password", None)
    return values


def _render(**extra):
    """列表页统一入口：正常打开和「提交失败回填」都走这里。"""
    accounts, fatal = _load_all()
    context = {
        "active_page": "accounts",
        "accounts": accounts,
        "tag_key": config.TAG_KEY,  # TAG 列的占位提示
        "today": date.today(),      # 启用日期输入框的 max，挡住未来日期
        # 没配 Token 时开关照样能存，但页面要说清楚「存了也不会发」
        "tg_configured": telegram.configured(),
        # 「发测试消息」的结果：{"ok": bool, "message": str}，显示在重新打开的弹窗里
        "tg_test": None,
        "max_tg_chats": excel_source.MAX_TG_CHATS,
        # 告警邮箱：平台下拉框、「测试连接」的结果（没开 JS 时整页提交才用得到）
        "mail_providers": mail_inbox.PROVIDERS,
        "mail_default_provider": mail_inbox.DEFAULT_PROVIDER,
        "mail_test": None,
        "mail_fixed_chats": config.MAIL_ALERT_CHAT_IDS,
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
        # 删除时密码没对上：重新打开那一行的删除确认窗，写明原因
        "delete_key": "",
        "delete_error": "",
    }
    context.update(extra)
    # 重新打开的弹窗停在哪一页：有错就停在第一条错误所在的那页（有错的页签都标红点），
    # 刚点过「发测试消息」/「测试连接」就停在那一页，否则从基础信息开始
    pages = [getattr(error, "page", "basic") for error in context["errors"]]
    context["error_pages"] = set(pages)
    context["open_page"] = pages[0] if pages else (
        "tg" if context["tg_test"] else "mail" if context["mail_test"] else "basic"
    )
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
        return _render(errors=errors, create_form=_form_values(request.form), open_create=True), 400

    try:
        note = excel_source.create_account(data, actor=_actor())
    except ExcelSourceError as exc:
        return _render(errors=[str(exc)], create_form=_form_values(request.form), open_create=True), 409

    told, problems = _notify("created", lambda a: a.account == data["account"])
    flash(f"{note}。{told}", "ok")
    _warn(problems)
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
    current = next(a for a in existing if a.key == key)
    data, errors = excel_source.validate(request.form, others, creating=False, current=current)
    if errors:
        return _render(errors=errors, edit_key=key, edit_form=_form_values(request.form)), 400

    try:
        note = excel_source.update_account(key, data, actor=_actor())
    except ExcelSourceError as exc:
        return _render(errors=[str(exc)], edit_key=key, edit_form=_form_values(request.form)), 409

    if not note:
        flash("没有任何字段发生变化，台账未改动。", "warn")
        return redirect(url_for("accounts.index"))
    told, problems = "", []
    if current.mail_enabled and not data.get("mail_address"):
        # 清空了告警邮箱，邮件告警跟着关了（见 update_account）：和在表格里点关一样通知群。
        # 卡片上要写被关掉的是哪个邮箱，所以用改之前的地址；发不发、发给谁按改之后的 TG 设置
        after = next((a for a in _load_all()[0] if a.key == key), None)
        if after is not None:
            told, problems = _announce(
                "mail_off", replace(after, mail_address=current.mail_address, mail_provider=current.mail_provider)
            )
    flash(f"{note}。{told}", "ok")
    _warn(problems)
    return redirect(url_for("accounts.index"))


@bp.route("/tg-test", methods=["POST"])
@login_required
@csrf_protect
def tg_test():
    """弹窗里的「发测试消息」。

    按钮用 formaction 提交**同一个表单**，所以拿到的是弹窗里此刻填着的值——
    可以先测、测通了再保存。测完原样回填、重新打开同一个弹窗，不丢输入。
    这里**不写台账**，只发消息。

    一个账号可以有多个群：挨个发，每个群单独给结果，页面上贴在对应那一行旁边——
    哪个 ID 错了一眼看到，不用一个个排除。
    """
    key = (request.form.get("key") or "").strip()
    chats = excel_source.form_chat_ids(request.form)
    # 和正式消息一样只写账号 ID，不带上游
    label = (request.form.get("account") or "").strip()

    per_chat: dict[str, dict] = {}
    for chat in chats:
        if not telegram.valid_chat_id(chat):
            per_chat[chat] = {"ok": False, "message": "格式不对"}
            continue
        try:
            note = alerts.send_test(chat, label)
        except telegram.TelegramError as exc:
            per_chat[chat] = {"ok": False, "message": str(exc)}
        else:
            # 卡片画不出来时退回发了文字：发是发出去了，但得让人知道
            per_chat[chat] = {"ok": True, "message": f"已发送（{note}）" if note else "已发送"}

    if not chats:
        summary = {"ok": False, "message": "先填群组 ID 再测。"}
    else:
        good = sum(1 for r in per_chat.values() if r["ok"])
        summary = {
            "ok": good == len(per_chat),
            "message": (
                f"{len(per_chat)} 个群全部发送成功，去群里看一眼有没有收到。"
                if good == len(per_chat)
                else f"{len(per_chat)} 个群里 {good} 个成功、{len(per_chat) - good} 个失败，原因见对应那一行。"
            ),
        }
    result = {**summary, "chats": per_chat}

    values = _form_values(request.form)
    if key:
        existing, _ = _load_all()
        if not any(a.key == key for a in existing):
            flash("这个账号已经不在台账里了，页面可能已过期。已重新加载。", "error")
            return redirect(url_for("accounts.index"))
        return _render(tg_test=result, edit_key=key, edit_form=values)
    return _render(tg_test=result, create_form=values, open_create=True)


@bp.route("/tg-toggle", methods=["POST"])
@login_required
@csrf_protect
def tg_toggle():
    """表格里的 TG 开关：点一下只翻 TG_ENABLED 这一格。

    开的时候台账里必须已经有群组 ID——页面上没填的开关是灰的，但页面可能是
    几分钟前打开的，所以在写入的锁里再判一次（见 set_tg_enabled）。
    """
    key = (request.form.get("key") or "").strip()
    enabled = request.form.get("tg_enabled") == "1"
    try:
        note = excel_source.set_tg_enabled(key, enabled, actor=_actor())
    except ExcelSourceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.index"))
    flash(f"{note}。" if note else "本来就是这样，没有改动。", "ok" if note else "warn")
    return redirect(url_for("accounts.index"))


@bp.route("/mail-toggle", methods=["POST"])
@login_required
@csrf_protect
def mail_toggle():
    """表格里的邮件开关：点一下只翻 MAIL_ENABLED 这一格。开的时候邮箱必须已经填全。"""
    key = (request.form.get("key") or "").strip()
    enabled = request.form.get("mail_enabled") == "1"
    try:
        note = excel_source.set_mail_enabled(key, enabled, actor=_actor())
    except ExcelSourceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.index"))
    if not note:
        flash("本来就是这样，没有改动。", "warn")
        return redirect(url_for("accounts.index"))
    # 开 / 关邮件告警都给这个账号的群发一张卡片（跟 TG 开关走，见 alerts.notify_account）
    told, problems = _notify("mail_on" if enabled else "mail_off", lambda a: a.key == key)
    flash(f"{note}。{told}", "ok")
    _warn(problems)
    return redirect(url_for("accounts.index"))


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


@bp.route("/mail-test", methods=["POST"])
@login_required
@csrf_protect
def mail_test():
    """弹窗里的「测试连接」：用此刻填着的邮箱登录一下，只读地看一眼收件箱。不写台账。

    页面上用 fetch 调，回一段 JSON，弹窗和里面填着的密码都原地不动（密码不回填，整页
    刷新的话就得重打一遍）。没开 JS 时按钮就是个普通的提交，整页刷新、重新打开弹窗。

    修改弹窗里密码留空 = 用台账里存着的那个，和保存时「留空不改」同一个意思；但换了
    邮箱地址就不能沿用旧密码。
    """
    key = (request.form.get("key") or "").strip()
    current = None
    if key:
        existing, _ = _load_all()
        current = next((a for a in existing if a.key == key), None)
        if current is None:
            if _wants_json():
                return jsonify(ok=False, message="这个账号已经不在台账里了，页面可能已过期，刷新后再试。")
            flash("这个账号已经不在台账里了，页面可能已过期。已重新加载。", "error")
            return redirect(url_for("accounts.index"))

    result = _probe_mailbox(request.form, current)
    if _wants_json():
        return jsonify(result)
    values = _form_values(request.form)
    if current is not None:
        return _render(mail_test=result, edit_key=key, edit_form=values)
    return _render(mail_test=result, create_form=values, open_create=True)


def _probe_mailbox(form, current: Account | None) -> dict:
    provider = (form.get("mail_provider") or mail_inbox.DEFAULT_PROVIDER).strip()
    address = (form.get("mail_address") or "").strip()
    server = (form.get("mail_server") or "").strip()
    password = (form.get("mail_password") or "").strip()
    if not address:
        return {"ok": False, "message": "先填邮箱地址再测。"}
    if provider not in mail_inbox.BY_KEY:
        return {"ok": False, "message": "不认识这个邮箱平台，请从下拉框里选。"}
    if provider == mail_inbox.CUSTOM and mail_inbox.parse_server(server) is None:
        return {"ok": False, "message": "选了「其他平台」就要填 IMAP 服务器，形如 imap.example.com:993。"}
    if (
        not password and current is not None and current.mail_password
        and address.lower() == current.mail_address.lower()
    ):
        password = current.mail_password
    if not password:
        return {"ok": False, "message": "先填密码再测。"}
    box = mail_inbox.mailbox_for(provider, address, password, server)
    try:
        message = mail_inbox.probe(box, mail_rules.classify_headers)
    except mail_inbox.MailError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": message}


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
    if not note:
        flash("状态本来就是这样，没有改动。", "warn")
        return redirect(url_for("accounts.index"))

    told, problems = _notify("restored" if enabled else "disabled", lambda a: a.key == key)
    flash(f"{note}。{told}", "ok")
    _warn(problems)
    return redirect(url_for("accounts.index"))


@bp.route("/delete", methods=["POST"])
@login_required
@csrf_protect
def delete():
    """删除账号：整行清空、不能恢复，所以要重新输一遍登录密码。

    密码输错按登录失败计数，和登录页共用同一个锁定：拿到一个没退出的会话，也不能靠这里
    一遍遍试出密码。密码本身不进日志，也不回填。
    """
    key = (request.form.get("key") or "").strip()
    existing, fatal = _load_all()
    if fatal:
        flash(fatal, "error")
        return redirect(url_for("accounts.index"))
    if not any(a.key == key for a in existing):
        flash("这个账号已经不在台账里了，页面可能已过期。已重新加载。", "error")
        return redirect(url_for("accounts.index"))

    ip = client_ip()
    remaining = lockout_remaining(ip)
    if remaining:
        return _render(delete_key=key, delete_error=f"密码输错太多次了，请在 {remaining} 秒后再试。"), 429
    if not password_matches(request.form.get("password") or ""):
        record_failure(ip)
        return _render(delete_key=key, delete_error="登录密码不对，账号没有删除。"), 403
    clear_failures(ip)

    try:
        note = excel_source.delete_account(key, actor=_actor())
    except ExcelSourceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("accounts.index"))
    flash(f"{note}。", "ok")
    return redirect(url_for("accounts.index"))


def _notify(event: str, match) -> tuple[str, list[str]]:
    """账号刚新增 / 停用 / 恢复、邮件告警刚开 / 关：按 TG 开关给它的群发一张通知卡片。

    返回（追加在提示条后面的一句话, 发送时的问题）。台账在这之前已经写好了，
    通知发不出去不影响改动本身，只另起一条警告说清楚。
    """
    accounts, _ = _load_all()
    account = next((a for a in accounts if match(a)), None)
    if account is None:
        return "", []
    return _announce(event, account)


def _announce(event: str, account: Account) -> tuple[str, list[str]]:
    summary = alerts.notify_account(event, account, log=current_app.logger.warning)
    if summary is None:          # 这个账号没开 TG 告警，或者没填群
        return "", []
    told = f"已通知这个账号的 {summary.sent} 个 TG 群。" if summary.sent else ""
    return told, summary.problems


def _warn(problems: list[str]) -> None:
    if problems:
        flash("TG 通知没有全部发出去：" + "；".join(problems), "warn")
