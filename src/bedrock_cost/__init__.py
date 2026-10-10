"""Bedrock 成本监控平台。

把 cred.xlsx 里的账号台账和 AWS Cost Explorer 的实际消费拼在一起，展示每个
上游账号的额度使用情况，并支持按日/按月、按服务/标签/账号下钻。

对外只暴露一个应用工厂：

    from bedrock_cost import create_app
    app = create_app()

模块分工：
    config          .env 读取与默认值
    excel_source    读台账（账号、额度、启用日期、比率、凭证、TAG 列）
    cost_explorer   概览页用的 CE 查询：按标签拆 TAG / UNTAG
    usage_explorer  下钻页用的 CE 查询：时间序列 + 三种维度
    report          概览页八列口径与合计
    chart           堆叠柱状图（SVG），图表色板的唯一来源
    dates           日期区间解析与快捷项
    auth / views    Web 层：登录蓝图与概览
    account_pages   账号页：一个账号的摘要 / 成本 / 用量 / 预估 / 时间线 / 配额六个页签
    ops             运营看板：经营汇总（收入、AWS 原价、毛利）、风险与告警、模型与用量
    ops_report      运营看板的数据拼装
    dashboard       概览、账号页、看板共用的数据拼装（卡片、图表、报错弹窗）
    customers       客户：账号在客户名下的阶段、客户的钱、时间线、月度对账单
    customer_pages  客户页：客户列表、客户详情和详情页上的动作（分配、替换、结算……）
    cost_history    客户页要的每天消费，存一份在磁盘上（停用账号的历史、查询失败时顶着）
    activity        各账号的用量状态（活跃 / 已中断 / 无调用）
    events          发出去的告警流水（看板的「最近告警」）
    accounts        账号管理页：台账的增 / 改 / 停用、生命周期标签
    settings_pages  设置：操作日志（台账的每次改动）、登录记录
    login_log       登录记录（成功、失败、锁定、退出），设置页读它
    filters         Jinja 过滤器与全局
"""

from __future__ import annotations

from datetime import timedelta

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config, excel_source
from .account_pages import bp as account_bp
from .accounts import bp as accounts_bp
from .auth import bp as auth_bp, client_ip
from .customer_pages import bp as customers_bp
from .filters import register_filters
from .ops import bp as ops_bp
from .settings_pages import bp as settings_bp
from .views import bp as views_bp

__all__ = ["create_app", "__version__"]
__version__ = "1.0.0"


def _remember_ip() -> None:
    """操作日志带上这次改动是从哪个 IP 来的（excel_source.audit_ip）。"""
    ip = client_ip()
    excel_source.audit_ip.set("" if ip == "?" else ip)


def _forget_ip(_error=None) -> None:
    excel_source.audit_ip.set("")


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
        # 模板里用 config.OPS_DASHBOARD 决定显不显示看板的入口
        OPS_DASHBOARD=config.OPS_DASHBOARD,
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
    app.register_blueprint(customers_bp)
    app.register_blueprint(account_bp)
    if app.config["OPS_DASHBOARD"]:
        app.register_blueprint(ops_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(settings_bp)
    app.before_request(_remember_ip)
    app.teardown_request(_forget_ip)
    app.after_request(_security_headers)
    return app
