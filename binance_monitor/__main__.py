from __future__ import annotations

import argparse
import sys
from .core import Monitor, Settings, ValidationError, detect_local_proxy, parse_targets


def main() -> int:
    parser = argparse.ArgumentParser(description="币安合约跟单监控与自动加入（桌面中明确启用后运行）")
    parser.add_argument("--demo", action="store_true", help="使用明确标记的模拟数据")
    parser.add_argument("--check", metavar="ID_OR_LINK", help="单次检查公开接口，输出 JSON")
    parser.add_argument("--proxy", default=None, help="HTTP/HTTPS 代理地址，不含账号密码；默认检测系统本地代理")
    parser.add_argument("--headless", action="store_true", help="命令行检查；与 --demo 或 --check 一起使用")
    parser.add_argument("--quit-after-demo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-edge-component", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_edge_component:
        if args.demo or args.check or args.headless or args.quit_after_demo:
            parser.error("--check-edge-component 不能与其他运行模式组合")
        from .browser_account import check_edge_component
        try:
            check_edge_component()
            return 0
        except Exception:
            return 1
    if args.quit_after_demo and (not args.demo or args.check or args.headless):
        parser.error("--quit-after-demo 仅用于桌面演示")
    if args.check or args.headless:
        import json
        try:
            targets = parse_targets(args.check or "5075281354358777856")
            settings = Settings(targets, proxy=detect_local_proxy() if args.proxy is None else args.proxy, demo=args.demo)
            failed = False

            def emit(event):
                nonlocal failed
                failed |= event.kind == "error"
                result = {"event": event.kind, "portfolioId": event.target, "demo": args.demo}
                if event.message:
                    result["message"] = event.message
                if event.snapshot:
                    value = event.snapshot
                    result.update(nickname=value.nickname, copiers=value.copiers, capacity=value.capacity,
                                  slots=value.slots, minimum=str(value.minimum) if value.minimum else None,
                                  status=value.state(settings.ceiling))
                print(json.dumps(result, ensure_ascii=False), flush=True)

            if args.headless and not (args.demo or args.check):
                parser.error("--headless 需要与 --demo 或 --check 一起使用")
            Monitor(settings, emit).run(once=True)
            return 1 if failed else 0
        except ValidationError as error:
            print(str(error), file=sys.stderr)
            return 2
    from .gui import make_window
    make_window(demo=args.demo, quit_after_demo=args.quit_after_demo).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
