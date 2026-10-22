"""切换事务：快照 → 执行 → 实测校验 → 成功或回滚。

设计要点：
1. 一律硬断。只改路由 metric 对已建立的代理长连接无效，等于没切。
2. 结果以实测为准：拿到 IP + 网关可达 + 外网 TCP 探活，命令返回码不采信。
3. 全程互斥，同一时刻只允许一个事务。
4. 失败自动回滚；回滚也失败时进入 ERROR 状态，由 UI 强提醒。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import ConfigStore
from ..journal import Journal
from ..logbuf import Logger
from ..win import adapters as ad
from ..win import health, wifi
from ..win.admin import is_admin

KIND_LABEL = {"ethernet": "有线", "wifi": "WiFi", "hotspot": "手机热点"}


@dataclass
class Step:
    key: str
    label: str
    state: str = "pending"
    detail: str = ""


@dataclass
class Job:
    id: int = 0
    running: bool = False
    kind: str = ""
    ssid: str = ""
    source: str = "manual"
    ok: bool | None = None
    message: str = ""
    started: float = 0.0
    elapsed: float = 0.0
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "running": self.running,
            "kind": self.kind,
            "ssid": self.ssid,
            "source": self.source,
            "ok": self.ok,
            "message": self.message,
            "elapsed": round(self.elapsed, 1),
            "steps": [asdict(s) for s in self.steps],
        }


class BusyError(RuntimeError):
    pass


class Switcher:
    def __init__(self, store: ConfigStore, journal: Journal, log: Logger) -> None:
        self.store = store
        self.journal = journal
        self.log = log
        self._lock = threading.Lock()
        self._seq = 0
        self.job = Job()
        self.manual_until = 0.0
        self.manual_trigger_active = False
        self.trigger_active = False

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> dict:
        items = ad.list_adapters()
        egress = ad.current_egress(items)
        cur_wifi = wifi.interface()
        return {
            "egress_kind": egress.kind if egress else "none",
            "egress_adapter": egress.name if egress else "",
            "wifi_ssid": cur_wifi.ssid if cur_wifi.connected else "",
            "adapters": [{"name": a.name, "up": a.is_up, "kind": a.kind} for a in items if not a.virtual],
        }

    # ------------------------------------------------------------------ 主流程
    def switch(self, kind: str, ssid: str = "", password: str = "", source: str = "manual") -> dict:
        if not self._lock.acquire(blocking=False):
            raise BusyError("已有切换正在进行")
        self._seq += 1
        job = Job(id=self._seq, running=True, kind=kind, ssid=ssid, source=source, started=time.monotonic())
        job.steps = self._plan(kind)
        self.job = job
        try:
            result = self._execute(job, kind, ssid, password)
            return result
        finally:
            job.running = False
            job.elapsed = time.monotonic() - job.started
            if source == "manual":
                grace = float(self.store.config()["auto"]["manual_grace_seconds"])
                self.manual_until = time.monotonic() + grace
                # 记下「按下的那一刻触发是不是已经命中」，自动模式据此判断该不该让路
                self.manual_trigger_active = bool(self.trigger_active)
            self._lock.release()

    def _plan(self, kind: str) -> list[Step]:
        if kind == "ethernet":
            return [
                Step("enable", "启用有线网卡"),
                Step("detach", "断开无线链路"),
                Step("ip", "获取地址"),
                Step("online", "外网校验"),
            ]
        return [
            Step("detach", "断开原链路"),
            Step("enable", "启用无线网卡"),
            Step("assoc", "连接网络"),
            Step("ip", "获取地址"),
            Step("online", "外网校验"),
        ]

    def _set(self, job: Job, key: str, state: str, detail: str = "") -> None:
        for step in job.steps:
            if step.key == key:
                step.state = state
                if detail:
                    step.detail = detail
        if detail:
            self.log.info("switch", f"{key}: {detail}")

    def _execute(self, job: Job, kind: str, ssid: str, password: str) -> dict:
        if not is_admin():
            self.log.error("switch", "缺少管理员权限，拒绝执行")
            return self._finish(job, False, "需要管理员权限才能启停网卡")

        snap = self.snapshot()
        target = {"kind": kind, "ssid": ssid}
        self.journal.start(snap, target)
        self.log.info("switch", f"开始切换 → {KIND_LABEL.get(kind, kind)} {ssid}（{job.source}）")

        try:
            if kind == "ethernet":
                self._to_ethernet(job)
            else:
                self._to_wireless(job, ssid, password)
        except SwitchFailure as exc:
            step_label = next((s.label for s in job.steps if s.key == exc.step), "切换")
            self._set(job, exc.step, "fail", str(exc))
            self.log.error("switch", f"{step_label} 失败：{exc}")
            rollback_ok = self._rollback(job, snap)
            self.journal.fail(str(exc))
            if rollback_ok:
                return self._finish(job, False, f"{step_label}失败，已回滚到原网络")
            self.log.error("switch", "回滚失败，需要人工介入")
            return self._finish(job, False, f"{step_label}失败，且回滚失败，请检查网络")

        self.journal.done(self.snapshot())
        self._remember(kind, ssid, password)
        return self._finish(job, True, f"已切换到{KIND_LABEL.get(kind, kind)}" + (f"「{ssid}」" if ssid else ""))

    def _remember(self, kind: str, ssid: str, password: str) -> None:
        """连上过的网络自动收进网络库，下次不用再回头找密码。"""
        if kind not in ("wifi", "hotspot") or not ssid:
            return
        try:
            if any(p.get("ssid") == ssid for p in self.store.profiles()):
                return
            self.store.upsert_profile(
                {"ssid": ssid, "type": kind, "password": password, "note": "自动收录"}
            )
            self.log.info("switch", f"已把「{ssid}」加入网络库")
        except Exception as exc:  # noqa: BLE001
            self.log.warn("switch", f"加入网络库失败：{exc}")

    def _finish(self, job: Job, ok: bool, message: str) -> dict:
        job.ok = ok
        job.message = message
        job.elapsed = time.monotonic() - job.started
        job.running = False
        self.log.info("switch", message)
        return {"ok": ok, "message": message, "job": job.to_dict()}

    # ------------------------------------------------------------------ 目标：有线
    def _to_ethernet(self, job: Job) -> None:
        items = ad.list_adapters()
        wired = ad.find_ethernets(items)
        if not wired:
            raise SwitchFailure("未检测到有线网卡", "enable")

        self._set(job, "enable", "run")
        for nic in wired:
            if not nic.is_up:
                ok, detail = ad.set_enabled(nic, True)
                if not ok:
                    raise SwitchFailure(detail, "enable")
        if not any(ad.wait_status(nic.name, True, 12) for nic in wired):
            raise SwitchFailure("有线网卡未能启用（可能未插网线）", "enable")
        self._set(job, "enable", "ok", "有线网卡已启用")

        self._set(job, "detach", "run")
        cur = wifi.interface()
        if cur.connected:
            ok, detail = wifi.disconnect()
            if not ok:
                raise SwitchFailure(detail, "detach")
        self._set(job, "detach", "ok", "无线已断开")

        self._set(job, "ip", "run")
        ip = None
        for nic in wired:
            ip = ad.wait_ip(nic.name, 18)
            if ip:
                break
        if not ip:
            raise SwitchFailure("有线网卡未获取到 IPv4 地址", "ip")
        self._set(job, "ip", "ok", f"获得地址 {ip}")

        self._verify_online(job)

    # ------------------------------------------------------------------ 目标：无线
    def _to_wireless(self, job: Job, ssid: str, password: str) -> None:
        if not ssid:
            raise SwitchFailure("未指定要连接的网络", "assoc")

        items = ad.list_adapters()
        wlan = ad.find_wifi(items)
        if not wlan:
            raise SwitchFailure("未检测到无线网卡", "enable")

        # 先做无破坏性的预检查：配置都备不齐，就别把有线断了 —— 免得白断一次网再回滚
        typed = bool(password)  # 用户这次亲手敲的密码才允许覆盖系统里已有的配置
        if not password:
            password = self.store.password_for(ssid)
        current = wifi.interface()
        already = bool(current.connected and current.ssid == ssid)
        if not already:
            ok, detail = wifi.ensure_profile(
                ssid, password, open_network=not password, sync=typed
            )
            if not ok:
                raise SwitchFailure(detail, "assoc")

        self._set(job, "detach", "run")
        for nic in ad.find_ethernets(items):
            if nic.is_up:
                ok, detail = ad.set_enabled(nic, False)
                if not ok:
                    raise SwitchFailure(detail, "detach")
        self._set(job, "detach", "ok", "有线已断开（硬断，保证出口干净）")

        self._set(job, "enable", "run")
        # 注意：无线网卡的 Status 有 Up / Disconnected / Disabled 三种。
        # 只要不是 Disabled 就算「已启用」，此时它还没连上任何 AP，等 Up 是等不到的。
        if wlan.is_enabled:
            self._set(job, "enable", "ok", "无线网卡本就启用")
        else:
            ok, detail = ad.set_enabled(wlan, True)
            if not ok:
                raise SwitchFailure(detail, "enable")
            if not ad.wait_enabled(wlan.name, 12):
                raise SwitchFailure("无线网卡未能启用（检查飞行模式、硬件开关或驱动）", "enable")
            self._set(job, "enable", "ok", "无线网卡已启用")

        self._set(job, "assoc", "run")
        if already:
            self._set(job, "assoc", "ok", f"本来就连着 {ssid}")
        else:
            ok, detail = wifi.connect(
                ssid, timeout=float(self.store.config()["switch"]["connect_timeout"])
            )
            if not ok:
                raise SwitchFailure(detail, "assoc")
            self._set(job, "assoc", "ok", f"已连接 {ssid}")

        self._set(job, "ip", "run")
        ip = ad.wait_ip(wlan.name, 18)
        if not ip:
            raise SwitchFailure("未获取到 IPv4 地址（可能需要网页认证）", "ip")
        self._set(job, "ip", "ok", f"获得地址 {ip}")

        self._verify_online(job)

    def _verify_online(self, job: Job) -> None:
        self._set(job, "online", "run")
        cfg = self.store.config()["switch"]
        if not self.store.config()["auto"]["verify_online"]:
            self._set(job, "online", "ok", "已跳过（用户关闭校验）")
            return
        ok, detail = health.wait_online(float(cfg["online_timeout"]))
        if not ok:
            raise SwitchFailure(f"外网不可达（{detail}），可能需要网页认证", "online")
        self._set(job, "online", "ok", f"外网可达（{detail}）")

    # ------------------------------------------------------------------ 回滚
    def _rollback(self, job: Job, snap: dict) -> bool:
        self.log.warn("switch", "开始回滚")
        key = job.steps[0].key
        job.steps.append(Step("rollback", "回滚到原网络", "run"))
        try:
            for item in snap.get("adapters", []):
                if not item["up"]:
                    continue
                for nic in ad.list_adapters():
                    if nic.name == item["name"] and not nic.is_up:
                        ad.set_enabled(nic, True)
                        ad.wait_status(nic.name, True, 12)

            want = snap.get("wifi_ssid")
            if want:
                wifi.ensure_profile(want, self.store.password_for(want), open_network=not self.store.password_for(want))
                wifi.connect(want, timeout=15)

            ok, _ = health.wait_online(15)
            job.steps[-1].state = "ok" if ok else "fail"
            job.steps[-1].detail = "已还原" if ok else "原网络也未能恢复"
            if ok:
                self.journal.done(self.snapshot())
            return ok
        except Exception as exc:  # noqa: BLE001
            self.log.error("switch", f"回滚异常：{exc}")
            job.steps[-1].state = "fail"
            job.steps[-1].detail = str(exc)
            return False


class SwitchFailure(Exception):
    def __init__(self, message: str, step: str = "") -> None:
        super().__init__(message)
        self.step = step
