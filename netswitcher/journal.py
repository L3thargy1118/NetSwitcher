"""切换事务账本。

每次切换前落盘快照，只有校验通过才标 done。程序崩溃或被杀后，
下一次启动能发现 phase != done 的残留事务并提示还原。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def pending(self) -> dict | None:
        data = self.read()
        if data and data.get("phase") in ("switching", "reverting"):
            return data
        return None

    def start(self, before: dict, target: dict, phase: str = "switching") -> None:
        self._write({"phase": phase, "before": before, "target": target})

    def done(self, after: dict | None = None) -> None:
        self._write({"phase": "done", "after": after or {}})

    def fail(self, reason: str) -> None:
        data = self.read() or {}
        data["phase"] = "failed"
        data["reason"] = reason
        self._save(data)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def _write(self, payload: dict) -> None:
        payload["ts"] = datetime.now().isoformat(timespec="seconds")
        self._save(payload)

    def _save(self, payload: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
