"""连通性校验：网关 ping + 多目标 TCP 探活。

用 TCP 连接而不是 ICMP 判断"能不能上网"，因为 ICMP 经常被上游丢弃。
探活目标优先选国内公共 DNS 的 443/53 端口，避免 DNS 解析本身成为变量。
"""

from __future__ import annotations

import socket
import subprocess
import time

from .shell import CREATE_NO_WINDOW

PROBE_TARGETS = [
    ("223.5.5.5", 443),
    ("119.29.29.29", 443),
    ("223.6.6.6", 53),
    ("114.114.114.114", 53),
]


def tcp_ok(host: str, port: int, timeout: float = 1.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ping(host: str, timeout_ms: int = 800) -> bool:
    if not host:
        return False
    try:
        proc = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True,
            timeout=timeout_ms / 1000 + 2,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def internet() -> tuple[bool, str]:
    """返回（是否在线, 命中的探活目标）。"""
    for host, port in PROBE_TARGETS:
        if tcp_ok(host, port):
            return True, f"{host}:{port}"
    return False, "无可用出口"


def wait_online(timeout: float = 20.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = "无可用出口"
    while time.monotonic() < deadline:
        ok, detail = internet()
        if ok:
            return True, detail
        last = detail
        time.sleep(0.8)
    return False, last
