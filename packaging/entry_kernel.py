"""PyInstaller 入口：只跑内核（给 Electron 用）。

控制台子系统：Electron 要读它 stdout 里那一行 `{"type":"ready",...}` 才知道端口和 token。
Electron 用 windowsHide 拉起，所以用户看不到黑框。
"""

import sys

from netswitcher.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
