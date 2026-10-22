"""日志：内存环形缓冲（给 UI）+ 按天落盘。"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path


class Logger:
    def __init__(self, log_dir: Path, keep: int = 400) -> None:
        self.dir = log_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._buf: deque[dict] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._seq = 0

    def _write(self, level: str, module: str, message: str) -> dict:
        with self._lock:
            self._seq += 1
            now = datetime.now()
            record = {
                "seq": self._seq,
                "ts": now.strftime("%H:%M:%S"),
                "date": now.strftime("%Y-%m-%d"),
                "level": level,
                "module": module,
                "message": message,
            }
            self._buf.append(record)
        try:
            line = f"{now:%Y-%m-%d %H:%M:%S} [{level:<5}] {module:<8} {message}\n"
            path = self.dir / f"app-{now:%Y%m%d}.log"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass
        return record

    def info(self, module: str, message: str) -> None:
        self._write("INFO", module, message)

    def warn(self, module: str, message: str) -> None:
        self._write("WARN", module, message)

    def error(self, module: str, message: str) -> None:
        self._write("ERROR", module, message)

    def debug(self, module: str, message: str) -> None:
        self._write("DEBUG", module, message)

    def tail(self, limit: int = 120, since: int = 0) -> list[dict]:
        with self._lock:
            items = list(self._buf)
        if since:
            items = [r for r in items if r["seq"] > since]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
