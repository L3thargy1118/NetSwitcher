"""网卡枚举、类型判定与启停。

注意：中文系统的网卡别名可能是「以太网 2」，别名前后还可能带空格，
所以类型判定一律看 MediaType / InterfaceDescription，别名只用于展示。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .shell import ShellError, as_list, ps_json, ps_run

_QUERY = r"""
$ad  = @(Get-NetAdapter | Select-Object Name,InterfaceAlias,InterfaceDescription,Status,MediaType,ifIndex,Virtual,MacAddress,LinkSpeed)
$ip  = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object InterfaceIndex,IPAddress,PrefixLength,PrefixOrigin)
$rt  = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object ifIndex,NextHop,RouteMetric,InterfaceMetric)
$dns = @(Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object InterfaceIndex,ServerAddresses)
[pscustomobject]@{ adapters = $ad; ips = $ip; routes = $rt; dns = $dns }
"""


@dataclass
class Adapter:
    index: int
    name: str
    alias: str
    desc: str
    status: str
    media: str
    virtual: bool
    mac: str
    speed: str
    ip: str | None = None
    prefix: int | None = None
    gateway: str | None = None
    metric: int | None = None
    dns: list[str] = field(default_factory=list)

    @property
    def is_up(self) -> bool:
        """链路已连通（有载波、已关联 AP）。"""
        return self.status.lower() == "up"

    @property
    def is_enabled(self) -> bool:
        """网卡处于启用状态。

        无线网卡有三态：Up（已连上 AP）/ Disconnected（启用但没连）/ Disabled（被禁用）。
        判断「能不能用」要看这个，而不是 is_up —— 没连上时状态永远不是 Up。
        """
        return self.status.lower() not in ("disabled", "")

    @property
    def kind(self) -> str:
        media = (self.media or "").lower()
        if "802.11" in media or "native 802.11" in media:
            return "wifi"
        if self.virtual:
            return "virtual"
        if "802.3" in media:
            return "ethernet"
        return "other"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "alias": self.alias,
            "desc": self.desc,
            "status": self.status,
            "media": self.media,
            "virtual": self.virtual,
            "mac": self.mac,
            "speed": self.speed,
            "ip": self.ip,
            "gateway": self.gateway,
            "metric": self.metric,
            "dns": self.dns,
            "kind": self.kind,
            "up": self.is_up,
        }


def _txt(value: Any) -> str:
    return ("" if value is None else str(value)).strip()


def list_adapters() -> list[Adapter]:
    data = ps_json(_QUERY) or {}
    ips = as_list(data.get("ips"))
    routes = as_list(data.get("routes"))
    dns = as_list(data.get("dns"))

    ip_by_index: dict[int, tuple[str, int]] = {}
    for row in ips:
        idx = row.get("InterfaceIndex")
        addr = _txt(row.get("IPAddress"))
        if idx is None or not addr or addr.startswith("169.254."):
            continue
        ip_by_index.setdefault(int(idx), (addr, int(row.get("PrefixLength") or 0)))

    route_by_index: dict[int, tuple[str, int]] = {}
    for row in routes:
        idx = row.get("ifIndex")
        hop = _txt(row.get("NextHop"))
        if idx is None or not hop:
            continue
        total = int(row.get("RouteMetric") or 0) + int(row.get("InterfaceMetric") or 0)
        prev = route_by_index.get(int(idx))
        if prev is None or total < prev[1]:
            route_by_index[int(idx)] = (hop, total)

    dns_by_index: dict[int, list[str]] = {}
    for row in dns:
        idx = row.get("InterfaceIndex")
        if idx is None:
            continue
        servers = as_list(row.get("ServerAddresses"))
        dns_by_index[int(idx)] = [str(s) for s in servers if str(s).strip()]

    out: list[Adapter] = []
    for row in as_list(data.get("adapters")):
        idx = int(row.get("ifIndex") or 0)
        ip_pair = ip_by_index.get(idx)
        route = route_by_index.get(idx)
        out.append(
            Adapter(
                index=idx,
                name=_txt(row.get("Name")),
                alias=_txt(row.get("InterfaceAlias")),
                desc=_txt(row.get("InterfaceDescription")),
                status=_txt(row.get("Status")) or "Unknown",
                media=_txt(row.get("MediaType")),
                virtual=bool(row.get("Virtual")),
                mac=_txt(row.get("MacAddress")),
                speed=_txt(row.get("LinkSpeed")),
                ip=ip_pair[0] if ip_pair else None,
                prefix=ip_pair[1] if ip_pair else None,
                gateway=route[0] if route else None,
                metric=route[1] if route else None,
                dns=dns_by_index.get(idx, []),
            )
        )
    out.sort(key=lambda a: a.index)
    return out


def current_egress(adapters: list[Adapter]) -> Adapter | None:
    """当前出口 = Up 且有网关的网卡里，metric 最小的那个。"""
    candidates = [a for a in adapters if a.is_up and a.gateway]
    if not candidates:
        return None
    return min(candidates, key=lambda a: a.metric if a.metric is not None else 99999)


def find_wifi(adapters: list[Adapter]) -> Adapter | None:
    for a in adapters:
        if a.kind == "wifi":
            return a
    return None


def find_ethernets(adapters: list[Adapter]) -> list[Adapter]:
    return [a for a in adapters if a.kind == "ethernet"]


def set_enabled(adapter: Adapter, enabled: bool) -> tuple[bool, str]:
    verb = "Enable-NetAdapter" if enabled else "Disable-NetAdapter"
    script = f"{verb} -Name '{adapter.name}' -Confirm:$false -ErrorAction Stop; 'ok'"
    try:
        rc, out = ps_run(script, timeout=30)
    except ShellError as exc:
        return False, str(exc)
    if rc != 0 or "ok" not in out:
        return False, out.strip() or f"{verb} 失败"
    return True, "已启用" if enabled else "已禁用"


def wait_status(adapter_name: str, want_up: bool, timeout: float = 15.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for a in list_adapters():
            if a.name == adapter_name and a.is_up == want_up:
                return True
        time.sleep(0.4)
    return False


def wait_enabled(adapter_name: str, timeout: float = 12.0) -> bool:
    """等待网卡从 Disabled 变成可用（不等于已连上网络）。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for a in list_adapters():
            if a.name == adapter_name and a.is_enabled:
                return True
        time.sleep(0.4)
    return False


def wait_ip(adapter_name: str, timeout: float = 18.0) -> str | None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for a in list_adapters():
            if a.name == adapter_name and a.is_up and a.ip:
                return a.ip
        time.sleep(0.5)
    return None
