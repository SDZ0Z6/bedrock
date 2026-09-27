"""命令行入口。

    python -m bedrock_cost                  起 web 服务（原来的行为，不带参数）
    python -m bedrock_cost alerts daily     发一次 Telegram 日报
    python -m bedrock_cost alerts hourly    跑一次小时告警
    python -m bedrock_cost alerts test --chat -1001234567890
                                            往一个群发测试消息

alerts 由 systemd timer 调起，跑完就退出；有任何发送或取数失败就以 1 退出，
`systemctl --failed` 和 `journalctl -u bedrock-alerts-*` 都看得到。

装过之后 `bedrock-cost` 等同于 `python -m bedrock_cost`。
"""

from __future__ import annotations

import argparse
import sys

from . import config


def _utf8_console() -> None:
    # Windows 控制台默认可能是 cp1252/cp936，直接 print 中文会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _serve() -> int:
    from . import create_app

    for warning in config.startup_warnings():
        print(f"[配置提醒] {warning}")
    print(f"\n  Bedrock 成本监控平台  ->  http://{config.HOST}:{config.PORT}\n")
    create_app().run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
    return 0


def _alerts(args: argparse.Namespace) -> int:
    from . import alerts, telegram

    if args.job == "test":
        if not args.chat:
            print("test 要用 --chat 指定群组 ID")
            return 2
        try:
            alerts.send_test(args.chat, "命令行测试")
        except telegram.TelegramError as exc:
            print(f"发送失败：{exc}")
            return 1
        print(f"已发往 {args.chat}")
        return 0

    run = alerts.run_daily if args.job == "daily" else alerts.run_hourly
    summary = run(dry_run=args.dry_run)
    print(f"完成：发出 {summary.sent} 条" + (f"，{len(summary.problems)} 个问题" if summary.problems else ""))
    for problem in summary.problems:
        print(f"  · {problem}")
    return 0 if summary.ok else 1


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="bedrock-cost", description="Bedrock 成本监控平台")
    sub = parser.add_subparsers(dest="command")

    alerts = sub.add_parser("alerts", help="发 Telegram 告警（由 systemd timer 调起）")
    alerts.add_argument("job", choices=("daily", "hourly", "test"))
    alerts.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印要发的消息，不真的发，也不改告警状态——上线前先用它看一眼",
    )
    alerts.add_argument("--chat", help="test 时发往的群组 ID")

    args = parser.parse_args(argv)
    if args.command == "alerts":
        return _alerts(args)
    return _serve()


if __name__ == "__main__":
    raise SystemExit(main())
