"""Telegram Bot API 的最小客户端：只用到 sendMessage 和 sendPhoto。

不引新依赖，走标准库 urllib。几件要注意的事：

  · **Token 就在 URL 里**（/bot<token>/sendMessage），urllib 的异常文本很容易把
    整个 URL 原样带出来。所以所有往外抛的错误信息都先过一遍 _redact。
  · Telegram 出错时 HTTP 状态码是 4xx，但响应体仍是 JSON，里面的 description
    才说得清原因（chat not found / bot was kicked …）。urllib 会把 4xx 当异常抛，
    要从异常里把响应体读出来。
  · 告警卡片是图片，用 sendPhoto 以 multipart/form-data 上传；图片下面的 caption
    上限 1024 个字，文字消息上限 4096。都按 Telegram 显示出来的字数算（HTML 标签
    不算），超了直接报错，这里不做静默截断。
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
import uuid

from . import config

MAX_MESSAGE_CHARS = 4096
MAX_CAPTION_CHARS = 1024

# 群组 / 超级群组是负数（超级群组以 -100 开头），私聊是正数，公开频道可以用 @名字
CHAT_ID_PATTERN = re.compile(r"^(-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")

_TAG = re.compile(r"<[^>]+>")


class TelegramError(RuntimeError):
    """发送失败。消息已经是给人看的中文，Token 已擦掉。"""


def configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def valid_chat_id(chat_id: str) -> bool:
    return bool(CHAT_ID_PATTERN.match((chat_id or "").strip()))


def visible(text: str) -> str:
    """HTML 格式的消息在 Telegram 里显示出来的样子：去掉标签、还原转义。"""
    return html.unescape(_TAG.sub("", text or ""))


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<TOKEN>") if token else text


# Telegram 的 description -> 说人话。按包含关系匹配，大小写不敏感。
_HINTS = (
    ("chat not found", "群组 ID 不对，或者 bot 还没有被拉进这个群"),
    ("bot was kicked", "bot 已经被移出这个群了，重新拉进来"),
    ("bot is not a member", "bot 不在这个群里，先把它拉进来"),
    # 比下面那条更具体，得排在前面：按顺序匹配，先中先得
    ("not enough rights to send photos", "bot 在这个群里没有发图片的权限"),
    ("not enough rights", "bot 在这个群里没有发言权限"),
    ("have no rights to send", "bot 在这个群里没有发言权限"),
    ("group chat was upgraded", "这个群升级成了超级群组，群组 ID 变了，用新的 -100 开头的那个"),
    ("unauthorized", "Bot Token 无效，检查 .env 里的 TELEGRAM_BOT_TOKEN"),
    ("can't parse entities", "消息格式有误（HTML 标签没闭合），这是程序的问题"),
    ("photo_invalid_dimensions", "卡片图片的尺寸 Telegram 不收，这是程序的问题"),
)


def _explain(status: int, description: str) -> str:
    lowered = description.lower()
    for needle, hint in _HINTS:
        if needle in lowered:
            return f"{hint}（Telegram：{description}）"
    if status == 429:
        return f"发得太频繁，被 Telegram 限流了（{description}）"
    return f"Telegram 返回 {status}：{description}" if description else f"Telegram 返回 {status}"


def _target(chat_id: str) -> tuple[str, str]:
    """(Token, 整理过的群组 ID)。没配 Token、ID 格式不对都在联网之前拦下。"""
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramError("服务器没有配置 TELEGRAM_BOT_TOKEN，发不出去")
    chat = (chat_id or "").strip()
    if not valid_chat_id(chat):
        raise TelegramError(f"群组 ID「{chat}」格式不对")
    return token, chat


def send_message(chat_id: str, text: str, *, timeout: float = 15.0) -> None:
    """往一个群发一条 HTML 格式的文字消息。失败抛 TelegramError。"""
    token, chat = _target(chat_id)
    if len(visible(text)) > MAX_MESSAGE_CHARS:
        raise TelegramError(f"消息有 {len(visible(text))} 个字符，超过了 Telegram 的 {MAX_MESSAGE_CHARS} 上限")
    body = json.dumps(
        {
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            # 消息里没有链接要预览；关掉免得哪天带了 URL 出一张大卡片
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    _post(token, "sendMessage", body, "application/json", timeout)


def send_photo(chat_id: str, photo: bytes, caption: str = "", *, timeout: float = 30.0) -> None:
    """往一个群发一张图片（告警卡片）。caption 是图片下面的文字，同样是 HTML。

    失败抛 TelegramError。
    """
    token, chat = _target(chat_id)
    if len(visible(caption)) > MAX_CAPTION_CHARS:
        raise TelegramError(
            f"图片说明有 {len(visible(caption))} 个字符，超过了 Telegram 的 {MAX_CAPTION_CHARS} 上限"
        )
    body, content_type = _multipart(
        {"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
        field="photo", filename="card.png", mime="image/png", data=photo,
    )
    _post(token, "sendPhoto", body, content_type, timeout)


def _multipart(fields: dict[str, str], *, field: str, filename: str, mime: str, data: bytes) -> tuple[bytes, str]:
    """标准库没有 multipart 编码，自己拼：普通字段一段一段，文件放最后。"""
    boundary = uuid.uuid4().hex
    head = []
    for name, value in fields.items():
        head += [f"--{boundary}", f'Content-Disposition: form-data; name="{name}"', "", value]
    head += [
        f"--{boundary}",
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"',
        f"Content-Type: {mime}",
        "",
        "",
    ]
    body = "\r\n".join(head).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


def _post(token: str, method: str, body: bytes, content_type: str, timeout: float) -> None:
    request = urllib.request.Request(
        f"{config.TELEGRAM_API_BASE}/bot{token}/{method}",
        data=body,
        headers={"Content-Type": content_type},
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
