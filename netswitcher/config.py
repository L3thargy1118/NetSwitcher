"""配置与网络库的读写。

配置文件都在 %APPDATA%\\NetSwitcher\\ 下：
    config.json    行为参数
    profiles.json  网络库（密码为 DPAPI 密文）
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from .win import dpapi


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "NetSwitcher"
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "app": {"autostart": False, "start_minimized": False, "close_to_tray": True},
    "auto": {
        "enabled": False,
        "mode": "system_proxy",
        "target_ssid": "",
        "debounce_on": 1.0,
        "debounce_off": 10.0,
        "max_retry": 3,
        "cooldown_seconds": 300,
        "manual_grace_seconds": 600,
        "verify_online": True,
        "process_patterns": [
            "clash*.exe",
            "verge-mihomo*.exe",
            "v2ray*.exe",
            "xray*.exe",
            "sing-box*.exe",
            "shadowsocks*.exe",
            "netch*.exe",
            "mihomo*.exe",
            "ziyoumao*.exe",
            "hiddify*.exe",
            "nekoray*.exe",
            "karing*.exe",
        ],
        "patterns_version": 2,
    },
    "switch": {"connect_timeout": 18, "online_timeout": 20, "strategy": "hard"},
    "ui": {"theme": "dark", "accent": "yellow", "lang": "zh"},
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigStore:
    def __init__(self) -> None:
        self.dir = app_dir()
        self.path = self.dir / "config.json"
        self.profiles_path = self.dir / "profiles.json"
        self._lock = threading.RLock()
        self._config = self._load_config()
        self._profiles = self._load_profiles()
        if self._migrated_patterns:
            self._save_config()

    # ---------- config ----------
    def _load_config(self) -> dict:
        self._migrated_patterns = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            merged = _deep_merge(DEFAULT_CONFIG, raw)
        except Exception:
            return copy.deepcopy(DEFAULT_CONFIG)

        # 老版本存下来的名单里没有新加的客户端，补一次；用户自己加的条目原样保留
        # 注意：要读 raw 里的版本号。merged 会从默认值继承成最新版，拿它比就永远不迁移。
        auto = merged.setdefault("auto", {})
        stored_version = int((raw.get("auto") or {}).get("patterns_version") or 0)
        if stored_version < int(DEFAULT_CONFIG["auto"]["patterns_version"]):
            current = [str(x).lower() for x in (auto.get("process_patterns") or [])]
            for pattern in DEFAULT_CONFIG["auto"]["process_patterns"]:
                if pattern.lower() not in current:
                    current.append(pattern)
            auto["process_patterns"] = current
            auto["patterns_version"] = DEFAULT_CONFIG["auto"]["patterns_version"]
            self._migrated_patterns = True
        return merged

    def _save_config(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._config, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def config(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._config)

    def patch_config(self, patch: dict) -> dict:
        with self._lock:
            self._config = _deep_merge(self._config, patch or {})
            self._save_config()
            return copy.deepcopy(self._config)

    # ---------- profiles ----------
    def _load_profiles(self) -> list[dict]:
        try:
            raw = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            return list(raw.get("profiles") or [])
        except Exception:
            return []

    def _save_profiles(self) -> None:
        tmp = self.profiles_path.with_suffix(".tmp")
        payload = {"version": 1, "profiles": self._profiles}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.profiles_path)

    def profiles(self) -> list[dict]:
        with self._lock:
            items = copy.deepcopy(self._profiles)
        # 密码永远不出内核
        for item in items:
            item.pop("password_enc", None)
            item["has_password"] = bool(self._password_of(item["id"]))
        items.sort(key=lambda p: (-int(p.get("priority") or 0), p.get("ssid", "")))
        return items

    def _password_of(self, profile_id: str) -> str:
        for item in self._profiles:
            if item.get("id") == profile_id:
                return dpapi.decrypt_text(item.get("password_enc", ""))
        return ""

    def find(self, ssid: str) -> dict | None:
        with self._lock:
            for item in self._profiles:
                if item.get("ssid") == ssid:
                    return copy.deepcopy(item)
        return None

    def upsert_profile(self, data: dict) -> dict:
        ssid = (data.get("ssid") or "").strip()
        if not ssid:
            raise ValueError("SSID 不能为空")
        kind = data.get("type") if data.get("type") in ("wifi", "hotspot") else "wifi"
        with self._lock:
            target = None
            for item in self._profiles:
                if item.get("ssid") == ssid:
                    target = item
                    break
            if target is None:
                target = {
                    "id": f"p_{uuid.uuid4().hex[:6]}",
                    "ssid": ssid,
                    "type": kind,
                    "password_enc": "",
                    "note": "",
                    "priority": 0,
                    "auto_connect": True,
                    "is_auto_target": False,
                }
                self._profiles.append(target)
            target["type"] = kind
            target["note"] = data.get("note", target.get("note", ""))
            target["priority"] = int(data.get("priority") or target.get("priority") or 0)
            target["auto_connect"] = bool(data.get("auto_connect", target.get("auto_connect", True)))
            if data.get("password"):
                target["password_enc"] = dpapi.encrypt_text(str(data["password"]))
            if data.get("is_auto_target"):
                for item in self._profiles:
                    item["is_auto_target"] = item is target
                target["is_auto_target"] = True
            self._save_profiles()
            safe = copy.deepcopy(target)
            safe.pop("password_enc", None)
            safe["has_password"] = bool(target.get("password_enc"))
            return safe

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            before = len(self._profiles)
            self._profiles = [p for p in self._profiles if p.get("id") != profile_id]
            changed = len(self._profiles) != before
            if changed:
                self._save_profiles()
            return changed

    def set_auto_target(self, profile_id: str) -> None:
        with self._lock:
            for item in self._profiles:
                item["is_auto_target"] = item.get("id") == profile_id
            self._save_profiles()

    def password_for(self, ssid: str) -> str:
        item = self.find(ssid)
        if not item:
            return ""
        return self._password_of(item["id"])

    def auto_target(self) -> dict | None:
        with self._lock:
            for item in self._profiles:
                if item.get("is_auto_target"):
                    return copy.deepcopy(item)
            for item in self._profiles:
                if item.get("type") == "hotspot":
                    return copy.deepcopy(item)
        return None
