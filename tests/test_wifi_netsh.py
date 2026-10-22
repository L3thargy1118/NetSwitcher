"""netsh 无线层的回归测试。

全部针对真机上踩到的问题，不是在测实现细节：

1. **带空格的 SSID 连不上**：`name="Xiaomi 14 Pro"` 里的字面引号会让 netsh 判定
   参数错误并直接退出（rc=1），而旧代码不看返回码，只是干等 18 秒直到超时。
   表现为「名字密码都对，但一点反应都没有」。
2. **返回码被忽略**：netsh 报「已成功完成连接请求」不代表连上了，反之报错也必须立刻失败。
3. **改密码**：老配置存在时不能让新密码石沉大海，且要保留原有 WPA3 认证方式。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from netswitcher.win import shell, wifi


class NetshArgTest(unittest.TestCase):
    def test_strip_quotes_inside_args(self):
        got = shell._strip_quotes(('name="Xiaomi 14 Pro"', 'ssid="Xiaomi 14 Pro"', "mode=bssid"))
        self.assertEqual(got, ["name=Xiaomi 14 Pro", "ssid=Xiaomi 14 Pro", "mode=bssid"])

    def test_netsh_never_sends_literal_quotes(self):
        seen: list[list[str]] = []

        def fake_run(argv, timeout):
            seen.append(argv)
            return 0, ""

        with mock.patch.object(shell, "_run", side_effect=fake_run):
            shell.netsh("wlan", "connect", 'name="Xiaomi 14 Pro"', 'interface="WLAN"')

        self.assertEqual(seen[0][-2:], ["name=Xiaomi 14 Pro", "interface=WLAN"])
        self.assertFalse(any('"' in arg for arg in seen[0]), "参数里不能残留引号")


class ConnectTest(unittest.TestCase):
    def test_rejected_request_fails_fast(self):
        """netsh 拒绝时立刻失败，不能让用户等满 18 秒。"""
        with mock.patch.object(wifi, "netsh", return_value=(1, "该命令的一个或多个参数错误或缺少。")):
            with mock.patch.object(wifi, "interface") as iface:
                started = time.monotonic()
                ok, msg = wifi.connect("Xiaomi 14 Pro", timeout=18)
                elapsed = time.monotonic() - started

        self.assertFalse(ok)
        self.assertLess(elapsed, 1.0, "应该立即返回，不进入轮询")
        self.assertIn("参数错误", msg)
        # 只为取网卡名查一次，不该进入 0.5s 轮询
        self.assertLessEqual(iface.call_count, 1)

    def test_success_requires_real_association(self):
        """netsh 回了「已成功」但状态没起来，不能算成功。"""
        states = iter(
            [wifi.WifiInterface(name="WLAN", state="关联")]
            + [wifi.WifiInterface(name="WLAN", state="已连接", ssid="Xiaomi 14 Pro")] * 5
        )
        with mock.patch.object(wifi, "netsh", return_value=(0, "已成功完成连接请求。")):
            with mock.patch.object(wifi, "interface", side_effect=lambda: next(states)):
                with mock.patch.object(wifi.time, "sleep", return_value=None):
                    ok, msg = wifi.connect("Xiaomi 14 Pro", timeout=5)
        self.assertTrue(ok, msg)


class TimeoutReasonTest(unittest.TestCase):
    def test_auth_stage_blames_password(self):
        reason = wifi._timeout_reason("X", ["已断开连接", "正在验证", "已断开连接"])
        self.assertIn("密码", reason)

    def test_not_visible_blames_hotspot(self):
        with mock.patch.object(wifi, "scan", return_value=[]):
            reason = wifi._timeout_reason("X", ["已断开连接"])
        self.assertIn("扫不到", reason)


class PasswordSyncTest(unittest.TestCase):
    XML = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>Xiaomi 14 Pro</name>
  <MSM><security><authEncryption>
    <authentication>WPA3SAE</authentication><encryption>AES</encryption>
    <transitionMode xmlns="http://www.microsoft.com/networking/WLAN/profile/v4">true</transitionMode>
  </authEncryption><sharedKey><keyType>passPhrase</keyType><protected>false</protected>
  <keyMaterial>old-password</keyMaterial></sharedKey></security></MSM>
</WLANProfile>
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.xml = Path(self.tmp.name) / "WLAN-Xiaomi 14 Pro.xml"
        self.xml.write_text(self.XML, encoding="utf-8")
        self.added: list[str] = []

    def _fake_add(self, path):
        self.added.append(Path(path).read_text(encoding="utf-8"))
        return True, "已将配置文件添加"

    def test_password_rewritten_and_auth_preserved(self):
        with mock.patch.object(wifi, "_export_profile", return_value=self.xml):
            with mock.patch.object(wifi, "_add_profile_file", side_effect=self._fake_add):
                ok, msg = wifi.sync_password("Xiaomi 14 Pro", "114514114514")

        self.assertTrue(ok, msg)
        self.assertEqual(msg, "已更新密码")
        self.assertEqual(len(self.added), 1)
        written = self.added[0]
        self.assertIn("<keyMaterial>114514114514</keyMaterial>", written)
        self.assertNotIn("old-password", written)
        self.assertIn("WPA3SAE", written, "不能把 WPA3 降级成 WPA2")

    def test_same_password_does_not_touch_profile(self):
        with mock.patch.object(wifi, "_export_profile", return_value=self.xml):
            with mock.patch.object(wifi, "_add_profile_file") as add:
                ok, msg = wifi.sync_password("Xiaomi 14 Pro", "old-password")
        self.assertTrue(ok)
        self.assertEqual(msg, "密码未变")
        add.assert_not_called()

    def test_existing_profile_gets_new_password_only_when_user_typed_one(self):
        with mock.patch.object(wifi, "profiles", return_value=["Xiaomi 14 Pro"]):
            with mock.patch.object(wifi, "sync_password", return_value=(True, "已更新密码")) as sync:
                ok, msg = wifi.ensure_profile("Xiaomi 14 Pro", "new-pass", sync=True)
        self.assertTrue(ok)
        self.assertEqual(msg, "已更新密码")
        sync.assert_called_once_with("Xiaomi 14 Pro", "new-pass")

    def test_stored_password_never_overwrites_working_profile(self):
        """库里存的密码可能是错的（系统里那份才对），不能拿去覆盖。"""
        with mock.patch.object(wifi, "profiles", return_value=["Xiaomi 14 Pro"]):
            with mock.patch.object(wifi, "sync_password") as sync:
                ok, msg = wifi.ensure_profile("Xiaomi 14 Pro", "stale-pass")
        self.assertTrue(ok)
        self.assertEqual(msg, "配置文件已存在")
        sync.assert_not_called()

    def test_existing_profile_without_password_is_left_alone(self):
        with mock.patch.object(wifi, "profiles", return_value=["Xiaomi 14 Pro"]):
            with mock.patch.object(wifi, "sync_password") as sync:
                ok, _ = wifi.ensure_profile("Xiaomi 14 Pro", "")
        self.assertTrue(ok)
        sync.assert_not_called()


class ProfileXmlTest(unittest.TestCase):
    def test_wpa3_network_gets_sae_profile(self):
        xml = wifi._profile_xml("Xiaomi 14 Pro", "pass", False, auth_kind="WPA3 - 个人")
        self.assertIn("WPA3SAE", xml)
        self.assertIn("transitionMode", xml)

    def test_wpa2_network_gets_psk_profile(self):
        xml = wifi._profile_xml("Old", "pass", False, auth_kind="WPA2 - 个人")
        self.assertIn("WPA2PSK", xml)
        self.assertNotIn("WPA3SAE", xml)

    def test_open_network_has_no_key(self):
        xml = wifi._profile_xml("HZCU-GONGYU", "", True, auth_kind="开放")
        self.assertIn("<authentication>open</authentication>", xml)
        self.assertNotIn("keyMaterial", xml)


if __name__ == "__main__":
    unittest.main()
