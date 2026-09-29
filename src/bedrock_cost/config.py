"""集中配置。所有可调项都从 .env 读取，缺失时使用安全默认值。

.env 里放的是登录口令和 Flask 会话密钥，不要提交到版本库。
AWS 的 AK/SK 不在这里，它们来自 cred.xlsx。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def _find_project_root() -> Path:
    """定位放 .env / cred.xlsx 的项目根目录。

    代码在 src/bedrock_cost/ 里，数据和配置在项目根，所以不能用 __file__ 的
    父目录。以 pyproject.toml 作为标记向上找：先从当前工作目录找（正常启动、
    以及 editable 安装后从别处运行都能命中），再从包所在位置找；都找不到就
    退回当前工作目录。
    """
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for candidate in (base, *base.parents):
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return Path.cwd()


BASE_DIR = _find_project_root()


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
# 账号台账（上游 / 账号 / 额度 / 启用日期 / 比率 / AK / SK）
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

# ---------------------------------------------------------------- 预估成本
# 计价档覆盖。auto = 按 ModelId 和推理配置的 ARN 自动判断跨区(global)还是
# 本区(standard)；实测该判断和账单误差 0.06%。真遇到判错时可以强制指定：
#   PRICE_TIER=global    全部按跨区价（便宜约 10%）
#   PRICE_TIER=standard  全部按本区价
PRICE_TIER = _text("PRICE_TIER", "auto").lower()

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

# 只在 HTTPS 下发送会话 cookie。上了公网必须打开；本地 http 开着会登不进去。
SESSION_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE", False)

# 部署在 Nginx 之类的反向代理后面时打开。打开后 Flask 会按
# X-Forwarded-For / X-Forwarded-Proto 还原真实客户端 IP 和协议——
# 否则所有请求看起来都来自 127.0.0.1，按 IP 的登录锁定形同虚设。
# 只有当代理确实由你自己控制、且会覆写这两个头时才可以打开。
TRUST_PROXY = _flag("TRUST_PROXY", False)

# ---------------------------------------------------------------- 展示与运行
CURRENCY_SYMBOL = _text("CURRENCY_SYMBOL", "$")
# 使用率颜色阈值（百分比）
WARN_PCT = _number("WARN_PCT", 70)
DANGER_PCT = _number("DANGER_PCT", 90)

HOST = _text("HOST", "127.0.0.1")
PORT = _number("PORT", 5000)
DEBUG = _flag("DEBUG", False)

# ---------------------------------------------------------------- Telegram 告警
# 开关和群组 ID 是**每个账号**的，在台账里（TG_ENABLED / TG_CHAT_IDS 两列，一个账号
# 可以填几个群），从账号管理页改。这里只放全局的几项。
#
# Bot Token 只放 .env，页面上看不到也改不了：能登录的人就能看到页面，Token 泄露
# 等于别人能冒充这个 bot 往你的群里发消息。在 Telegram 里找 @BotFather 建 bot 拿到。
TELEGRAM_BOT_TOKEN = _text("TELEGRAM_BOT_TOKEN")

# API 地址。一般不用改——服务器在吉隆坡，直连可达；万一哪天要走自建反代再改。
TELEGRAM_API_BASE = _text("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def _thresholds(raw: str) -> list[float]:
    """额度告警档位。坏值忽略，最后兜底成默认的四档。"""
    picked = set()
    for piece in raw.split(","):
        try:
            value = float(piece.strip())
        except ValueError:
            continue
        if value > 0:
            picked.add(value)
    return sorted(picked) or [50.0, 80.0, 90.0, 100.0]


# 额度使用率达到这些百分比时各发一次告警
TELEGRAM_THRESHOLDS = _thresholds(_text("TELEGRAM_THRESHOLDS", "50,80,90,100"))

# 连续多少个整点小时零调用算「用量中断」。1 = 上一个整点小时没调用就发。
# MAP 流量常有突发的空档，嫌吵就调大。
TELEGRAM_IDLE_HOURS = max(1, _number("TELEGRAM_IDLE_HOURS", 1))

# 每张告警卡片底部的署名。写成空（TELEGRAM_CARD_SIGNATURE=）就不画这一行——
# 所以这里不走 _text：_text 会把空值当成没配、换回默认值
TELEGRAM_CARD_SIGNATURE = os.environ.get(
    "TELEGRAM_CARD_SIGNATURE", "This message was sent automatically by pokemoncloud"
).strip()

# 告警要跨次运行记住的状态：上次有没有用量、哪些额度档位已经发过、CE 实账的缓存。
# 和台账放一起，systemd 单元的 ReadWritePaths 已经覆盖。
ALERT_STATE_PATH = Path(_text("ALERT_STATE_PATH", "alert-state.json"))
if not ALERT_STATE_PATH.is_absolute():
    ALERT_STATE_PATH = BASE_DIR / ALERT_STATE_PATH

# 每个账号最近一次查询成功的累计消费。CE 查不到时，概览页和日报拿它顶上并标出
# 「截至哪天」（见 last_known）。和告警状态放一起，systemd 的 ReadWritePaths 已经覆盖。
LAST_KNOWN_COSTS_PATH = Path(_text("LAST_KNOWN_COSTS_PATH", "last-known-costs.json"))
if not LAST_KNOWN_COSTS_PATH.is_absolute():
    LAST_KNOWN_COSTS_PATH = BASE_DIR / LAST_KNOWN_COSTS_PATH


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
