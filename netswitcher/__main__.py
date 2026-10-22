"""命令行入口。

    python -m netswitcher desktop          # 窗口模式（打包后双击走的就是这个）
    python -m netswitcher serve            # 起本地服务（给 Electron 用）
    python -m netswitcher state            # 打印当前网络状态
    python -m netswitcher scan             # 扫描周边 WiFi
    python -m netswitcher switch --kind hotspot --ssid "Xiaomi 14 Pro"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser

from .runtime import bootstrap as _bootstrap
from .runtime import static_dir as _static_dir


def cmd_serve(args: argparse.Namespace) -> int:
    from .api.server import App, serve

    store, log, journal = _bootstrap()
    app = App(store, log, journal, _static_dir())
    app.start()
    httpd, token = serve(app, args.port)
    port = httpd.server_address[1]

    payload = {"type": "ready", "port": port, "token": token, "admin": __import__(
        "netswitcher.win.admin", fromlist=["is_admin"]).is_admin()}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    log.info("app", f"服务已启动 http://127.0.0.1:{port}")

    if args.open:
        webbrowser.open(f"http://127.0.0.1:{port}/?token={token}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.shutdown()
    return 0


def cmd_desktop(_: argparse.Namespace) -> int:
    from .desktop import run

    try:
        run()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def cmd_state(_: argparse.Namespace) -> int:
    from .api.server import App

    store, log, journal = _bootstrap()
    app = App(store, log, journal, _static_dir())
    from .win import health

    app._last_online = health.internet()
    print(json.dumps(app.state(), ensure_ascii=False, indent=2))
    return 0


def cmd_scan(_: argparse.Namespace) -> int:
    from .win import wifi

    for net in wifi.scan():
        print(f"{net.signal:>3}%  {net.ssid}  [{net.auth} / {net.encryption}] {net.band}")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    from .engine.switcher import BusyError, Switcher

    store, log, journal = _bootstrap()
    switcher = Switcher(store, journal, log)
    try:
        result = switcher.switch(args.kind, args.ssid or "", args.password or "")
    except BusyError as exc:
        print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False))
        return 2
    for step in result["job"]["steps"]:
        mark = {"ok": "[OK]", "fail": "[!!]", "run": "[..]", "pending": "[  ]"}.get(step["state"], "[  ]")
        print(f"{mark} {step['label']}  {step['detail']}")
    print(json.dumps({"ok": result["ok"], "message": result["message"]}, ensure_ascii=False))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # desktop 的后续参数（--no-elevate / --check / --uninstall）要原样透传，
    # 走 argparse 会被当成未知参数直接报错
    if argv[:1] == ["desktop"]:
        from .desktop import run

        try:
            run()
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    parser = argparse.ArgumentParser(prog="netswitcher", description="Windows 网络智能切换工具")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动本地服务")
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--open", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser("desktop", help="窗口模式：起服务并打开应用窗口").set_defaults(func=cmd_desktop)
    sub.add_parser("state", help="打印当前状态").set_defaults(func=cmd_state)
    sub.add_parser("scan", help="扫描周边 WiFi").set_defaults(func=cmd_scan)

    p_switch = sub.add_parser("switch", help="切换网络")
    p_switch.add_argument("--kind", required=True, choices=["ethernet", "wifi", "hotspot"])
    p_switch.add_argument("--ssid", default="")
    p_switch.add_argument("--password", default="")
    p_switch.set_defaults(func=cmd_switch)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = parser.parse_args(["serve"], namespace=args)
        args.func = cmd_serve
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
