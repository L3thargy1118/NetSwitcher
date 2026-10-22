"""进程检测：用于「代理软件触发」模式。

三种取进程列表的办法，按代价从低到高：
1. Toolhelp32 快照（ctypes 直调 kernel32）—— 几毫秒，本文件默认用它；
2. tasklist 命令行 —— 机器上进程一多就要 0.5 秒；
3. PowerShell 的 Get-Process —— 每次启动约 1 秒，最慢，只在兜底时用。

后台线程每 2 秒刷一次缓存，调用方拿到的永远是缓存，不阻塞。
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import threading
import time
from ctypes import wintypes

from .shell import CREATE_NO_WINDOW, _decode  # noqa: PLC2701

DEFAULT_PATTERNS = [
    "clash*.exe",
    "verge-mihomo*.exe",
    "v2ray*.exe",
    "xray*.exe",
    "sing-box*.exe",
    "shadowsocks*.exe",
    "netch*.exe",
    "mihomo*.exe",
]

_TTL = 2.0
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_lock = threading.Lock()
_cache: set[str] = set()
_cache_at = 0.0
_watcher: threading.Thread | None = None


def _snapshot_names() -> set[str]:
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot 失败")
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        found: set[str] = set()
        if _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                found.add(entry.szExeFile.lower())
                if not _kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        return found
    finally:
        _kernel32.CloseHandle(snap)


def _tasklist_names() -> set[str]:
    proc = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        timeout=10,
        creationflags=CREATE_NO_WINDOW,
    )
    text = _decode(proc.stdout)
    return {m.group(1).strip().lower() for m in re.finditer(r'^"([^"]+)"', text, re.M)}


def _read() -> set[str]:
    try:
        return _snapshot_names()
    except Exception:
        pass
    try:
        return _tasklist_names()
    except Exception:
        return set()


def _refresh() -> None:
    global _cache, _cache_at
    fresh = _read()
    if fresh:
        with _lock:
            _cache = fresh
            _cache_at = time.monotonic()


def start_watcher(interval: float = _TTL) -> None:
    global _watcher
    if _watcher and _watcher.is_alive():
        return

    def loop() -> None:
        while True:
            _refresh()
            time.sleep(interval)

    _watcher = threading.Thread(target=loop, daemon=True, name="proc-watch")
    _watcher.start()


def names(force: bool = False) -> set[str]:
    if force or not _cache_at:
        _refresh()
    with _lock:
        return set(_cache)


def all_names() -> list[str]:
    """当前所有进程名（去重、排序），给设置页的「挑一个」用。"""
    return sorted(names())


def match(patterns: list[str] | None = None) -> list[str]:
    """返回命中的进程名列表。"""
    pats = [p.strip().lower() for p in (patterns or DEFAULT_PATTERNS) if p.strip()]
    if not pats:
        return []
    hits = []
    for proc in names():
        for pat in pats:
            if _glob(pat, proc):
                hits.append(proc)
                break
    return sorted(set(hits))


def _glob(pattern: str, text: str) -> bool:
    """只支持 * 和 ?，避免 fnmatch 对 [ ] 的意外解释。"""
    regex = "^" + "".join(
        ".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern
    ) + "$"
    return re.match(regex, text) is not None
