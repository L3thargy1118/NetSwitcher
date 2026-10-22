"""自动模式「手动优先」的行为测试。

真实场景（用户报的 bug）：
    在校园 WiFi 上手动切了一次网络 → 10 分钟宽限期开始
    → 随后打开系统代理，自动模式一点反应都没有
宽限期本来是为了「刚点完有线，别又被拽回热点」，但它把**之后才出现的触发**也拦住了，
结果就是自动模式像是坏的。这里把两边的边界钉死。
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from netswitcher.engine import automode as am
from netswitcher.win import wifi

CFG = {
    "enabled": True,
    "mode": "system_proxy",
    "debounce_on": 0.0,
    "debounce_off": 0.0,
    "max_retry": 3,
    "cooldown_seconds": 60,
    "manual_grace_seconds": 600,
    "process_patterns": [],
    "target_ssid": "Xiaomi 14 Pro",
}


class FakeStore:
    def __init__(self) -> None:
        self.target = {"id": "p1", "ssid": "Xiaomi 14 Pro"}

    def config(self) -> dict:
        return {"auto": dict(CFG)}

    def auto_target(self):
        return self.target

    def password_for(self, ssid: str) -> str:
        return "pw" if ssid else ""


class FakeSwitcher:
    def __init__(self) -> None:
        self.job = SimpleNamespace(running=False)
        self.manual_until = 0.0
        self.manual_trigger_active = False
        self.trigger_active = False
        self.calls: list[tuple[str, str, str]] = []
        self.journal = SimpleNamespace(pending=lambda: None)

    def snapshot(self) -> dict:
        return {"egress_kind": "wifi", "wifi_ssid": "HZCU-GONGYU"}

    def switch(self, kind: str, ssid: str = "", password: str = "", source: str = "manual") -> dict:
        self.calls.append((kind, ssid, source))
        return {"ok": True, "message": "已切换"}


class FakeLog:
    def info(self, *a, **k) -> None:
        pass

    def warn(self, *a, **k) -> None:
        pass

    def error(self, *a, **k) -> None:
        pass


class AutoGraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.active = False
        self.store = FakeStore()
        self.switcher = FakeSwitcher()

        patches = [
            mock.patch.object(am.proxy, "Watcher", lambda *a, **k: SimpleNamespace(start=lambda: None, stop=lambda: None)),
            mock.patch.object(
                am.proxy,
                "read",
                side_effect=lambda: {
                    "active": self.active,
                    "server": "127.0.0.1:7892",
                    "pac": "",
                },
            ),
            mock.patch.object(
                wifi,
                "interface",
                return_value=wifi.WifiInterface(name="WLAN", state="已断开连接"),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.engine = am.AutoEngine(self.store, self.switcher, FakeLog())

    # ------------------------------------------------------------------ 用例
    def test_manual_switch_while_already_triggered_still_defers(self):
        """手动切换时触发就已经命中 —— 尊重用户，别把他拽回热点。"""
        self.switcher.manual_until = time.monotonic() + 600
        self.switcher.manual_trigger_active = True
        self.active = True

        self.engine.tick()

        self.assertEqual(self.engine.state, "idle")
        self.assertIn("暂不接管", self.engine.reason)
        self.assertEqual(self.switcher.calls, [], "宽限期内不该动作")

    def test_trigger_appearing_after_manual_switch_is_honoured(self):
        """手动切到校园 WiFi 之后才开代理 —— 这就是用户要自动接手的时候。"""
        self.switcher.manual_until = time.monotonic() + 600
        self.switcher.manual_trigger_active = False

        self.active = False
        self.engine.tick()
        self.assertIn("暂不接管", self.engine.reason)

        self.active = True
        self.engine.tick()  # 识别到「新出现的触发」，放弃宽限
        self.assertEqual(self.switcher.manual_until, 0.0)
        self.assertEqual(self.engine.state, "wait_on")

        self.engine.tick()  # 防抖 0s，直接切
        self.assertEqual(self.switcher.calls, [("hotspot", "Xiaomi 14 Pro", "auto")])

    def test_take_over_clears_grace(self):
        self.switcher.manual_until = time.monotonic() + 600
        self.switcher.manual_trigger_active = True
        self.active = True

        self.engine.take_over_now()

        self.assertEqual(self.switcher.manual_until, 0.0)
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.switcher.calls, [("hotspot", "Xiaomi 14 Pro", "auto")])

    def test_reason_reports_remaining_seconds(self):
        self.switcher.manual_until = time.monotonic() + 300
        self.switcher.manual_trigger_active = True
        self.active = True

        self.engine.tick()

        self.assertIn("还剩", self.engine.reason)


if __name__ == "__main__":
    unittest.main()
