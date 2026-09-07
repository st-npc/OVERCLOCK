"""Live device state: local + helper stats polling, rolling history for
sparklines, a status state machine, and spare-capacity scoring.

This module owns all *state about devices*. It knows nothing about job
splitting or HTTP routes — orchestrator.py reads from it to decide work
splits, and app_main.py reads from it to render the dashboard.
"""
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import psutil

import net_client

HISTORY_LEN = 60          # ~60 samples at 1s poll interval = 60s rolling window
POLL_INTERVAL = 1.0
RAM_FACTOR_CAP_GB = 1.0   # free RAM beyond this doesn't add extra spare-capacity credit

STATUS_CONNECTING = "connecting"
STATUS_IDLE = "idle"
STATUS_BUSY = "busy"
STATUS_UNREACHABLE = "unreachable"
STATUS_RECONNECTING = "reconnecting"


def _spare_score(status: str, cpu_percent: float, ram_free_gb: float) -> float:
    if status in (STATUS_UNREACHABLE, STATUS_CONNECTING):
        return 0.0
    cpu_headroom = max(0.0, 100.0 - cpu_percent) / 100.0
    ram_factor = min(1.0, ram_free_gb / RAM_FACTOR_CAP_GB) if ram_free_gb > 0 else 0.0
    score = cpu_headroom * ram_factor
    if status == STATUS_BUSY:
        # Already working on something for us right now — no free capacity
        # to hand out until the current chunk finishes.
        return 0.0
    return round(score, 4)


class DeviceRecord:
    def __init__(self, device_id: str, name: str, kind: str):
        self.id = device_id
        self.name = name
        self.kind = kind  # "local" | "helper"
        self.lock = threading.Lock()
        self.status = STATUS_CONNECTING
        self.cpu_percent = 0.0
        self.ram_percent = 0.0
        self.ram_free_gb = 0.0
        self.ram_total_gb = 0.0
        self.spare_score = 0.0
        self.consecutive_failures = 0
        self.last_error = None
        self.last_seen = None
        self.manual_busy = False  # set by orchestrator while local device is working
        self.history_cpu = deque(maxlen=HISTORY_LEN)
        self.history_ram = deque(maxlen=HISTORY_LEN)
        self.history_free_gb = deque(maxlen=HISTORY_LEN)

    def record_success(self, cpu_percent, ram_percent, ram_free_gb, ram_total_gb, reported_status=None):
        with self.lock:
            self.consecutive_failures = 0
            self.last_error = None
            self.last_seen = time.time()
            self.cpu_percent = cpu_percent
            self.ram_percent = ram_percent
            self.ram_free_gb = ram_free_gb
            self.ram_total_gb = ram_total_gb

            if self.manual_busy:
                new_status = STATUS_BUSY
            elif reported_status == STATUS_BUSY:
                new_status = STATUS_BUSY
            elif self.status == STATUS_UNREACHABLE:
                # First successful poll after being down: show as
                # "reconnecting" for one cycle before trusting it fully.
                new_status = STATUS_RECONNECTING
            else:
                new_status = STATUS_IDLE
            self.status = new_status

            self.spare_score = _spare_score(new_status, cpu_percent, ram_free_gb)
            self.history_cpu.append(cpu_percent)
            self.history_ram.append(ram_percent)
            self.history_free_gb.append(ram_free_gb)

    def record_failure(self, error: str):
        with self.lock:
            self.consecutive_failures += 1
            self.status = STATUS_UNREACHABLE
            self.last_error = error
            self.spare_score = 0.0
            # Still append to history so sparklines show the drop to zero
            # instead of silently freezing.
            self.history_cpu.append(0.0)
            self.history_ram.append(self.ram_percent)
            self.history_free_gb.append(0.0)

    def set_manual_busy(self, busy: bool):
        with self.lock:
            self.manual_busy = busy
            if busy:
                self.status = STATUS_BUSY
                self.spare_score = 0.0
            elif self.status != STATUS_UNREACHABLE:
                self.status = STATUS_IDLE
                self.spare_score = _spare_score(STATUS_IDLE, self.cpu_percent, self.ram_free_gb)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "name": self.name,
                "kind": self.kind,
                "status": self.status,
                "cpu_percent": round(self.cpu_percent, 1),
                "ram_percent": round(self.ram_percent, 1),
                "ram_free_gb": round(self.ram_free_gb, 2),
                "ram_total_gb": round(self.ram_total_gb, 2),
                "spare_score": self.spare_score,
                "last_error": self.last_error,
                "history": {
                    "cpu": list(self.history_cpu),
                    "ram": list(self.history_ram),
                    "free_gb": list(self.history_free_gb),
                },
            }


def _local_stats() -> dict:
    vm = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": vm.percent,
        "ram_free_gb": vm.available / (1024 ** 3),
        "ram_total_gb": vm.total / (1024 ** 3),
    }


class DeviceMonitor:
    def __init__(self):
        self._devices_lock = threading.RLock()
        self.devices = {}
        self.local = DeviceRecord("local", "This device", "local")
        self.devices["local"] = self.local
        # Prime psutil's internal baseline so the first cpu_percent() call
        # doesn't always report 0.0.
        psutil.cpu_percent(interval=None)

        self._stop_event = threading.Event()
        self._thread = None
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="monitor-poll")

    # -- helper registry -------------------------------------------------
    def add_helper(self, address: str) -> str:
        address = address.strip()
        with self._devices_lock:
            if address not in self.devices:
                self.devices[address] = DeviceRecord(address, address, "helper")
            return address

    def remove_helper(self, address: str):
        with self._devices_lock:
            self.devices.pop(address, None)

    def helper_ids(self) -> list:
        with self._devices_lock:
            return [d for d, rec in self.devices.items() if rec.kind == "helper"]

    def get(self, device_id: str) -> DeviceRecord:
        with self._devices_lock:
            return self.devices.get(device_id)

    # -- polling lifecycle -------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _poll_loop(self):
        while not self._stop_event.is_set():
            start = time.monotonic()
            try:
                stats = _local_stats()
                self.local.record_success(**stats)
            except Exception as exc:  # local psutil should never fail, but never crash the loop
                self.local.record_failure(str(exc))

            with self._devices_lock:
                helper_ids = [d for d, rec in self.devices.items() if rec.kind == "helper"]

            if helper_ids:
                futures = {self._pool.submit(self._poll_one_helper, hid): hid for hid in helper_ids}
                for fut in futures:
                    fut.result()  # exceptions are already caught inside _poll_one_helper

            elapsed = time.monotonic() - start
            self._stop_event.wait(max(0.0, POLL_INTERVAL - elapsed))

    def _poll_one_helper(self, helper_id: str):
        rec = self.get(helper_id)
        if rec is None:
            return
        try:
            data = net_client.fetch_stats(helper_id)
            rec.record_success(
                cpu_percent=data["cpu_percent"],
                ram_percent=data["ram_percent"],
                ram_free_gb=data["ram_free_gb"],
                ram_total_gb=data["ram_total_gb"],
                reported_status=data.get("status"),
            )
        except net_client.HelperError as exc:
            rec.record_failure(str(exc))

    # -- snapshots for UI/orchestrator -------------------------------------
    def snapshot_all(self) -> list:
        with self._devices_lock:
            records = list(self.devices.values())
        # local device first, then helpers in registration order
        records.sort(key=lambda r: (r.kind != "local", r.id))
        return [r.snapshot() for r in records]

    def reachable_helpers_with_score(self) -> list:
        """Helpers currently believed usable for a new job, with their
        latest spare_score. Excludes unreachable/connecting/busy helpers."""
        result = []
        with self._devices_lock:
            helpers = [rec for rec in self.devices.values() if rec.kind == "helper"]
        for rec in helpers:
            snap = rec.snapshot()
            if snap["status"] in (STATUS_IDLE, STATUS_RECONNECTING) and snap["spare_score"] > 0:
                result.append((rec.id, snap["spare_score"]))
        return result
