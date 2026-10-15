"""平台上的人：管理员和只读用户，以及每个人自己的资料（显示名、头像）。

管理员就是 .env 里的那一个（AUTH_USERNAME / AUTH_PASSWORD）：只有它能改东西、管用户，密码也只在 .env 里改。
只读用户是管理员在「用户管理」里建的：能看概览、客户、账号的六个页签，能导出 CSV；改不了任何东西，
看不到账号管理、用户管理和设置，也不能「刷新数据」（挡在 auth 那一层，对所有请求生效）。

每个人（管理员也是）在自己的资料页里设显示名和头像：头像是一个字或表情 + 八组底色里的一组，和账号头像一个样子
（avatars.person_avatar）。没设显示名就显示用户名；登录、登录记录、操作日志里用的一直是用户名。

存在 users.json（USERS_PATH）：
  {"admin": {"display", "avatar_text", "avatar_color", "scene", "pet"},     管理员的资料（不存密码）
   "users": [{"name", "note", "hash", "stamp", "disabled", "created", "display", "avatar_text", "avatar_color", "scene",
              "pet"}]}
  name      用户名（登录用，不分大小写；不能和管理员重名；建了就不改）
  note      备注（是谁，管理员自己看的）
  hash      密码的哈希（werkzeug，和 AUTH_PASSWORD_HASH 同一种）。**明文密码哪儿都不存**
  stamp     一个随机串：重置密码、自己改密码、停用都换一个。登录时存进会话，之后每个请求对一遍（session_user），
            对不上就当没登录——重置密码、停用、删除以后，这个用户在别处登录着的马上失效
  created   什么时候建的（UTC）
  scene     概览欢迎条的背景（空着是自动）；pet 是页面底下那只小家伙出不出来（"off" 是收起来了，空着是出来）

密码：新建、重置的时候管理员可以自己填（至少 MIN_PASSWORD 位），不填就由系统生成（new_password：4 组 4 个字，
约 80 位），都只在那一下显示一次；用户登录以后在自己的资料页里改。

写：一把锁 + 临时文件 + 原子替换，权限 600（里面是密码哈希）。读：每个请求都要对 stamp、画侧边栏，按文件的
inode、修改时间和大小缓存，不是每次都读盘。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from . import avatars, config

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{2,31}")
SCENE_RE = re.compile(r"[a-z]{1,16}")    # 背景的 key 长这样；有哪些由 welcome.SCENES 说了算
PET_OFF = "off"
NAME_RULE = "用户名 3 到 32 个字符：英文字母、数字和 . _ - @，开头是字母或数字。"
MAX_NOTE = 60
MAX_DISPLAY = 20
MIN_PASSWORD = 8
MAX_PASSWORD = 128
# 生成的密码用这些字：去掉了容易看错的 0 o 1 l i
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
FILE_MODE = 0o600
HASH_METHOD = "scrypt"   # werkzeug 的默认；测试里换成快的

_lock = threading.RLock()
_cache: dict = {"key": None, "book": None}


class UserError(ValueError):
    """新建、改密码、改资料时输入不对，消息直接给页面看。"""


class WrongPassword(UserError):
    """改密码时「现在的密码」不对：和登录失败一样算一次（按 IP 锁定）。"""


@dataclass(frozen=True)
class Look:
    """一个人在页面上的样子：显示名、头像（一个字或表情、八组底色里的第几组）、概览欢迎条的背景、页面底下那只小家伙。
    都可以空着。"""
    display: str = ""
    avatar_text: str = ""
    avatar_color: int | None = None
    scene: str = ""         # 背景（welcome.SCENES 的 key），空着是按时间、季节自动挑
    pet: str = ""           # 每个页面底下走来走去的小家伙（pet.py）：PET_OFF 是收起来了，空着是出来

    @property
    def pet_on(self) -> bool:
        return self.pet != PET_OFF


@dataclass(frozen=True)
class Person:
    """页面上画一个人：侧边栏底下、欢迎语、用户管理、资料页。管理员和只读用户都是它（模板里的 current_user()）。"""
    name: str
    admin: bool
    look: Look = field(default_factory=Look)

    @property
    def label(self) -> str:
        """显示的名字：设了显示名用显示名，没设用用户名。"""
        return self.look.display or self.name

    @property
    def role_label(self) -> str:
        return "管理员" if self.admin else "只读"

    @property
    def avatar(self) -> avatars.Avatar:
        return avatars.person_avatar(self.name, self.label, self.look.avatar_text, self.look.avatar_color)


@dataclass(frozen=True)
class User:
    """一个只读用户。"""
    name: str
    hash: str
    stamp: str
    note: str = ""
    created: datetime | None = None
    disabled: bool = False
    look: Look = field(default_factory=Look)

    @property
    def key(self) -> str:
        return self.name.casefold()

    @property
    def person(self) -> Person:
        return Person(name=self.name, admin=False, look=self.look)


@dataclass
class Book:
    """users.json 里的全部：管理员的资料、只读用户。"""
    admin: Look = field(default_factory=Look)
    users: list[User] = field(default_factory=list)


def new_password() -> str:
    """随机密码：xxxx-xxxx-xxxx-xxxx。"""
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(4))


def _hash(password: str) -> str:
    return generate_password_hash(password, method=HASH_METHOD)


@lru_cache(maxsize=1)
def _decoy() -> str:
    """没有这个用户时也算一次哈希：登录失败花的时间一样，试不出哪个用户名是真的。"""
    return _hash(secrets.token_hex(16))


def _stamp() -> str:
    return secrets.token_urlsafe(16)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------- 读写
def _path() -> Path:
    return config.USERS_PATH


def _look(raw) -> Look:
    if not isinstance(raw, dict):
        return Look()
    color = raw.get("avatar_color")
    scene = str(raw.get("scene") or "")
    return Look(display=str(raw.get("display") or ""), avatar_text=str(raw.get("avatar_text") or ""),
                avatar_color=color if isinstance(color, int) and 0 <= color < avatars.TONES else None,
                scene=scene if SCENE_RE.fullmatch(scene) else "", pet=PET_OFF if raw.get("pet") == PET_OFF else "")


def _look_json(look: Look) -> dict:
    return {"display": look.display, "avatar_text": look.avatar_text, "avatar_color": look.avatar_color,
            "scene": look.scene, "pet": look.pet}


def _read(path: Path, strict: bool = False) -> Book:
    """文件没有、读不了、格式不对都当没有用户（只读用户登不进来，管理员照常）；坏的那几条跳过。
    strict（要改的时候）：文件在、却读不了，就不改——不然写回去会把读不出来的那些用户冲掉。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Book()
    except (OSError, ValueError) as exc:
        if strict:
            raise UserError(f"{path.name} 读不了（{type(exc).__name__}），先别改用户，看看这个文件。") from exc
        return Book()
    if not isinstance(data, dict):
        return Book()
    rows = data.get("users")
    book = Book(admin=_look(data.get("admin")))
    for raw in rows if isinstance(rows, list) else []:
        try:
            created = datetime.fromisoformat(raw["created"]) if raw.get("created") else None
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            user = User(name=str(raw["name"]).strip(), hash=str(raw["hash"]), stamp=str(raw["stamp"]),
                        note=str(raw.get("note") or ""), created=created, disabled=bool(raw.get("disabled")),
                        look=_look(raw))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if user.name and user.hash and user.stamp:
            book.users.append(user)
    return book


def _write(book: Book) -> None:
    path = _path()
    payload = {
        "admin": _look_json(book.admin),
        "users": [
            {"name": user.name, "note": user.note, "hash": user.hash, "stamp": user.stamp, "disabled": user.disabled,
             "created": user.created.isoformat() if user.created else "", **_look_json(user.look)}
            for user in book.users
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".users-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        try:
            os.chmod(temp, FILE_MODE)
        except OSError:
            pass
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _cache["key"] = None


def _book() -> Book:
    path = _path()
    try:
        info = path.stat()
    except OSError:
        return Book()
    key = (str(path), info.st_ino, info.st_mtime_ns, info.st_size)
    with _lock:
        if _cache["key"] != key:
            _cache.update(key=key, book=_read(path))
        return _cache["book"]


def load() -> list[User]:
    """全部只读用户，先建的在前。"""
    return list(_book().users)


def find(name: str) -> User | None:
    wanted = (name or "").strip().casefold()
    return next((user for user in load() if user.key == wanted), None) if wanted else None


def admin_person(name: str) -> Person:
    """管理员：名字是 .env 里的 AUTH_USERNAME，资料存在 users.json 的 admin 那一项。"""
    return Person(name=name, admin=True, look=_book().admin)


def _change(apply):
    """读最新的一份、改、写回去，整个过程拿着锁。apply 抛 UserError 就什么都不写。"""
    with _lock:
        book = _read(_path(), strict=True)
        result = apply(book)
        _write(book)
        return result


def _locate(book: Book, name: str) -> int:
    wanted = (name or "").strip().casefold()
    for index, user in enumerate(book.users):
        if user.key == wanted:
            return index
    raise UserError("没有这个用户了：页面可能已经过期，刷新后再试。")


# ---------------------------------------------------------------- 管理员管用户
def check_name(name: str, users: list[User]) -> str:
    name = (name or "").strip()
    if not name:
        raise UserError("用户名没填。")
    if not NAME_RE.fullmatch(name):
        raise UserError(NAME_RULE)
    if name.casefold() == (config.AUTH_USERNAME or "").strip().casefold():
        raise UserError(f"{name} 是管理员的用户名，换一个。")
    if any(user.key == name.casefold() for user in users):
        raise UserError(f"已经有叫 {name} 的用户了。")
    return name


def check_password(name: str, password: str) -> str:
    """自己填的密码（管理员新建、重置时填的，用户自己改的）：长度够、不和用户名一样。"""
    if len(password) < MIN_PASSWORD:
        raise UserError(f"密码至少 {MIN_PASSWORD} 位。")
    if len(password) > MAX_PASSWORD:
        raise UserError(f"密码最多 {MAX_PASSWORD} 位。")
    if password.casefold() == (name or "").strip().casefold():
        raise UserError("密码不能和用户名一样。")
    return password


def create(name: str, note: str = "", password: str | None = None) -> tuple[User, str]:
    """新建一个只读用户，返回 (用户, 密码)。password 是管理员自己填的；不填（None）就由系统生成。
    密码只在这里出现这一次。"""
    note = " ".join((note or "").split())[:MAX_NOTE]

    def apply(book: Book) -> tuple[User, str]:
        clean = check_name(name, book.users)
        chosen = new_password() if password is None else check_password(clean, password)
        user = User(name=clean, hash=_hash(chosen), stamp=_stamp(), note=note, created=_now())
        book.users.append(user)
        return user, chosen

    return _change(apply)


def reset_password(name: str, password: str | None = None) -> tuple[User, str]:
    """换一个密码，返回 (用户, 新密码)：管理员自己填的，没填就由系统生成。stamp 跟着换：别处登录着的全退出。"""
    def apply(book: Book) -> tuple[User, str]:
        index = _locate(book, name)
        user = book.users[index]
        chosen = new_password() if password is None else check_password(user.name, password)
        book.users[index] = replace(user, hash=_hash(chosen), stamp=_stamp())
        return book.users[index], chosen

    return _change(apply)


def set_disabled(name: str, disabled: bool) -> User:
    """停用（登录着的马上退出）/ 恢复（用原来的密码登录）。"""
    def apply(book: Book) -> User:
        index = _locate(book, name)
        book.users[index] = replace(book.users[index], disabled=disabled, stamp=_stamp())
        return book.users[index]

    return _change(apply)


def delete(name: str) -> User:
    def apply(book: Book) -> User:
        return book.users.pop(_locate(book, name))

    return _change(apply)


# ---------------------------------------------------------------- 自己的资料
def clean_look(display: str, avatar_text: str, avatar_color) -> Look:
    """资料页提交上来的：显示名收成一行、最多 MAX_DISPLAY 个字；头像只留第一个字；底色是 0…7，空着是自动。"""
    display = " ".join((display or "").split())
    if len(display) > MAX_DISPLAY:
        raise UserError(f"名字最多 {MAX_DISPLAY} 个字。")
    raw = "" if avatar_color is None else str(avatar_color).strip()
    if raw and not (raw.isdigit() and 0 <= int(raw) < avatars.TONES):
        raise UserError("头像底色不对：页面可能已经过期，刷新后再试。")
    return Look(display=display, avatar_text=avatars.first_grapheme(avatar_text or ""),
                avatar_color=int(raw) if raw else None)


def save_look(person: Person, look: Look) -> Person:
    """存自己的资料：管理员的存在 admin 那一项，只读用户的存在自己那一条。"""
    def apply(book: Book) -> Person:
        if person.admin:
            book.admin = look
            return Person(name=person.name, admin=True, look=look)
        index = _locate(book, person.name)
        book.users[index] = replace(book.users[index], look=look)
        return book.users[index].person

    return _change(apply)


# ---------------------------------------------------------------- 登录、改自己的密码
def authenticate(name: str, password: str) -> tuple[str, User | None]:
    """登录：返回 (结果, 用户)。结果是 ok、user（没有这个用户）、fail（密码不对）、disabled（密码对，但停用了）。
    停用的要密码对了才说，不然谁都能试出哪些用户名停用了。"""
    user = find(name)
    if user is None:
        check_password_hash(_decoy(), password or "")
        return "user", None
    if not check_password_hash(user.hash, password or ""):
        return "fail", user
    return ("disabled" if user.disabled else "ok"), user


def session_user(name: str, stamp: str) -> User | None:
    """会话里的只读用户还算不算数：还在、没停用、stamp 对得上（没重置过密码）。算数就返回这个用户。"""
    user = find(name)
    if (user is not None and not user.disabled and stamp
            and hmac.compare_digest(user.stamp.encode("utf-8"), stamp.encode("utf-8"))):
        return user
    return None


def change_password(name: str, current: str, new: str, again: str) -> User:
    """用户自己改密码：先对现在的密码。stamp 跟着换（调用方把新的写回这个会话，别处登录着的退出）。"""
    current, new, again = current or "", new or "", again or ""
    if not current:
        raise UserError("要先输现在的密码。")
    if not new:
        raise UserError("新密码没填。")
    if new != again:
        raise UserError("两次输的新密码不一样。")
    check_password(name, new)

    def apply(book: Book) -> User:
        index = _locate(book, name)
        if not check_password_hash(book.users[index].hash, current):
            raise WrongPassword("现在的密码不对。")
        if new == current:
            raise UserError("新密码和现在的一样。")
        book.users[index] = replace(book.users[index], hash=_hash(new), stamp=_stamp())
        return book.users[index]

    return _change(apply)
