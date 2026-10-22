"""管理员权限检测与 UAC 重启。"""

from __future__ import annotations

import ctypes
import os
import sys


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(args: list[str] | None = None) -> bool:
    """以管理员身份重启当前程序，返回是否成功拉起。"""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = " ".join(args if args is not None else sys.argv[1:])
    else:
        exe = sys.executable
        params = " ".join(["-m", "netswitcher", *(args if args is not None else sys.argv[1:])])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, os.getcwd(), 1)
        return int(rc) > 32
    except Exception:
        return False
