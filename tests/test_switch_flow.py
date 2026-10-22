"""切换流程的回归测试。

重点盯住一个真实踩过的坑：
无线网卡的状态有 Up / Disconnected / Disabled 三种，处于 Disconnected 时它是「已启用」的，
但永远等不到 Up —— 老代码在这里死等 12 秒然后误判失败。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from netswitcher import config as cfgmod
from netswitcher.config import ConfigStore
from netswitcher.engine import switcher as sw
from netswitcher.engine.switcher import Switcher
from netswitcher.journal import Journal
from netswitcher.logbuf import Logger
from netswitcher.win import adapters as ad
from netswitcher.win import health, wifi


def _adapter(**kw):
    base = dict(
        index=1,
        name="NIC",
        alias="NIC",
        desc="",
        status="Up",
        media="802.3",
        virtual=False,
        mac="00-00-00-00-00-00",
        speed="1 Gbps",
    )
    base.update(kw)
    return ad.Adapter(**base)


ETH = _adapter(index=9, name="以太网 2", alias="以太网 2", status="Up", ip="10.62.100.116", gateway="10.62.0.1", metric=5)
WLAN = _adapter(index=19, name="WLAN", alias="WLAN", status="Disconnected", media="Native 802.11")


class AdapterStateTest(unittest.TestCase):
    def test_disconnected_wifi_counts_as_enabled(self):
        self.assertFalse(WLAN.is_up)
        self.assertTrue(WLAN.is_enabled)

    def test_disabled_wifi_counts_as_disabled(self):
        nic = _adapter(status="Disabled", media="Native 802.11")
        self.assertFalse(nic.is_enabled)

    def test_kind_from_media_type(self):
        self.assertEqual(WLAN.kind, "wifi")
        self.assertEqual(ETH.kind, "ethernet")


class WirelessFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        patcher = mock.patch.object(cfgmod, "app_dir", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.switcher = Switcher(ConfigStore(), Journal(self.root / "journal.json"), Logger(self.root / "logs"))
        self.calls: list[tuple[str, bool]] = []

        patches = [
            mock.patch.object(sw, "is_admin", return_value=True),
            mock.patch.object(ad, "list_adapters", return_value=[ETH, WLAN]),
            mock.patch.object(ad, "set_enabled", side_effect=self._set_enabled),
            mock.patch.object(ad, "wait_enabled", return_value=True),
            mock.patch.object(ad, "wait_ip", return_value="192.168.1.5"),
            mock.patch.object(wifi, "interface", return_value=wifi.WifiInterface(name="WLAN", state="已断开连接")),
            mock.patch.object(wifi, "ensure_profile", return_value=(True, "ok")),
            mock.patch.object(wifi, "connect", return_value=(True, "已连接")),
            mock.patch.object(health, "wait_online", return_value=(True, "223.5.5.5:443")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _set_enabled(self, nic, enabled):
        self.calls.append((nic.name, enabled))
        return True, "ok"

    def test_switch_to_wifi_does_not_hang_on_disconnected_adapter(self):
        started = time.monotonic()
        result = self.switcher.switch("wifi", "HZCU-GONGYU", "")
        elapsed = time.monotonic() - started

        self.assertTrue(result["ok"], result["message"])
        self.assertLess(elapsed, 3.0, "不该为了等无线网卡 Up 而阻塞")
        # 已经启用的无线网卡不该被重复启用
        self.assertNotIn(("WLAN", True), self.calls)
        # 有线必须被硬断
        self.assertIn(("以太网 2", False), self.calls)

        states = {s["key"]: s["state"] for s in result["job"]["steps"]}
        for key in ("detach", "enable", "assoc", "ip", "online"):
            self.assertEqual(states[key], "ok", f"{key} 应该是 ok，实际 {states[key]}")

    def test_profile_failure_does_not_cut_the_cable(self):
        """配置备不齐时不许先断有线，否则白断一次网。"""
        with mock.patch.object(wifi, "ensure_profile", return_value=(False, "系统里没有该网络的配置，且未提供密码")):
            result = self.switcher.switch("wifi", "NEED-PASSWORD", "")
        self.assertFalse(result["ok"])
        self.assertNotIn(("以太网 2", False), self.calls, "断网动作不该发生")

    def test_switch_reports_chinese_step_name_on_failure(self):
        with mock.patch.object(wifi, "connect", return_value=(False, "连接超时（状态：已断开连接）")):
            result = self.switcher.switch("wifi", "HZCU-GONGYU", "")
        self.assertFalse(result["ok"])
        self.assertIn("连接网络失败", result["message"])

    def test_successful_wifi_is_added_to_library(self):
        """连上过的网络要自动进网络库，下次不用再找密码。"""
        result = self.switcher.switch("wifi", "HZCU-GONGYU", "")
        self.assertTrue(result["ok"], result["message"])
        ssids = [p["ssid"] for p in self.switcher.store.profiles()]
        self.assertIn("HZCU-GONGYU", ssids)

    def test_auto_collect_does_not_overwrite_user_entry(self):
        self.switcher.store.upsert_profile({"ssid": "HZCU-GONGYU", "type": "hotspot", "note": "手加的"})
        self.switcher.switch("wifi", "HZCU-GONGYU", "")
        item = next(p for p in self.switcher.store.profiles() if p["ssid"] == "HZCU-GONGYU")
        self.assertEqual(item["type"], "hotspot", "用户手动标的类型不能被自动收录改掉")
        self.assertEqual(item["note"], "手加的")


if __name__ == "__main__":
    unittest.main()
