"""集中配置。所有可调项都从 .env 读取，缺失时使用安全默认值。

.env 里放的是登录口令和 Flask 会话密钥，不要提交到版本库。
AWS 的 AK/SK 不在这里，它们来自 cred.xlsx。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """极简 .env 解析器，省掉 python-dotenv 依赖。

    已经存在的真实环境变量优先，方便临时覆盖：
        set TAG_KEY=other-key && python app.py
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env_file(BASE_DIR / ".env")


def _text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _number(name: str, default: int) -> int:
    try:
        return int(_text(name) or default)
    except ValueError:
        return default


def _flag(name: str, default: bool = False) -> bool:
    raw = _text(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- 数据源
# 账号台账（上游 / 账号 / 预算 / 比率 / AK / SK）
EXCEL_PATH = Path(_text("EXCEL_PATH", "cred.xlsx"))
if not EXCEL_PATH.is_absolute():
    EXCEL_PATH = BASE_DIR / EXCEL_PATH

# ---------------------------------------------------------------- Cost Explorer
# 成本分配标签键：该标签有值的消费算 TAG，标签缺失/为空的算 UNTAG。
TAG_KEY = _text("TAG_KEY", "map-migrated")

# UnblendedCost / AmortizedCost / NetUnblendedCost / NetAmortizedCost
COST_METRIC = _text("COST_METRIC", "UnblendedCost")

# Cost Explorer 是全局服务，端点固定在 us-east-1。
CE_REGION = _text("CE_REGION", "us-east-1")

# 留空 = 统计账号下全部服务（当前配置）。
# 若只想统计部分服务，填 Cost Explorer 里的服务全名，逗号分隔。
# 注意：Bedrock 上的 Claude 在 CE 里是按模型独立计费条目
# （如 "Claude Opus 5 (Amazon Bedrock Edition)"），不叫 "Amazon Bedrock"。
SERVICE_FILTER = [s.strip() for s in _text("SERVICE_FILTER").split(",") if s.strip()]

# CE 每次请求收费 0.01 USD，缓存可以显著省钱。单位：秒。
CACHE_TTL = _number("CACHE_TTL", 900)

# 并发查询账号数
MAX_WORKERS = _number("MAX_WORKERS", 8)

# 单个账号的 CE 请求超时与重试
CE_TIMEOUT = _number("CE_TIMEOUT", 30)
CE_RETRIES = _number("CE_RETRIES", 3)

# ---------------------------------------------------------------- 登录
AUTH_USERNAME = _text("AUTH_USERNAME", "admin")
AUTH_PASSWORD = _text("AUTH_PASSWORD")
# 可选：改用 werkzeug 生成的哈希，设置后 AUTH_PASSWORD 被忽略
AUTH_PASSWORD_HASH = _text("AUTH_PASSWORD_HASH")

SECRET_KEY = _text("SECRET_KEY") or secrets.token_hex(32)
SESSION_HOURS = _number("SESSION_HOURS", 12)

# 连续失败达到上限后锁定该 IP 一段时间
MAX_LOGIN_ATTEMPTS = _number("MAX_LOGIN_ATTEMPTS", 8)
LOCKOUT_SECONDS = _number("LOCKOUT_SECONDS", 300)

# ---------------------------------------------------------------- 展示与运行
CURRENCY_SYMBOL = _text("CURRENCY_SYMBOL", "$")
# 使用率颜色阈值（百分比）
WARN_PCT = _number("WARN_PCT", 70)
DANGER_PCT = _number("DANGER_PCT", 90)

HOST = _text("HOST", "127.0.0.1")
PORT = _number("PORT", 5000)
DEBUG = _flag("DEBUG", False)


def startup_warnings() -> list[str]:
    """启动时需要提醒用户的配置问题。"""
    problems = []
    if not AUTH_PASSWORD and not AUTH_PASSWORD_HASH:
        problems.append(
            "未设置 AUTH_PASSWORD，任何人都无法登录。请在 .env 中填写登录密码。"
        )
    if not os.environ.get("SECRET_KEY"):
        problems.append(
            "未设置 SECRET_KEY，本次使用了临时随机值；重启后所有登录会话会失效。"
        )
    if not EXCEL_PATH.is_file():
        problems.append(f"找不到账号台账文件：{EXCEL_PATH}")
    return problems
