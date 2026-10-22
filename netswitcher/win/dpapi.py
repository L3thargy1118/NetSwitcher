"""DPAPI 加解密（CurrentUser 作用域），用于 WiFi 密码。

密文只能被同一 Windows 用户解开；换用户或换机器都解不开。
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _take(blob: _BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _kernel32.LocalFree(blob.pbData)


def protect(data: bytes) -> bytes:
    out = _BLOB()
    if not _crypt32.CryptProtectData(
        ctypes.byref(_blob(data)), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    ):
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    return _take(out)


def unprotect(data: bytes) -> bytes:
    out = _BLOB()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(_blob(data)), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    ):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    return _take(out)


def encrypt_text(text: str) -> str:
    if not text:
        return ""
    return base64.b64encode(protect(text.encode("utf-8"))).decode("ascii")


def decrypt_text(token: str) -> str:
    if not token:
        return ""
    try:
        return unprotect(base64.b64decode(token.encode("ascii"))).decode("utf-8")
    except Exception:
        return ""
