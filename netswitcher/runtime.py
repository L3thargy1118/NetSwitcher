"""运行期公共件：静态目录定位 + 内核各部件装配。

单独放一个模块，是因为 `__main__`（命令行）和 `desktop`（打包后的窗口模式）都要用，
而 `desktop` 不适合反过来 import `__main__`。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import ConfigStore
from .journal import Journal
from .logbuf import Logger


def static_dir() -> Path:
    """界面静态文件目录。

    PyInstaller 打包后资源落在 `sys._MEIPASS`；源码运行时在同级 `frontend/renderer`。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "frontend" / "renderer"
    return Path(__file__).resolve().parent.parent / "frontend" / "renderer"


def bootstrap() -> tuple[ConfigStore, Logger, Journal]:
    store = ConfigStore()
    log = Logger(store.dir / "logs")
    journal = Journal(store.dir / "journal.json")
    return store, log, journal
