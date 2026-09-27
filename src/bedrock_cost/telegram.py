"""Telegram Bot API 的最小客户端：只用到 sendMessage。

不引新依赖，走标准库 urllib。几件要注意的事：

  · **Token 就在 URL 里**（/bot<token>/sendMessage），urllib 的异常文本很容易把
    整个 URL 原样带出来。所以所有往外抛的错误信息都先过一遍 _redact。
  · Telegram 出错时 HTTP 状态码是 4xx，但响应体仍是 JSON，里面的 description
    才说得清原因（chat not found / bot was kicked …）。urllib 会把 4xx 当异常抛，
    要从异常里把响应体读出来。
  · 单条消息上限 4096 个字符。日报按群拆过，一个群里几十个账号也到不了上限；
    真超了就由调用方分条，这里不做静默截断。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import config

MAX_MESSAGE_CHARS = 4096

# 群组 / 超级群组是负数（超级群组以 -100 开头），私聊是正数，公开频道可以用 @名字
CHAT_ID_PATTERN = re.compile(r"^(-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")


class TelegramError(RuntimeError):
    """发送失败。消息已经是给人看的中文，Token 已擦掉。"""


def configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def valid_chat_id(chat_id: str) -> bool:
    return bool(CHAT_ID_PATTERN.match((chat_id or "").strip()))


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<TOKEN>") if token else text


# Telegram 的 description -> 说人话。按包含关系匹配，大小写不敏感。
_HINTS = (
    ("chat not found", "群组 ID 不对，或者 bot 还没有被拉进这个群"),
    ("bot was kicked", "bot 已经被移出这个群了，重新拉进来"),
    ("bot is not a member", "bot 不在这个群里，先把它拉进来"),
    ("not enough rights", "bot 在这个群里没有发言权限"),
    ("have no rights to send", "bot 在这个群里没有发言权限"),
    ("group chat was upgraded", "这个群升级成了超级群组，群组 ID 变了，用新的 -100 开头的那个"),
    ("unauthorized", "Bot Token 无效，检查 .env 里的 TELEGRAM_BOT_TOKEN"),
    ("can't parse entities", "消息格式有误（HTML 标签没闭合），这是程序的问题"),
)


def _explain(status: int, description: str) -> str:
    lowered = description.lower()
    for needle, hint in _HINTS:
        if needle in lowered:
            return f"{hint}（Telegram：{description}）"
    if status == 429:
        return f"发得太频繁，被 Telegram 限流了（{description}）"
    return f"Telegram 返回 {status}：{description}" if description else f"Telegram 返回 {status}"


def send_message(chat_id: str, text: str, *, timeout: float = 15.0) -> None:
    """往一个群发一条 HTML 格式的消息。失败抛 TelegramError。"""
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramError("服务器没有配置 TELEGRAM_BOT_TOKEN，发不出去")
    chat = (chat_id or "").strip()
    if not valid_chat_id(chat):
        raise TelegramError(f"群组 ID「{chat}」格式不对")
    if len(text) > MAX_MESSAGE_CHARS:
        raise TelegramError(f"消息有 {len(text)} 个字符，超过了 Telegram 的 {MAX_MESSAGE_CHARS} 上限")

    body = json.dumps(
        {
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            # 消息里没有链接要预览；关掉免得哪天带了 URL 出一张大卡片
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config.TELEGRAM_API_BASE}/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # 4xx 的响应体里才有真正的原因
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            payload = {}
        raise TelegramError(
            _redact(_explain(exc.code, str(payload.get("description", ""))), token)
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TelegramError(_redact(f"连不上 Telegram：{reason}", token)) from None
    except ValueError:
        raise TelegramError("Telegram 返回的不是 JSON") from None

    if not payload.get("ok"):
        raise TelegramError(
            _redact(
                _explain(int(payload.get("error_code") or 0), str(payload.get("description", ""))),
                token,
            )
        )
