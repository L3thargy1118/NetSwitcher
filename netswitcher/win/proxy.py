"""系统代理状态：读取 + 变更监听。

监听用 RegNotifyChangeKeyValue（毫秒级），拿不到句柄时降级为 400ms 轮询。
"""

from __future__ import annotations

import ctypes
import threading
import time
import winreg
from ctypes import wintypes

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

_REG_NOTIFY_CHANGE_LAST_SET = 0x00000004
_KEY_NOTIFY = 0x0010
_KEY_READ = 0x0001

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_advapi32.RegNotifyChangeKeyValue.argtypes = [
    ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.HANDLE, wintypes.BOOL
]
_advapi32.RegNotifyChangeKeyValue.restype = wintypes.LONG
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def read() -> dict:
    """读取当前系统代理设置。"""
    enabled = 0
    server = ""
    pac = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            try:
                enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            except FileNotFoundError:
                enabled = 0
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except FileNotFoundError:
                server = ""
            try:
                pac = str(winreg.QueryValueEx(key, "AutoConfigURL")[0] or "")
            except FileNotFoundError:
                pac = ""
    except OSError:
        pass

    active = bool(enabled) and bool(server or pac)
    return {
        "enabled": bool(enabled),
        "server": server,
        "pac": pac,
        "active": active,
        "summary": f"{server or pac}" if active else "未开启",
    }


def set_enabled(enabled: bool) -> bool:
    """改写系统代理开关（可选功能，仅在用户开启「切换期间临时关闭」时使用）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enabled else 0)
        return True
    except OSError:
        return False


class Watcher(threading.Thread):
    """后台监听注册表变化，状态真正改变时回调 on_change(新状态)。"""

    def __init__(self, on_change=None) -> None:
        super().__init__(daemon=True, name="proxy-watcher")
        self._on_change = on_change
        self._stop = threading.Event()
        self._last = read()

    @property
    def state(self) -> dict:
        return self._last

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, state: dict) -> None:
        self._last = state
        if self._on_change:
            try:
                self._on_change(state)
            except Exception:
                pass

    def run(self) -> None:
        if not self._notify_loop():
            self._poll_loop()

    def _notify_loop(self) -> bool:
        """返回 False 表示该路径不可用，交给轮询兜底。"""
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REG_PATH, 0, _KEY_NOTIFY | _KEY_READ
            )
        except OSError:
            return False

        try:
            handle = ctypes.c_void_p(int(key.handle))
            while not self._stop.is_set():
                event = _kernel32.CreateEventW(None, False, False, None)
                if not event:
                    return False
                try:
                    rc = _advapi32.RegNotifyChangeKeyValue(
                        handle, True, _REG_NOTIFY_CHANGE_LAST_SET, event, True
                    )
                    if rc != 0:
                        return False
                    _kernel32.WaitForSingleObject(event, 500)
                finally:
                    _kernel32.CloseHandle(event)
                fresh = read()
                if fresh["enabled"] != self._last["enabled"] or fresh["server"] != self._last["server"]:
                    self._emit(fresh)
        except Exception:
            return False
        finally:
            try:
                key.Close()
            except Exception:
                pass
        return True

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.4)
            fresh = read()
            if fresh["enabled"] != self._last["enabled"] or fresh["server"] != self._last["server"]:
                self._emit(fresh)
