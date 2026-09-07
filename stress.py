"""Synthetic local load generator — a demo tool, not part of the sharing
pipeline. Lets you deliberately overload *this* device (real CPU/RAM
load, measured by the same psutil calls device_monitor already polls) so
you can watch the dashboard react honestly: spare capacity drops, the
next job's split shifts toward whatever helpers are available, exactly
as it would if the load were real. Nothing in orchestrator.py or
device_monitor.py needs to know this exists — it works purely by making
the local machine genuinely busier.

Runs workers as separate OS processes (not threads) so CPU load isn't
capped by the GIL to a single core, and so a hard stop (terminate) always
works even if a worker loop misbehaves. Every worker is a daemon process,
which is an extra safety net beyond `stop()`: if app_main.py itself dies
without cleanly stopping the generator, the OS-level daemon flag makes
Python terminate them anyway when the parent process exits.
"""
import multiprocessing
import os
import time

import psutil

MAX_CPU_WORKERS = os.cpu_count() or 4
MAX_RAM_MB = 4096
MIN_FREE_RAM_MB = 512  # never let RAM stress push the system below this


def _burn_cpu(stop_event):
    x = 0.0001
    while not stop_event.is_set():
        for _ in range(200_000):
            x = (x * 1.0000001) % 99991


def _burn_ram(stop_event, mb: int):
    block = bytes(1024 * 1024)  # template block, copied per-MB so pages are actually touched
    blocks = []
    try:
        for _ in range(mb):
            if stop_event.is_set():
                return
            blocks.append(bytearray(block))
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        blocks.clear()


class LoadGenerator:
    def __init__(self):
        self._stop_event = None
        self._procs = []
        self._cpu_workers = 0
        self._ram_mb = 0

    def is_active(self) -> bool:
        return bool(self._procs)

    def status(self) -> dict:
        return {
            "active": self.is_active(),
            "cpu_workers": self._cpu_workers,
            "ram_mb": self._ram_mb,
            "max_cpu_workers": MAX_CPU_WORKERS,
            "max_ram_mb": MAX_RAM_MB,
        }

    def start(self, cpu_workers: int, ram_mb: int) -> dict:
        self.stop()  # clean slate — no orphaned workers from a previous start

        cpu_workers = max(0, min(int(cpu_workers), MAX_CPU_WORKERS))
        ram_mb = max(0, min(int(ram_mb), MAX_RAM_MB))

        available_mb = psutil.virtual_memory().available / (1024 * 1024)
        allowed_ram_mb = max(0, int(available_mb - MIN_FREE_RAM_MB))
        ram_mb = min(ram_mb, allowed_ram_mb)

        stop_event = multiprocessing.Event()
        procs = []

        for _ in range(cpu_workers):
            p = multiprocessing.Process(target=_burn_cpu, args=(stop_event,), daemon=True)
            p.start()
            procs.append(p)

        if ram_mb > 0:
            p = multiprocessing.Process(target=_burn_ram, args=(stop_event, ram_mb), daemon=True)
            p.start()
            procs.append(p)

        self._stop_event = stop_event
        self._procs = procs
        self._cpu_workers = cpu_workers
        self._ram_mb = ram_mb
        return self.status()

    def stop(self) -> dict:
        if self._procs:
            self._stop_event.set()
            for p in self._procs:
                p.join(timeout=2)
                if p.is_alive():
                    p.terminate()
        self._stop_event = None
        self._procs = []
        self._cpu_workers = 0
        self._ram_mb = 0
        return self.status()
