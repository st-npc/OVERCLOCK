"""Job lifecycle: figure out how much work each device can take, split the
batch, dispatch it concurrently, and guarantee every image gets processed
somewhere — even if every helper drops out.

This is where the "never make things worse than doing nothing" guarantee
lives: any failure on a helper's chunk falls back to local processing of
that same chunk rather than being dropped or hanging the run.
"""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import net_client
import task
from device_monitor import DeviceMonitor

OUTPUT_DIR = "processed_output"
MIN_LOCAL_SHARE = 0.2   # local always keeps at least this fraction, even fully loaded
MAX_LOCAL_SHARE = 1.0


class JobAlreadyRunningError(Exception):
    pass


class Orchestrator:
    def __init__(self, monitor: DeviceMonitor, event_queue):
        self.monitor = monitor
        self.events = event_queue
        self._job_lock = threading.Lock()
        self._job_running = False
        self.current_job_id = None

    def _emit(self, event_type: str, **fields):
        self.events.put({"type": event_type, "ts": time.time(), **fields})

    def log(self, message: str, level: str = "info"):
        self._emit("log", level=level, message=message)

    def is_running(self) -> bool:
        with self._job_lock:
            return self._job_running

    def start_job_async(self, helper_addresses: list, num_images: int = 24):
        with self._job_lock:
            if self._job_running:
                raise JobAlreadyRunningError("A job is already running")
            self._job_running = True
        thread = threading.Thread(
            target=self._run_job_safely, args=(helper_addresses, num_images), daemon=True
        )
        thread.start()

    def _run_job_safely(self, helper_addresses, num_images):
        job_id = uuid.uuid4().hex[:8]
        self.current_job_id = job_id
        try:
            self._run_job(job_id, helper_addresses, num_images)
        except Exception as exc:  # a bug here must never leave the UI stuck
            self.log(f"Job {job_id} failed unexpectedly: {exc}", level="error")
            self._emit("done", job_id=job_id, ok=False, error=str(exc))
        finally:
            with self._job_lock:
                self._job_running = False
            self.monitor.local.set_manual_busy(False)

    # -- core job logic --------------------------------------------------
    def _run_job(self, job_id: str, helper_addresses: list, num_images: int):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self._emit("job_start", job_id=job_id, num_images=num_images, helpers=helper_addresses)
        self.log(f"Job {job_id}: generating {num_images} demo images")
        images = task.make_demo_images(num_images)

        reachable = self._probe_helpers(helper_addresses)
        if helper_addresses and not reachable:
            self.log(
                f"Job {job_id}: none of {len(helper_addresses)} configured helper(s) are reachable — "
                "falling back to fully local processing",
                level="warn",
            )
        elif not helper_addresses:
            self.log(f"Job {job_id}: no helpers configured — running fully local", level="info")

        shares = self._compute_shares(reachable)
        counts = self._split_counts(num_images, shares)
        assignments = self._build_assignments(counts)

        share_desc = ", ".join(f"{name}={c}" for name, c in counts.items() if c > 0)
        self.log(f"Job {job_id}: split -> {share_desc}")

        results = [None] * num_images
        device_counts = {name: 0 for name in counts}
        pool = ThreadPoolExecutor(max_workers=max(1, len(assignments)))
        pending = []

        for target, indices in assignments.items():
            if not indices:
                continue
            fut = pool.submit(self._process_chunk, job_id, target, indices, images)
            pending.append((target, indices, fut))

        # Resolve futures, handling fallback for any helper chunk that fails.
        fallback_futs = []
        for target, indices, fut in pending:
            try:
                chunk_results, elapsed = fut.result()
                for idx, img in zip(indices, chunk_results):
                    results[idx] = img
                device_counts[target] = device_counts.get(target, 0) + len(indices)
                self.log(
                    f"Job {job_id}: {target} finished {len(indices)} image(s) in {elapsed:.2f}s"
                )
            except net_client.HelperError as exc:
                self.log(
                    f"Job {job_id}: helper {target} dropped mid-job ({exc}) — "
                    f"reassigning its {len(indices)} image(s) to local",
                    level="warn",
                )
                fb_fut = pool.submit(self._process_chunk, job_id, "local", indices, images)
                fallback_futs.append(("local", indices, fb_fut))

        for target, indices, fut in fallback_futs:
            try:
                chunk_results, elapsed = fut.result()
                for idx, img in zip(indices, chunk_results):
                    results[idx] = img
                device_counts[target] = device_counts.get(target, 0) + len(indices)
                self.log(f"Job {job_id}: local fallback finished {len(indices)} image(s) in {elapsed:.2f}s")
            except Exception as exc:
                # Local processing must never fail for a demo image batch;
                # if it somehow does, surface it loudly rather than silently
                # dropping images from the output.
                self.log(f"Job {job_id}: local fallback FAILED for {len(indices)} image(s): {exc}", level="error")

        pool.shutdown(wait=True)

        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            self.log(
                f"Job {job_id}: {len(missing)} image(s) could not be processed by any device", level="error"
            )

        saved = self._save_outputs(job_id, results)
        breakdown = ", ".join(f"{name}: {c}" for name, c in device_counts.items() if c > 0)
        self.log(f"Job {job_id}: complete — {saved} image(s) saved to {OUTPUT_DIR}/ ({breakdown})")
        self._emit(
            "done",
            job_id=job_id,
            ok=len(missing) == 0,
            saved=saved,
            missing=len(missing),
            breakdown=device_counts,
        )

    def _probe_helpers(self, helper_addresses: list) -> list:
        """Fresh reachability probe at job start (not just relying on the
        1s-cadence background poll), so brand-new helpers configured in the
        same Start click are picked up immediately."""
        reachable = []
        for addr in helper_addresses:
            self.monitor.add_helper(addr)
            rec = self.monitor.get(addr)
            try:
                data = net_client.fetch_stats(addr)
                rec.record_success(
                    cpu_percent=data["cpu_percent"],
                    ram_percent=data["ram_percent"],
                    ram_free_gb=data["ram_free_gb"],
                    ram_total_gb=data["ram_total_gb"],
                    reported_status=data.get("status"),
                )
                snap = rec.snapshot()
                if snap["status"] in ("idle", "reconnecting") and snap["spare_score"] > 0:
                    reachable.append((addr, snap["spare_score"]))
                else:
                    self.log(f"Helper {addr} reachable but reports no spare capacity right now", level="warn")
            except net_client.HelperError as exc:
                rec.record_failure(str(exc))
                self.log(f"Helper {addr} unreachable at job start ({exc})", level="warn")
        return reachable

    def _compute_shares(self, reachable_helpers: list) -> dict:
        local_snap = self.monitor.local.snapshot()
        overload = max(local_snap["cpu_percent"], local_snap["ram_percent"]) / 100.0
        local_share = min(MAX_LOCAL_SHARE, max(MIN_LOCAL_SHARE, 1.0 - overload))

        if not reachable_helpers:
            return {"local": 1.0}

        total_score = sum(score for _, score in reachable_helpers)
        if total_score <= 0:
            return {"local": 1.0}

        offload = 1.0 - local_share
        shares = {"local": local_share}
        for addr, score in reachable_helpers:
            shares[addr] = offload * (score / total_score)
        return shares

    @staticmethod
    def _split_counts(n: int, shares: dict) -> dict:
        # Largest-remainder method so counts sum exactly to n.
        raw = {name: n * frac for name, frac in shares.items()}
        floors = {name: int(v) for name, v in raw.items()}
        remainder = n - sum(floors.values())
        remainders_sorted = sorted(raw.keys(), key=lambda k: raw[k] - floors[k], reverse=True)
        for name in remainders_sorted[:remainder]:
            floors[name] += 1
        return floors

    @staticmethod
    def _build_assignments(counts: dict) -> dict:
        assignments = {}
        cursor = 0
        # deterministic order: local first, then helpers
        ordered = sorted(counts.keys(), key=lambda k: (k != "local", k))
        for name in ordered:
            c = counts[name]
            assignments[name] = list(range(cursor, cursor + c))
            cursor += c
        return assignments

    def _process_chunk(self, job_id: str, target: str, indices: list, images: list):
        chunk = [images[i] for i in indices]
        start = time.monotonic()
        if target == "local":
            self.monitor.local.set_manual_busy(True)
            try:
                result = task.process_batch(chunk)
            finally:
                self.monitor.local.set_manual_busy(False)
            return result["images"], time.monotonic() - start
        else:
            data = net_client.send_process_chunk(target, job_id, f"{indices[0]}-{indices[-1]}", chunk)
            return data["images"], time.monotonic() - start

    @staticmethod
    def _save_outputs(job_id: str, results: list) -> int:
        saved = 0
        job_dir = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        for idx, img_b64 in enumerate(results):
            if img_b64 is None:
                continue
            try:
                img = task.decode_image(img_b64)
                img.save(os.path.join(job_dir, f"image_{idx:03d}.png"))
                saved += 1
            except Exception:
                continue
        return saved
