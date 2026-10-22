"""本地 HTTP 服务：给 Electron 前端用。

只监听 127.0.0.1，端口随机，所有 /api 请求必须带本次启动生成的 token。
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..config import ConfigStore
from ..engine.automode import AutoEngine
from ..engine.switcher import BusyError, Switcher
from ..journal import Journal
from ..logbuf import Logger
from ..win import adapters as ad
from ..win import health, procs, proxy, wifi
from ..win.admin import is_admin


class App:
    """把内核各部件组装起来，供 HTTP 层调用。"""

    def __init__(self, store: ConfigStore, log: Logger, journal: Journal, static_dir: Path) -> None:
        self.store = store
        self.log = log
        self.journal = journal
        self.static_dir = static_dir
        self.switcher = Switcher(store, journal, log)
        self.auto = AutoEngine(store, self.switcher, log)
        self._scan_cache: list[dict] = []
        self._scan_at = 0.0
        self._snap = None
        self._snap_at = 0.0
        self._snap_lock = threading.Lock()
        self._warn_at: dict[str, float] = {}
        # 界面存活信号：页面每 1.5 秒轮询一次，据此判断窗口还在不在
        self.last_hit = 0.0
        self.client_gone = 0.0

    def touch(self) -> None:
        """任何一次请求都算「窗口还活着」。"""
        now = time.monotonic()
        self.last_hit = now
        if self.client_gone and now - self.client_gone > 2:
            self.client_gone = 0.0  # 关页面又马上刷新，取消退出

    def mark_client_gone(self) -> None:
        """页面卸载（关窗口/刷新）时由前端 beacon 触发。"""
        self.client_gone = time.monotonic()
        self.log.info("app", "界面已关闭，准备退出")

    def start(self) -> None:
        self.auto.start()
        procs.start_watcher()
        threading.Thread(target=self._online_loop, daemon=True, name="online-probe").start()
        threading.Thread(target=self._net_loop, daemon=True, name="net-poll").start()

    def stop(self) -> None:
        self.auto.stop()

    def _online_loop(self) -> None:
        while True:
            try:
                self._last_online = health.internet()
            except Exception:
                self._last_online = (False, "探活异常")
            time.sleep(5)

    def _net_loop(self) -> None:
        """在后台固定刷新网卡快照，让 /api/state 永远不用等 PowerShell。"""
        while True:
            try:
                self._refresh_net()
            except Exception as exc:  # noqa: BLE001
                self._warn("net", exc)
            time.sleep(1.5)

    def _refresh_net(self) -> None:
        items = ad.list_adapters()
        current = wifi.interface()
        with self._snap_lock:
            self._snap = (items, current)
            self._snap_at = time.monotonic()

    # ---------------- 状态 ----------------
    def _net_snapshot(self):
        with self._snap_lock:
            snap = self._snap
        if snap is not None:
            return snap
        self._refresh_net()
        with self._snap_lock:
            return self._snap or ([], wifi.WifiInterface())

    def state(self) -> dict:
        items, cur_wifi = self._net_snapshot()
        egress = ad.current_egress(items)
        online, via = self._last_online
        payload: dict[str, Any] = {
            "version": __version__,
            "admin": is_admin(),
            "online": online,
            "online_via": via,
            "adapters": [a.to_dict() for a in items if not a.virtual],
            "egress": egress.to_dict() if egress else None,
            "wifi": cur_wifi.to_dict(),
            "snapshot_age": round(time.monotonic() - self._snap_at, 1),
        }
        # 每个子系统单独兜底：一块坏了不能把整个状态接口带崩
        for key, fn, default in (
            ("proxy", proxy.read, {}),
            ("auto", self.auto.snapshot, {}),
            ("job", self.switcher.job.to_dict, {}),
            ("profiles", self.store.profiles, []),
            ("processes", lambda: procs.match(self.store.config()["auto"]["process_patterns"])[:5], []),
        ):
            try:
                payload[key] = fn()
            except Exception as exc:  # noqa: BLE001
                payload[key] = default
                self._warn(key, exc)
        try:
            job = payload.get("job") or {}
            payload["pending_journal"] = bool(self.journal.pending()) and not job.get("running")
        except Exception as exc:  # noqa: BLE001
            payload["pending_journal"] = False
            self._warn("journal", exc)
        return payload

    def _warn(self, key: str, exc: Exception) -> None:
        """同一个字段的错误 30 秒内只记一次，别让轮询把日志刷满。"""
        now = time.monotonic()
        with self._snap_lock:
            if now - self._warn_at.get(key, 0.0) < 30:
                return
            self._warn_at[key] = now
        self.log.error("api", f"状态字段 {key} 读取失败：{exc}")

    _last_online: tuple[bool, str] = (False, "")

    def scan(self, force: bool = False) -> list[dict]:
        import time

        if not force and time.monotonic() - self._scan_at < 8:
            return self._scan_cache
        nets = [n.to_dict() for n in wifi.scan()]
        saved = {p["ssid"] for p in self.store.profiles()}
        have = set(wifi.profiles())
        for net in nets:
            net["saved"] = net["ssid"] in saved
            net["profiled"] = net["ssid"] in have
        self._scan_cache = nets
        self._scan_at = time.monotonic()
        return nets

    def switch(self, payload: dict) -> dict:
        kind = payload.get("kind", "")
        if kind not in ("ethernet", "wifi", "hotspot"):
            raise ValueError("kind 必须是 ethernet / wifi / hotspot")
        try:
            return self.switcher.switch(
                kind,
                payload.get("ssid", "") or "",
                payload.get("password", "") or "",
                source="manual",
            )
        except BusyError as exc:
            return {"ok": False, "message": str(exc), "job": self.switcher.job.to_dict()}


class QuietServer(ThreadingHTTPServer):
    """浏览器刷新、页面关闭都会直接掐断连接，这在 HTTP 服务里是常态。

    默认实现会把整段 traceback 打到控制台，看起来像崩了，其实只是对端先走了。
    这里把这类连接异常吞掉，真正的服务端错误才记录。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, log: Logger | None = None) -> None:
        self.log = log
        super().__init__(address, handler)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        if self.log:
            self.log.error("api", f"请求处理异常：{exc!r}\n{traceback.format_exc(limit=6)}")


def make_handler(app: App, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"NetSwitcher/{__version__}"
        protocol_version = "HTTP/1.1"

        # ---------------- 基础 ----------------
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        def log_error(self, fmt: str, *args: Any) -> None:
            msg = fmt % args
            if any(k in msg for k in ("Broken pipe", "Connection reset", "Connection aborted", "10053", "10054")):
                return
            app.log.warn("api", msg)

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
                self.close_connection = True

        def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def _authed(self, query: dict) -> bool:
            if self.path.startswith("/api/"):
                supplied = self.headers.get("X-Token") or (query.get("token", [""])[0])
                return supplied == token
            return True

        # ---------------- 路由 ----------------
        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(200, b"{}")

        def do_HEAD(self) -> None:  # noqa: N802
            # 浏览器/预览面板偶尔先发 HEAD 探活，不实现会报 501
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path
            if not self._authed(query):
                return self._json({"error": "unauthorized"}, 401)

            app.touch()
            if path == "/api/bye":
                app.mark_client_gone()
                return self._json({"ok": True})
            if path.startswith("/api/"):
                return self._api_get(path, query)
            return self._static(path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authed(parse_qs(parsed.query)):
                return self._json({"error": "unauthorized"}, 401)
            app.touch()
            return self._api_post(parsed.path, self._body())

        def do_PATCH(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authed(parse_qs(parsed.query)):
                return self._json({"error": "unauthorized"}, 401)
            app.touch()
            return self._api_post(parsed.path, self._body(), method="PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authed(parse_qs(parsed.query)):
                return self._json({"error": "unauthorized"}, 401)
            app.touch()
            m = re.fullmatch(r"/api/profiles/([\w-]+)", parsed.path)
            if m:
                ok = app.store.delete_profile(m.group(1))
                return self._json({"ok": ok})
            self._json({"error": "not found"}, 404)

        # ---------------- API ----------------
        def _api_get(self, path: str, query: dict) -> None:
            if path == "/api/state":
                return self._json(app.state())
            if path == "/api/scan":
                return self._json({"networks": app.scan(force=query.get("force", ["0"])[0] == "1")})
            if path == "/api/profiles":
                return self._json({"profiles": app.store.profiles()})
            if path == "/api/config":
                return self._json(app.store.config())
            if path == "/api/logs":
                since = int(query.get("since", ["0"])[0] or 0)
                limit = int(query.get("limit", ["120"])[0] or 120)
                return self._json({"logs": app.log.tail(limit, since)})
            if path == "/api/processes":
                return self._json({"processes": procs.all_names()})
            if path == "/api/ping":
                return self._json({"ok": True, "admin": is_admin()})
            self._json({"error": "not found"}, 404)

        def _api_post(self, path: str, body: dict, method: str = "POST") -> None:
            try:
                if path == "/api/switch":
                    return self._json(app.switch(body))
                if path == "/api/profiles":
                    item = app.store.upsert_profile(body)
                    # 改了密码就顺手写进系统的 WiFi 配置，不然连接时用的还是旧密钥
                    synced = None
                    pwd = str(body.get("password") or "").strip()
                    if pwd and item.get("ssid"):
                        if item["ssid"] in wifi.profiles():
                            ok, msg = wifi.sync_password(item["ssid"], pwd)
                            synced = {"ok": ok, "message": msg}
                    return self._json({"ok": True, "profile": item, "system": synced})
                if path == "/api/auto":
                    return self._json(self._set_auto(body))
                if path == "/api/config":
                    return self._json({"ok": True, "config": app.store.patch_config(body)})
                if path == "/api/logs/clear":
                    app.log.clear()
                    return self._json({"ok": True})
                if path == "/api/journal/clear":
                    app.journal.clear()
                    return self._json({"ok": True})
                if path == "/api/cooldown/reset":
                    app.auto.reset_cooldown()
                    return self._json({"ok": True})
                if path == "/api/auto/takeover":
                    app.auto.take_over_now()
                    return self._json({"ok": True})
                if path == "/api/restore":
                    res = app.switcher.switch(
                        body.get("kind", "ethernet"),
                        body.get("ssid", ""),
                        body.get("password", ""),
                        source="manual",
                    )
                    return self._json(res)
                if path == "/api/scan":
                    return self._json({"networks": app.scan(force=True)})
            except Exception as exc:  # noqa: BLE001
                app.log.error("api", f"{path} 失败：{exc}")
                return self._json({"ok": False, "message": str(exc)}, 500)
            self._json({"error": "not found"}, 404)

        def _set_auto(self, body: dict) -> dict:
            patch: dict[str, Any] = {}
            for key in ("enabled", "mode", "debounce_on", "debounce_off", "process_patterns", "verify_online"):
                if key in body:
                    patch[key] = body[key]
            if "target_id" in body:
                app.store.set_auto_target(body["target_id"])
            if "target_ssid" in body:
                patch["target_ssid"] = body["target_ssid"]
            if patch:
                app.store.patch_config({"auto": patch})
            return {"ok": True, "auto": app.auto.snapshot()}

        # ---------------- 静态 ----------------
        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            root = app.static_dir.resolve()
            target = (root / rel).resolve()
            if not str(target).startswith(str(root)) or not target.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

    return Handler


def serve(app: App, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(18)
    httpd = QuietServer(("127.0.0.1", port), make_handler(app, token), log=app.log)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="http")
    thread.start()
    return httpd, token
