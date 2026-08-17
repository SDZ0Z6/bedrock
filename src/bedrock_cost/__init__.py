"""Bedrock 成本监控平台。

把 cred.xlsx 里的账号台账和 AWS Cost Explorer 的实际消费拼在一起，展示每个
上游账号的预算使用情况，并支持按日/按月、按服务/标签/账号下钻。

对外只暴露一个应用工厂：

    from bedrock_cost import create_app
    app = create_app()

模块分工：
    config          .env 读取与默认值
    excel_source    读台账（账号、预算、比率、凭证、TAG 列）
    cost_explorer   概览页用的 CE 查询：按标签拆 TAG / UNTAG
    usage_explorer  下钻页用的 CE 查询：时间序列 + 三种维度
    report          概览页八列口径与合计
    chart           堆叠柱状图（SVG），图表色板的唯一来源
    dates           日期区间解析与快捷项
    auth / views    Web 层：登录蓝图与页面蓝图
    filters         Jinja 过滤器与全局
"""

from __future__ import annotations

from datetime import timedelta

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config
from .auth import bp as auth_bp
from .filters import register_filters
from .views import bp as views_bp

__all__ = ["create_app", "__version__"]
__version__ = "1.0.0"


def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def create_app(**overrides: object) -> Flask:
    """构建 Flask 应用。

    overrides 会覆盖 app.config，测试里用来打开 TESTING 之类的开关。
    模板和静态文件放在包内，所以装成 wheel 也能正常找到。
    """
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_HOURS),
    )
    app.config.update(overrides)

    if config.TRUST_PROXY:
        # 取最靠右的那一个值：Nginx 追加的那个才是它真正看到的对端地址，
        # 左边的可以由客户端随便伪造。不做这一步的话，攻击者只要每次换一个
        # 假的 X-Forwarded-For 就能绕开按 IP 的登录锁定。
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    register_filters(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.after_request(_security_headers)
    return app
