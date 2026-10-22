"""自动模式状态机。

两种触发模式（互斥，用户可选）：
    system_proxy  系统代理被开启 → 切到热点；关闭 → 回切
    app_process   代理软件进程出现 → 切到热点；全部退出 → 回切

状态：idle / wait_on / switching / on_target / wait_off / reverting / cooldown / error
计时一律用单调时钟，系统时间跳变不影响防抖。
"""

from __future__ import annotations

import threading
import time

from ..config import ConfigStore
from ..logbuf import Logger
from ..win import procs, proxy
from .switcher import BusyError, Switcher

STATE_LABEL = {
    "idle": "空闲",
    "wait_on": "等待触发确认",
    "switching": "切换中",
    "on_target": "已切至热点",
    "wait_off": "等待回切确认",
    "reverting": "回切中",
    "cooldown": "冷却中",
    "error": "异常",
}


class AutoEngine(threading.Thread):
    def __init__(self, store: ConfigStore, switcher: Switcher, log: Logger) -> None:
        super().__init__(daemon=True, name="auto-engine")
        self.store = store
        self.switcher = switcher
        self.log = log

        self._stop = threading.Event()
        self._lock = threading.RLock()

        self.state = "idle"
        self.reason = ""
        self.triggers: list[str] = []
        self.fail_count = 0
        self.last_error = ""
        self.since = time.monotonic()
        self._stage_at = 0.0
        self._cooldown_until = 0.0
        self._no_target_at = 0.0
        self._before: dict | None = None
        self._last_active = False
        self._grace_from = 0.0
        self._grace_saw_active = False
        self._grace_logged = False

        self.proxy_watcher = proxy.Watcher()
        self.proxy_watcher.start()

    # ------------------------------------------------------------------ 状态
    def _goto(self, state: str, reason: str = "") -> None:
        if state != self.state:
            self.log.info("auto", f"状态 {self.state} → {state}" + (f"（{reason}）" if reason else ""))
            self.since = time.monotonic()
            self.state = state
        if reason:
            self.reason = reason

    def snapshot(self) -> dict:
        cfg = self.store.config()["auto"]
        now = time.monotonic()
        target = self.store.auto_target()
        return {
            "enabled": bool(cfg["enabled"]),
            "mode": cfg["mode"],
            "target": (target or {}).get("ssid", cfg.get("target_ssid", "")),
            "target_id": (target or {}).get("id", ""),
            "state": self.state,
            "state_label": STATE_LABEL.get(self.state, self.state),
            "reason": self.reason,
            "triggers": list(self.triggers),
            "fail_count": self.fail_count,
            "last_error": self.last_error,
            "since": round(now - self.since, 1),
            "cooldown_left": max(0, round(self._cooldown_until - now)),
            "debounce_on": cfg["debounce_on"],
            "debounce_off": cfg["debounce_off"],
            "manual_grace_left": max(0, round(self.switcher.manual_until - now)),
            "proxy": self.proxy_watcher.state,
        }

    # ------------------------------------------------------------------ 触发判定
    def _evaluate(self, cfg: dict) -> list[str]:
        hits: list[str] = []
        mode = cfg["mode"]
        if mode == "system_proxy":
            state = proxy.read()
            self.proxy_watcher._last = state  # 保持后端与实测同步
            if state["active"]:
                hits.append(f"系统代理已开启 {state['server'] or state['pac']}")
        else:
            matched = procs.match(cfg["process_patterns"])
            if matched:
                hits.append("检测到 " + "、".join(matched))
        self.triggers = hits
        return hits

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.log.error("auto", f"引擎异常：{exc}")
            self._stop.wait(0.2)

    def stop(self) -> None:
        self._stop.set()
        self.proxy_watcher.stop()

    def tick(self) -> None:
        with self._lock:
            cfg = self.store.config()["auto"]
            now = time.monotonic()

            if not cfg["enabled"]:
                if self.state != "idle":
                    self._goto("idle", "自动模式已关闭")
                self.triggers = []
                return

            if self.state in ("switching", "reverting"):
                return

            if self._cooldown_until and now < self._cooldown_until:
                if self.state != "cooldown":
                    self._goto("cooldown", "连续失败，暂停尝试")
                return
            if self._cooldown_until and now >= self._cooldown_until:
                self._cooldown_until = 0.0
                self.fail_count = 0
                self._goto("idle", "冷却结束")

            if self.switcher.job.running:
                return

            hits = self._evaluate(cfg)
            active = bool(hits)
            self._last_active = active
            self.switcher.trigger_active = active

            # 手动优先：手动切过一次后短时间内不抢回来（防止刚点完有线又被拽回热点）。
            # 但触发如果是在手动切换**之后**才出现的，用户意图就是让自动模式接手，
            # 这时候拦着反而像「自动模式坏了」——所以只拦「手动时就已经在触发」的情况。
            if self.switcher.manual_until != self._grace_from:
                self._grace_from = self.switcher.manual_until
                self._grace_logged = False
                self._grace_saw_active = bool(
                    getattr(self.switcher, "manual_trigger_active", self._last_active)
                )

            if self.state in ("idle", "wait_on") and now < self.switcher.manual_until:
                if active and not self._grace_saw_active:
                    self.switcher.manual_until = 0.0
                    self._grace_from = 0.0
                    self.log.info("auto", "触发是手动切换之后才出现的，取消手动优先")
                else:
                    if active:
                        self._grace_saw_active = True
                    left = int(self.switcher.manual_until - now)
                    if active and not self._grace_logged:
                        # 只在每次宽限期记一条，避免刷屏；不然用户会觉得「什么也没发生」
                        self._grace_logged = True
                        self.log.warn(
                            "auto",
                            f"手动切换后 {left}s 内不接管自动模式；要立刻切换请点「立即接管」",
                        )
                    self._goto("idle", f"手动切换后暂不接管（还剩 {left}s，可点「立即接管」）")
                    return

            if active:
                if self.state == "idle":
                    if not self.store.auto_target():
                        self._no_target()
                        return
                    self._stage_at = now
                    self._goto("wait_on", hits[0])
                elif self.state == "wait_on":
                    if now - self._stage_at >= float(cfg["debounce_on"]):
                        self._do_switch(cfg, hits[0])
                elif self.state == "wait_off":
                    self._goto("on_target", "触发恢复")
            else:
                if self.state == "wait_on":
                    self._goto("idle", "触发消失，取消")
                elif self.state == "on_target":
                    self._stage_at = now
                    self._goto("wait_off", "触发已消失")
                elif self.state == "wait_off":
                    if now - self._stage_at >= float(cfg["debounce_off"]):
                        self._do_revert(cfg)

    def _no_target(self) -> None:
        """没有目标热点时不要反复进出等待态，否则日志会被刷爆。"""
        now = time.monotonic()
        if now - self._no_target_at < 60:
            return
        self._no_target_at = now
        self.reason = "触发命中，但还没选目标热点"
        self.log.warn("auto", "触发命中，但没有配置目标热点（去网络库添加一个手机热点）")

    # ------------------------------------------------------------------ 动作
    def _do_switch(self, cfg: dict, reason: str) -> None:
        target = self.store.auto_target()
        if not target:
            self._goto("idle", "未设置目标热点")
            self.log.warn("auto", "触发命中，但没有配置目标热点")
            return

        ssid = target["ssid"]
        from ..win import wifi

        current = wifi.interface()
        if current.connected and current.ssid == ssid:
            self._goto("on_target", "已在目标热点上")
            return

        self._goto("switching", reason)
        self._before = self.switcher.snapshot()
        try:
            result = self.switcher.switch("hotspot", ssid, self.store.password_for(ssid), source="auto")
        except BusyError:
            self._goto("idle", "切换被占用")
            return

        if result["ok"]:
            self.fail_count = 0
            self._goto("on_target", f"已切换到 {ssid}")
        else:
            self.fail_count += 1
            self.last_error = result["message"]
            self.log.error("auto", f"切换失败（{self.fail_count}/{cfg['max_retry']}）：{result['message']}")
            if self.fail_count >= int(cfg["max_retry"]):
                self._cooldown_until = time.monotonic() + float(cfg["cooldown_seconds"])
                self._goto("cooldown", "连续失败，进入冷却")
            else:
                self._goto("idle", result["message"])

    def _do_revert(self, cfg: dict) -> None:
        before = self._before or self.switcher.journal.pending() or {}
        if not before:
            self._goto("idle", "无可还原的快照")
            return

        self._goto("reverting", "回切原网络")
        kind = before.get("egress_kind") or before.get("before", {}).get("egress_kind")
        ssid = before.get("wifi_ssid") or before.get("before", {}).get("wifi_ssid") or ""

        if kind not in ("ethernet", "wifi"):
            self._goto("idle", "原网络不可识别")
            return

        try:
            result = self.switcher.switch(
                kind,
                ssid if kind == "wifi" else "",
                self.store.password_for(ssid) if ssid else "",
                source="auto",
            )
        except BusyError:
            self._goto("on_target", "回切被占用")
            return

        if result["ok"]:
            self._before = None
            self._goto("idle", "已回切")
        else:
            self.last_error = result["message"]
            self._goto("error", "回切失败，请手动还原")
            self.log.error("auto", f"回切失败：{result['message']}")

    def reset_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0
            self.fail_count = 0
            self._goto("idle", "已手动解除冷却")

    def take_over_now(self) -> None:
        """用户点「立即接管」：放弃手动优先，让自动模式按当前触发立刻动作。"""
        with self._lock:
            self.switcher.manual_until = 0.0
            self._grace_from = 0.0
            self._grace_saw_active = False
            self.log.info("auto", "用户要求立即接管，取消手动优先")
            if self.state in ("idle", "wait_on"):
                self._goto("idle", "已取消手动优先")
