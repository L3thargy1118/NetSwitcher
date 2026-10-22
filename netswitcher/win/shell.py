"""PowerShell / netsh 子进程封装。

两个坑，都在这里处理掉：
1. 中文 Windows 控制台默认 GB2312 —— 统一在脚本头把 OutputEncoding 设为 UTF-8。
2. 控制台列宽会把长行硬折行，`"IP":"192.168.1.5"` 被折成两行会让 JSON 解析失败。
   所以 ps_json 让 PowerShell 把 JSON 写进临时文件，Python 读文件，绕开折行。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000

_SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = os.path.join(_SYSROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
NETSH = os.path.join(_SYSROOT, "System32", "netsh.exe")

_PREFIX = (
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "$OutputEncoding=[System.Text.Encoding]::UTF8;"
)


class ShellError(RuntimeError):
    def __init__(self, message: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs", "cp936"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise ShellError(f"命令超时（{timeout:.0f}s）") from exc
    return proc.returncode, _decode(proc.stdout) + _decode(proc.stderr)


_QUOTED_ARG = re.compile(r'^([A-Za-z][\w-]*)="(.*)"$', re.S)


def _strip_quotes(args: tuple[str, ...]) -> list[str]:
    """把 `key="value"` 还原成 `key=value`（见 netsh 的注释）。"""
    cleaned: list[str] = []
    for arg in args:
        m = _QUOTED_ARG.match(arg)
        cleaned.append(f"{m.group(1)}={m.group(2)}" if m and '"' not in m.group(2) else arg)
    return cleaned


def ps_run(script: str, timeout: float = 40.0) -> tuple[int, str]:
    """执行 PowerShell 脚本，返回（返回码, 合并输出）。"""
    argv = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _PREFIX + script,
    ]
    return _run(argv, timeout)


def ps_json(script: str, timeout: float = 40.0) -> Any:
    """执行脚本并取回 JSON 结果（经临时文件，避免控制台折行）。"""
    tmp = Path(tempfile.gettempdir()) / f"netswitcher_{uuid.uuid4().hex}.json"
    wrapper = (
        _PREFIX
        + "$__r = & {\n"
        + script
        + "\n};\n"
        + f"ConvertTo-Json -InputObject $__r -Depth 8 | Out-File -LiteralPath '{tmp}' -Encoding utf8"
    )
    try:
        rc, out = ps_run(wrapper, timeout)
        if rc != 0:
            raise ShellError("PowerShell 执行失败", stdout=out)
        try:
            raw = tmp.read_text(encoding="utf-8-sig")
        except FileNotFoundError as exc:
            raise ShellError("PowerShell 未产生输出", stdout=out) from exc
    finally:
        tmp.unlink(missing_ok=True)

    raw = raw.strip()
    if not raw or raw == "null":
        return None
    return json.loads(raw)


def netsh(*args: str, timeout: float = 25.0) -> tuple[int, str]:
    """调用 netsh，返回（返回码, 输出）。netsh 输出为本地 ANSI 代码页。

    参数里**绝对不能**带字面引号。用 subprocess 直接传给 netsh 时，引号不会被
    外层 shell 剥掉，netsh 自己的解析器遇到 `name="Xiaomi 14 Pro"` 会判定
    「参数错误」并直接退出 —— 表现就是「命令返回了，但网络毫无反应」。
    带空格的参数交给 CreateProcess 的引号规则处理即可。

    这里统一做一次兜底：把 `key="value"` 拆成 `key=value`，避免调用方再踩。
    """
    return _run([NETSH, *_strip_quotes(args)], timeout)


def as_list(value: Any) -> list:
    """PowerShell 单元素结果会退化成对象，统一成列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
