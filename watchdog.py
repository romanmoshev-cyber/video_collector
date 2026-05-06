from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class Heartbeat:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_ts = time.monotonic()
        self._last_meta: dict[str, Any] = {'status': 'boot'}

    def beat(self, **meta: Any) -> None:
        with self._lock:
            self._last_ts = time.monotonic()
            self._last_meta = {'ts': int(time.time()), **meta}
            self.path.write_text(json.dumps(self._last_meta, ensure_ascii=False, indent=2), encoding='utf-8')

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_ts

    @property
    def last_meta(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_meta)


class WatchdogKiller(threading.Thread):
    def __init__(self, heartbeat: Heartbeat, timeout_sec: int, check_interval_sec: int):
        super().__init__(name='watchdog-killer', daemon=True)
        self.heartbeat = heartbeat
        self.timeout_sec = timeout_sec
        self.check_interval_sec = check_interval_sec
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self.check_interval_sec):
            age = self.heartbeat.age()
            if age > self.timeout_sec:
                try:
                    self.heartbeat.beat(status='watchdog_exit', age_sec=round(age, 2))
                finally:
                    os._exit(70)


async def heartbeat_loop(heartbeat: Heartbeat, interval_sec: int = 10) -> None:
    while True:
        heartbeat.beat(status='alive')
        await asyncio.sleep(interval_sec)
