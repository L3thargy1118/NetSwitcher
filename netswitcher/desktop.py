"""窗口模式：起内核，再开一个只显示本程序的独立窗口。

为什么不用 PySide/PyQt/pywebview：
    那些都要额外运行时，包体会翻好几倍（Qt 约 80MB，pywebview 还要带 .NET 绑定）。
    Edge 的 `--app=` 本来就是无地址栏、无标签页的独立窗口，Win10/11 又必然自带 Edge，
    零依赖、零安装，视觉上和原生应用没区别。万一没有 Edge 就退回默认浏览器。

打包后双击 NetSwitcher.exe 走的就是这里。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "NetSwitcher"
WINDOW_SIZE = "1200,900"

_MB_YESNO = 0x04
_MB_ICONQUESTION = 0x20
_MB_ICONERROR = 0x10
_MB_OK = 0x00


def _msgbox(text: str, title: str = APP_NAME, flags: int = _MB_OK) -> int:
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:  # noqa: BLE001
        return 0


def _edge() -> Path | None:
    roots = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if candidate.is_file():
            return candidate
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        ) as key:
            path = winreg.QueryValue(key, None)
            if path and Path(path).is_file():
                return Path(path)
    except OSError:
        pass
    return None


def open_window(url: str) -> subprocess.Popen | None:
    """打开应用窗口；返回进程句柄，没有 Edge 时返回 None（已退化为默认浏览器）。"""
    edge = _edge()
    if edge is None:
        import webbrowser

        webbrowser.open(url)
        return None

    # 独立 profile：避免和用户日常 Edge 抢实例（否则 --app 会把窗口塞进已有浏览器进程，
    # 我们也就没法用进程退出判断窗口关闭）
    profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetSwitcher" / "ui-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            str(edge),
            f"--app={url}",
            f"--window-size={WINDOW_SIZE}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,msEdgeSplitScreen",
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _elevate(args: list[str]) -> bool:
    """用管理员身份重新拉起自己。用户点掉 UAC 就返回 False。"""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = subprocess.list2cmdline(args)
        workdir = str(Path(sys.executable).parent)
    else:
        exe = sys.executable
        params = subprocess.list2cmdline(["-m", "netswitcher", "desktop", *args])
        workdir = os.getcwd()
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
    except Exception:  # noqa: BLE001
        return False
    return int(rc) > 32  # >32 才是真的启动成功


def _self_check() -> int:
    """打包自检：确认依赖能导入、静态资源在包里。给构建脚本用。"""
    try:
        from .api.server import App  # noqa: F401  只是验证能导入
        from .runtime import static_dir

        index = static_dir() / "index.html"
        return 0 if index.is_file() else 1
    except Exception:  # noqa: BLE001
        return 1


def _shortcuts() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return [
        home / "Desktop" / f"{APP_NAME}.lnk",
        programs / f"{APP_NAME}.lnk",
        programs / APP_NAME / f"{APP_NAME}.lnk",
    ]


def uninstall() -> int:
    """卸载：删快捷方式、注册表项、安装目录，可选删配置。"""
    if not getattr(sys, "frozen", False):
        print("只有安装目录里的 NetSwitcher.exe 支持卸载。")
        return 2

    if _msgbox(f"确定要卸载 {APP_NAME} 吗？", APP_NAME, _MB_YESNO | _MB_ICONQUESTION) != 6:
        return 0

    remove_cfg = (
        _msgbox(
            "是否同时删除配置与日志？\n\n选择「是」将清除网络库里保存的 WiFi 与热点、所有设置和日志。",
            APP_NAME,
            _MB_YESNO | _MB_ICONQUESTION,
        )
        == 6
    )

    for path in _shortcuts():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        import winreg

        winreg.DeleteKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}",
        )
    except OSError:
        pass

    if remove_cfg:
        import shutil

        cfg = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
        shutil.rmtree(cfg, ignore_errors=True)

    # 自己正被占用，交给 cmd 延迟删除
    root = Path(sys.executable).parent
    subprocess.Popen(
        f'ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{root}"',
        shell=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return 0


def _cli_args() -> list[str]:
    """`python -m netswitcher desktop --check` 时把命令名本身剔掉。"""
    tail = sys.argv[1:]
    return tail[1:] if tail[:1] == ["desktop"] else tail


def main(argv: list[str] | None = None) -> int:
    args = list(_cli_args() if argv is None else argv)
    if "--check" in args:
        return _self_check()
    if "--uninstall" in args:
        return uninstall()

    if not _is_admin() and "--no-elevate" not in args:
        if _elevate(args):
            return 0  # 提权后的新进程接管窗口，本进程退出
        _msgbox(
            "没有拿到管理员权限，将以受限模式运行。\n\n"
            "界面可以正常查看，但启停网卡会被拒绝，网络切换会失败。",
            APP_NAME,
            _MB_ICONERROR,
        )

    from .api.server import App, serve
    from .runtime import bootstrap, static_dir

    store, log, journal = bootstrap()
    app = App(store, log, journal, static_dir())
    app.start()
    httpd, token = serve(app, 0)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={token}"
    log.info("app", f"窗口模式已启动 {url}")

    proc = open_window(url)
    started = time.monotonic()
    try:
        if proc is None:
            # 退化到默认浏览器时没法判断窗口何时关闭，只能常驻
            while True:
                time.sleep(1)
        else:
            _wait_for_window(app, started, url)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.shutdown()
    return 0


# 窗口关闭后等这么久仍无请求，才认为页面真的走了（给刷新留出余量）
_GONE_GRACE = 5.0
# 兜底：这么久一次请求都没有，说明窗口早就没了。
# 取 90s 是因为窗口最小化 5 分钟后，Chromium 会把定时器限流到每分钟一次。
_IDLE_TIMEOUT = 90.0
# 一直没连上（Edge 没起来）的话，改用默认浏览器兜底
_FALLBACK_AFTER = 25.0
# 兜底后仍然没连上就收工
_NEVER_CONNECTED_TIMEOUT = 90.0


def _wait_for_window(app, started: float, url: str) -> None:
    """等着界面消失。

    不能用「Edge 子进程退出」当信号：Chromium 在 Windows 上是启动器 + 浏览器两段式，
    启动器常常交代完就退出，而窗口还开着。旧代码据此结束进程、顺手关掉了 HTTP 服务，
    用户看到的就是窗口里「拒绝连接」。所以改看协议层：页面每 1.5 秒轮询一次，
    没请求了才算关；前端还会在 pagehide 时补一个 /api/bye 让退出更及时。
    """
    fell_back = False
    while True:
        time.sleep(1)
        now = time.monotonic()

        if app.client_gone and now - app.client_gone >= _GONE_GRACE and app.last_hit < app.client_gone:
            app.log.info("app", "窗口已关闭，进程退出")
            return
        if app.last_hit and now - app.last_hit > _IDLE_TIMEOUT:
            app.log.info("app", f"界面 {_IDLE_TIMEOUT:.0f}s 没有任何请求，按关闭处理并退出")
            return

        # Edge 没能把窗口开出来（被策略拦、profile 坏了……）：换默认浏览器，别让用户干等
        if not fell_back and not app.last_hit and now - started > _FALLBACK_AFTER:
            fell_back = True
            log_path = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME / "logs"
            _msgbox(
                f"Edge 窗口没能连上内核（{url}），改用默认浏览器打开。\n\n"
                f"如果浏览器也没弹出来，把上面的地址复制到浏览器里即可。\n日志：{log_path}",
                APP_NAME,
            )
            import webbrowser

            webbrowser.open(url)

        if not app.last_hit and now - started > _NEVER_CONNECTED_TIMEOUT:
            return


def run() -> None:
    """带兜底提示的入口：异常时弹窗，而不是静默退出。"""
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        from .config import app_dir

        log_dir = app_dir() / "logs"
        _msgbox(
            f"{APP_NAME} 启动失败：\n\n{exc}\n\n详细日志：{log_dir}",
            APP_NAME,
            _MB_ICONERROR,
        )
        raise
