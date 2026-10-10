"""设置：平台自己的记录，两个页签。

  · 操作日志：台账的每一次改动（谁、什么时候、改了什么），就是台账旁边的 ledger-audit.log，凭证的值
    从来不写进去（见 excel_source._audit）。能搜、能按类筛：搜账号号码就是这个账号的全部改动，包括
    时间线（EVENTS 表）出现之前改过的额度。
  · 登录记录：每次登录成功、失败、被锁定、退出（见 login_log）。

只读本地文件，不调任何 AWS API，不花钱。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, redirect, render_template, request, url_for

from . import config, excel_source, login_log
from .auth import client_ip, login_required
from .views import page_meta

bp = Blueprint("settings", __name__, url_prefix="/settings")

TABS = (("audit", "操作日志", "settings.audit", "正在读操作日志"),
        ("logins", "登录记录", "settings.logins", "正在读登录记录"))
AUDIT_TAIL = 5000     # 操作日志只读最后这么多行（一行一百来字，几年也就几兆）
AUDIT_SHOW = 500      # 一次最多列这么多条，更早的用搜索找
AUDIT_KINDS = ("账号", "客户", "标签", "告警", "其他")
_NUMBER = re.compile(r"(?<!\d)(\d{12})(?!\d)")


@dataclass
class AuditLine:
    when: str          # 日志里原样的本地时间：2026-10-10 02:47:59
    actor: str
    note: str
    kind: str          # AUDIT_KINDS 之一

    @property
    def parts(self) -> list[tuple[str, str]]:
        """说明里的 12 位账号号码单独拎出来（页面上点它去那个账号的时间线）：[(文字, 号码或空)]。"""
        out, last = [], 0
        for found in _NUMBER.finditer(self.note):
            if found.start() > last:
                out.append((self.note[last:found.start()], ""))
            out.append((found.group(1), found.group(1)))
            last = found.end()
        if last < len(self.note):
            out.append((self.note[last:], ""))
        return out


def _kind(note: str) -> str:
    """按 excel_source 写日志时的说法归类。"""
    if note.endswith(("TG 告警", "邮件告警")):
        return "告警"
    if "生命周期" in note:
        return "标签"
    if note.startswith(("新增账号", "修改账号", "停用账号", "恢复账号", "删除账号")):
        return "账号"
    if "客户" in note:
        return "客户"
    return "其他"


def audit_path() -> Path:
    return config.EXCEL_PATH.parent / excel_source.AUDIT_NAME


def audit_lines(path: Path | None = None) -> list[AuditLine]:
    """操作日志最后 AUDIT_TAIL 行，新的在前。文件没有、读不了就是空的；格式不对的行跳过。"""
    try:
        with open(path or audit_path(), encoding="utf-8", errors="replace") as handle:
            tail = deque(handle, maxlen=AUDIT_TAIL)
    except OSError:
        return []
    out = []
    for raw in reversed(tail):
        parts = raw.rstrip("\r\n").split("\t", 2)
        if len(parts) != 3 or not parts[2].strip():
            continue
        when, actor, note = (part.strip() for part in parts)
        out.append(AuditLine(when=when, actor=actor, note=note, kind=_kind(note)))
    return out


def _tabs(current: str) -> list[tuple[str, str, bool, str]]:
    return [(url_for(endpoint), name, key == current, hint) for key, name, endpoint, hint in TABS]


@bp.route("/")
@login_required
def index():
    return redirect(url_for("settings.audit"))


@bp.route("/audit")
@login_required
def audit():
    query = (request.args.get("q") or "").strip()
    kind = request.args.get("kind") if request.args.get("kind") in AUDIT_KINDS else ""
    lines = audit_lines()
    counts = {name: sum(1 for line in lines if line.kind == name) for name in AUDIT_KINDS}
    needle = query.casefold()
    matched = [line for line in lines
               if (not kind or line.kind == kind)
               and (not needle or needle in f"{line.when}\t{line.actor}\t{line.note}".casefold())]
    return render_template(
        "settings.html", tab="audit", tabs=_tabs("audit"), active_page="settings",
        lines=matched[:AUDIT_SHOW], matched=len(matched), total=len(lines), counts=counts,
        kinds=AUDIT_KINDS, kind=kind, query=query, show_limit=AUDIT_SHOW, tail_limit=AUDIT_TAIL,
        audit_name=excel_source.AUDIT_NAME, **page_meta(),
    )


@bp.route("/logins")
@login_required
def logins():
    entries = login_log.recent()
    since = datetime.now(timezone.utc) - timedelta(days=30)
    month = [entry for entry in entries if entry.when >= since]
    summary = {
        "ok": sum(1 for entry in month if entry.kind == "ok"),
        "failed": sum(1 for entry in month if entry.failed),
        "ips": len({entry.ip for entry in month if entry.kind == "ok" and entry.ip}),
    }
    return render_template(
        "settings.html", tab="logins", tabs=_tabs("logins"), active_page="settings",
        entries=entries, summary=summary, my_ip=client_ip(), **page_meta(),
    )
