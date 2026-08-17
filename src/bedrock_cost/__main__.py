"""命令行入口。

    python -m bedrock_cost
或（装过之后）
    bedrock-cost
"""

from __future__ import annotations

import sys

from . import config, create_app


def main() -> int:
    # Windows 控制台默认可能是 cp1252/cp936，直接 print 中文会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    for warning in config.startup_warnings():
        print(f"[配置提醒] {warning}")
    print(f"\n  Bedrock 成本监控平台  ->  http://{config.HOST}:{config.PORT}\n")

    create_app().run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
