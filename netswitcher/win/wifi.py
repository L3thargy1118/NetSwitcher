"""WiFi 扫描、连接、配置文件管理（netsh wlan 封装）。

netsh 的中文输出全角半角混用、冒号两侧空格数量不定，因此统一走「按第一个冒号切分」
的宽容解析，并同时兼容英文键名。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

from .shell import netsh

_SEP = re.compile(r"[:：]")
_STATE_CONNECTED = {"已连接", "connected"}
_STATE_DISCONNECTED = {"已断开连接", "disconnected"}
# 关联/验证/取地址 —— 出现这些说明网卡确实在尝试，失败原因大概率在密码或认证方式
_STATE_TRYING = ("关联", "正在连接", "正在验证", "正在识别", "正在获取", "associating", "authenticating")


def _split(line: str) -> tuple[str, str] | None:
    m = _SEP.search(line)
    if not m:
        return None
    key = line[: m.start()].strip()
    val = line[m.end():].strip()
    if not key:
        return None
    return key, val


def _norm(value: str) -> str:
    return value.strip().strip("：:").strip()


@dataclass
class WifiInterface:
    name: str = ""
    state: str = ""
    ssid: str = ""
    signal: int | None = None
    band: str = ""
    auth: str = ""
    profile: str = ""

    @property
    def connected(self) -> bool:
        return self.state.lower() in _STATE_CONNECTED or self.state in _STATE_CONNECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "ssid": self.ssid,
            "signal": self.signal,
            "band": self.band,
            "auth": self.auth,
            "profile": self.profile,
            "connected": self.connected,
        }


@dataclass
class WifiNetwork:
    ssid: str
    auth: str = ""
    encryption: str = ""
    signal: int = 0
    band: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ssid": self.ssid,
            "auth": self.auth,
            "encryption": self.encryption,
            "signal": self.signal,
            "band": self.band,
            "open": ("开放" in self.auth) or ("open" in self.auth.lower()),
        }


def interface() -> WifiInterface:
    """读取第一个无线接口的当前状态。"""
    rc, out = netsh("wlan", "show", "interfaces")
    info = WifiInterface()
    if rc != 0:
        return info
    for line in out.splitlines():
        if line.strip().startswith("系统上") or "There is" in line:
            continue
        kv = _split(line)
        if not kv:
            continue
        key, val = kv
        key = _norm(key)
        if key in ("名称", "Name") and not info.name:
            info.name = val
        elif key in ("状态", "State") and not info.state:
            info.state = val
        elif key == "SSID" and not info.ssid:
            info.ssid = val
        elif key in ("信号", "Signal"):
            num = re.search(r"(\d+)", val)
            if num:
                info.signal = int(num.group(1))
        elif key in ("波段", "Band") and not info.band:
            info.band = val
        elif key in ("身份验证", "Authentication") and not info.auth:
            info.auth = val
        elif key in ("配置文件", "Profile") and not info.profile:
            info.profile = val
    return info


def scan() -> list[WifiNetwork]:
    rc, out = netsh("wlan", "show", "networks", "mode=bssid")
    if rc != 0:
        return []

    networks: list[WifiNetwork] = []
    current: WifiNetwork | None = None
    header = re.compile(r"^\s*SSID\s+(\d+)\s*[:：]\s*(.*)$")

    for line in out.splitlines():
        m = header.match(line)
        if m:
            name = m.group(2).strip()
            current = WifiNetwork(ssid=name) if name else None
            if current:
                networks.append(current)
            continue
        if current is None:
            continue
        kv = _split(line)
        if not kv:
            continue
        key, val = _norm(kv[0]), kv[1]
        if key in ("身份验证", "Authentication"):
            current.auth = val
        elif key in ("加密", "Encryption"):
            current.encryption = val
        elif key in ("信号", "Signal"):
            num = re.search(r"(\d+)", val)
            if num:
                current.signal = max(current.signal, int(num.group(1)))
        elif key in ("波段", "Band") and not current.band:
            current.band = val

    # 同一 SSID 可能有多个 BSSID，合并保留最强信号
    merged: dict[str, WifiNetwork] = {}
    for n in networks:
        old = merged.get(n.ssid)
        if old is None:
            merged[n.ssid] = n
        else:
            old.signal = max(old.signal, n.signal)
            old.band = old.band or n.band
    return sorted(merged.values(), key=lambda n: n.signal, reverse=True)


def profiles() -> list[str]:
    rc, out = netsh("wlan", "show", "profiles")
    if rc != 0:
        return []
    names: list[str] = []
    pat = re.compile(r"^\s*(?:所有用户配置文件|All User Profile)\s*[:：]\s*(.+)$")
    for line in out.splitlines():
        m = pat.match(line)
        if m:
            names.append(m.group(1).strip())
    return names


def _clean(out: str, limit: int = 180) -> str:
    return " ".join((out or "").split())[:limit]


def _net_auth(ssid: str) -> str:
    """从扫描结果里取该 SSID 的认证方式（取不到就返回空串）。"""
    for n in scan():
        if n.ssid == ssid:
            return n.auth or ""
    return ""


def _profile_xml(ssid: str, password: str, open_network: bool, auth_kind: str = "") -> str:
    """生成 WLAN 配置文件。

    认证方式要跟热点实际广播的一致，否则会「关联但验证不过」：
    手机热点现在默认可能是 WPA3-Personal（SAE），而 WPA2PSK 的配置连不上纯 WPA3 的 AP。
    """
    name = escape(ssid)
    auth = auth_kind.lower()
    if open_network or not password:
        security = """
    <security>
      <authEncryption>
        <authentication>open</authentication>
        <encryption>none</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
    </security>"""
    elif "wpa3" in auth or "sae" in auth:
        security = f"""
    <security>
      <authEncryption>
        <authentication>WPA3SAE</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
        <transitionMode xmlns="http://www.microsoft.com/networking/WLAN/profile/v4">true</transitionMode>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>{escape(password)}</keyMaterial>
      </sharedKey>
    </security>"""
    else:
        security = f"""
    <security>
      <authEncryption>
        <authentication>WPA2PSK</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>{escape(password)}</keyMaterial>
      </sharedKey>
    </security>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{name}</name>
  <SSIDConfig>
    <SSID><name>{name}</name></SSID>
  </SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM>{security}
  </MSM>
</WLANProfile>
"""


def _add_profile_file(path: "Path") -> tuple[bool, str]:
    rc, out = netsh("wlan", "add", "profile", f"filename={path}", "user=current")
    return rc == 0, _clean(out)


def _export_profile(ssid: str) -> "Path | None":
    """把已有配置导出成 XML（含明文密钥），用于保留原有认证方式后再改密码。"""
    import tempfile
    from pathlib import Path

    out_dir = Path(tempfile.gettempdir()) / "netswitcher_profiles"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.xml"):
        old.unlink(missing_ok=True)
    rc, _ = netsh("wlan", "export", "profile", f"name={ssid}", f"folder={out_dir}", "key=clear")
    if rc != 0:
        return None
    files = sorted(out_dir.glob("*.xml"))
    return files[0] if files else None


def sync_password(ssid: str, password: str) -> tuple[bool, str]:
    """把已有配置里的密钥改成给定密码，保留 WPA3 / 过渡模式等原始设置。

    `netsh wlan add profile` 对同名配置是**覆盖**（实测 rc=0，不报冲突），
    所以这条路既可以改密码，也不会多出一份重复配置。
    """
    if not password:
        return False, "没有密码"
    path = _export_profile(ssid)
    if path is None:
        return False, "导出已有配置失败"

    text = path.read_text(encoding="utf-8", errors="replace")
    old = re.search(r"<keyMaterial>.*?</keyMaterial>", text, re.S)
    if not old:
        return False, "配置里没有密钥字段"
    new = f"<keyMaterial>{escape(password)}</keyMaterial>"
    if old.group(0) == new:
        return True, "密码未变"
    try:
        path.write_text(text[: old.start()] + new + text[old.end():], encoding="utf-8")
        ok, detail = _add_profile_file(path)
    finally:
        path.unlink(missing_ok=True)
    return ok, "已更新密码" if ok else f"更新密码失败：{detail}"


def ensure_profile(
    ssid: str, password: str = "", open_network: bool = False, sync: bool = False
) -> tuple[bool, str]:
    """确保系统里有该 SSID 的可用配置。

    sync=True 才去覆盖已有配置的密码（仅当用户这次**亲手输入**了密码）。
    从配置库里读出来的密码不覆盖：库里存错了而系统配置是对的，那种情况下
    覆盖会把本来能连的网络弄坏 —— 保住能用的，比「自愈」重要。
    """
    exists = ssid in profiles()
    if exists:
        if sync and password:
            ok, msg = sync_password(ssid, password)
            if ok:
                return True, msg
            # 同步失败不算致命：旧配置可能就是对的，继续用
            return True, "沿用已有配置"
        return True, "配置文件已存在"
    if not password and not open_network:
        return False, "系统里没有该网络的配置，且未提供密码"

    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / "netswitcher_profile.xml"
    path.write_text(
        _profile_xml(ssid, password, open_network, _net_auth(ssid)), encoding="utf-8"
    )
    try:
        ok, detail = _add_profile_file(path)
    finally:
        path.unlink(missing_ok=True)
    if not ok:
        return False, detail or "添加配置文件失败"
    return True, "已保存配置"


def delete_profile(ssid: str) -> tuple[bool, str]:
    rc, out = netsh("wlan", "delete", "profile", f"name={ssid}")
    return rc == 0, _clean(out)


def connect(ssid: str, timeout: float = 18.0, interface_name: str | None = None) -> tuple[bool, str]:
    """连接指定 SSID，并以 interfaces 的实测状态判定成功。

    不能只看 netsh 的返回码：它经常报告「已成功完成连接请求」而实际没连上；
    反过来也会因为参数写法不对而直接报错 —— 所以两种都要判，并且把报错原因带出去。
    """
    iface = interface_name or interface().name
    args = ["wlan", "connect", f"name={ssid}", f"ssid={ssid}"]
    if iface:
        args.append(f"interface={iface}")
    rc, out = netsh(*args)
    if rc != 0:
        return False, f"系统拒绝了连接请求：{_clean(out) or '参数或权限不对'}"

    deadline = time.monotonic() + timeout
    states: list[str] = []
    while time.monotonic() < deadline:
        cur = interface()
        if cur.connected and cur.ssid == ssid:
            return True, f"已连接 {ssid}"
        if cur.state and (not states or states[-1] != cur.state):
            states.append(cur.state)
        time.sleep(0.5)
    return False, _timeout_reason(ssid, states)


def _timeout_reason(ssid: str, states: list[str]) -> str:
    """把超时翻译成能指导操作的一句话。"""
    tail = "、".join(dict.fromkeys(states)) or "一直未发起关联"
    tried = any(h in " ".join(states) for h in _STATE_TRYING)
    if tried:
        return (
            f"密码或认证方式不对（过程：{tail}）"
            "：确认热点密码；若手机热点是 WPA3，需重建配置"
        )
    visible = [n for n in scan() if n.ssid == ssid]
    if not visible:
        return f"当前扫不到「{ssid}」（{tail}）：手机热点是否已开启、是否在范围内"
    return f"系统没有发起关联（{tail}）：网卡可能被其他程序占用，或飞行模式 / 硬件开关被打开"


def disconnect(timeout: float = 8.0) -> tuple[bool, str]:
    iface = interface()
    if not iface.connected:
        return True, "无线本就未连接"
    args = ["wlan", "disconnect"]
    if iface.name:
        args.append(f"interface={iface.name}")
    netsh(*args)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not interface().connected:
            return True, "已断开无线"
        time.sleep(0.4)
    return False, "断开无线超时"

