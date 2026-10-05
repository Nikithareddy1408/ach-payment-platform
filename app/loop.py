"""Runs a `step` function repeatedly on N background threads. When a step
finds no work, the thread sleeps poll_interval. stop() lets in-flight steps
finish first (graceful shutdown)."""
import logging
import threading
from typing import Callable

log = logging.getLogger("ach.loop")


class BackgroundLoop:
    def __init__(self, name: str, step: Callable[[int], bool], concurrency: int, poll_interval_s: float):
        self.name, self.step, self.concurrency, self.poll_interval_s = name, step, concurrency, poll_interval_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(self.concurrency):
            t = threading.Thread(target=self._run, args=(i,), name=f"{self.name}-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _run(self, index: int) -> None:
        while not self._stop.is_set():
            did_work = False
            try:
                did_work = self.step(index)
            except Exception:
                log.exception("background step failed", extra={"loop": self.name})
            if not did_work:
                self._stop.wait(self.poll_interval_s)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join()
        self._threads = []
