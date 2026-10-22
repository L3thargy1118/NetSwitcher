"""窗口模式的生命周期测试。

盯的是用户真实遇到的那个问题：打包后双击，窗口里显示「拒绝连接」。

根因不在 Edge 能不能打开，而在**进程退得太早**：Chromium 在 Windows 上是启动器 + 浏览器
两段式，启动器交代完就退出。老代码拿 `proc.wait()` 当「窗口还开着」的信号，启动器一退，
主进程就把 HTTP 服务关了 —— 窗口还在，连上去自然是拒绝连接。

现在改成看协议层：页面每 1.5 秒轮询，没请求了才算关。下面两个用例把这个行为钉住。
"""

from __future__ import annotations

import threading
import time
import unittest
import urllib.request
from unittest import mock

from netswitcher import desktop


class _FakeWindow:
    """假装是 open_window 的返回值：只要不是 None，就会走心跳分支。"""

    def wait(self) -> None:  # 旧代码会调它；新代码不该依赖
        raise AssertionError("不该再用 Edge 进程退出作为窗口关闭信号")


class DesktopLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.url: str | None = None
        self.token = ""
        self.addCleanup(self._stop)

        def fake_open(url: str) -> _FakeWindow:
            self.url = url
            self.token = url.split("token=")[1]
            return _FakeWindow()

        patcher = mock.patch.object(desktop, "open_window", side_effect=fake_open)
        patcher.start()
        self.addCleanup(patcher.stop)

        # --no-elevate：否则 main() 会去请求 UAC 然后立刻返回，测试里拿不到窗口
        self.thread = threading.Thread(
            target=desktop.main, args=(["--no-elevate"],), daemon=True
        )
        self.thread.start()

        deadline = time.monotonic() + 20
        while self.url is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.url is None:
            raise AssertionError("窗口模式没有启动")

    def _stop(self) -> None:
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            # 线程是 daemon，测试结束时不会拖住进程，但要让后续用例拿到干净状态
            pass

    # ------------------------------------------------------------------
    def _get(self, path: str) -> int:
        req = urllib.request.Request(f"{self.url.split('/?token=')[0]}{path}", headers={"X-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status
        except Exception:  # noqa: BLE001
            return 0

    def test_survives_launcher_exit(self):
        """页面还在轮询时，进程必须活着 —— 老代码这里就挂了。"""
        for _ in range(4):
            self.assertEqual(self._get("/api/state"), 200)
            self.assertTrue(self.thread.is_alive(), "页面还在轮询，进程不该退出")
            time.sleep(1.5)

    def test_exits_after_page_says_bye(self):
        """窗口关闭（页面发 /api/bye）后要在几秒内收工。"""
        self.assertEqual(self._get("/api/state"), 200)
        self.assertEqual(self._get(f"/api/bye?token={self.token}"), 200)

        self.thread.join(timeout=12)
        self.assertFalse(self.thread.is_alive(), "关窗口后进程应自行退出")

    def test_reload_does_not_kill_the_process(self):
        """刷新页面会先触发 bye 再重新轮询，不能因此把进程关掉。"""
        self.assertEqual(self._get("/api/state"), 200)
        self.assertEqual(self._get(f"/api/bye?token={self.token}"), 200)
        time.sleep(3)  # 落在 5 秒宽限期内
        self.assertEqual(self._get("/api/state"), 200)  # 新页面来了
        time.sleep(4)
        self.assertTrue(self.thread.is_alive(), "刷新后进程应该继续跑")


if __name__ == "__main__":
    unittest.main()
