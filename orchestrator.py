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
from device_monitor import DeviceMonitor
from tasks import DEFAULT_TASK, get_task

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

    def start_job_async(self, helper_addresses: list, num_images: int = 24, task_type: str = DEFAULT_TASK):
        with self._job_lock:
            if self._job_running:
                raise JobAlreadyRunningError("A job is already running")
            self._job_running = True
        thread = threading.Thread(
            target=self._run_job_safely, args=(helper_addresses, num_images, task_type), daemon=True
        )
        thread.start()

    def _run_job_safely(self, helper_addresses, num_images, task_type):
        job_id = uuid.uuid4().hex[:8]
        self.current_job_id = job_id
        self.monitor.begin_job()  # fast polling for the whole job, not just dispatch
        try:
            self._run_job(job_id, helper_addresses, num_images, task_type)
        except Exception as exc:  # a bug here must never leave the UI stuck
            self.log(f"Job {job_id} failed unexpectedly: {exc}", level="error")
            self._emit("done", job_id=job_id, ok=False, error=str(exc))
        finally:
            with self._job_lock:
                self._job_running = False
            self.monitor.local.set_manual_busy(False)
            self.monitor.end_job()

    # -- core job logic --------------------------------------------------
    def _run_job(self, job_id: str, helper_addresses: list, num_images: int, task_type: str = DEFAULT_TASK):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        task_mod = get_task(task_type)
        self._emit(
            "job_start", job_id=job_id, num_images=num_images, helpers=helper_addresses,
            task_type=task_type, task_name=task_mod.DISPLAY_NAME,
        )
        self.log(f"Job {job_id}: generating {num_images} work item(s) for '{task_mod.DISPLAY_NAME}'")
        items = task_mod.generate_work(num_images)

        reachable, probe_summary = self._probe_helpers(helper_addresses)
        if helper_addresses and not reachable:
            if probe_summary["unreachable"] == len(helper_addresses):
                reason = f"none of {len(helper_addresses)} configured helper(s) are reachable"
            else:
                bits = []
                if probe_summary["no_capacity"]:
                    bits.append(f"{probe_summary['no_capacity']} reachable but with no spare capacity")
                if probe_summary["paused"]:
                    bits.append(f"{probe_summary['paused']} paused by their operator")
                if probe_summary["unreachable"]:
                    bits.append(f"{probe_summary['unreachable']} unreachable")
                reason = f"none of {len(helper_addresses)} configured helper(s) can take work right now ({', '.join(bits)})"
            self.log(f"Job {job_id}: {reason} — falling back to fully local processing", level="warn")
        elif not helper_addresses:
            self.log(f"Job {job_id}: no helpers configured — running fully local", level="info")

        shares = self._compute_shares(reachable)
        counts = self._split_counts(num_images, shares)
        assignments = self._build_assignments(counts)

        share_desc = ", ".join(f"{name}={c}" for name, c in counts.items() if c > 0)
        self.log(f"Job {job_id}: split -> {share_desc}")
        self._emit("split", job_id=job_id, counts={k: v for k, v in counts.items() if v > 0})

        # Track real before/peak CPU & RAM for every device about to do work,
        # so the UI can show exactly how much load shifted — not just how
        # many items each device got.
        tracked_targets = {name for name, c in counts.items() if c > 0}
        tracked_targets.add("local")  # helper chunks can fall back to local mid-job
        for name in tracked_targets:
            rec = self.monitor.get(name)
            if rec:
                rec.start_tracking()

        results = [None] * num_images
        device_counts = {name: 0 for name in counts}
        device_elapsed = {name: 0.0 for name in counts}
        compression_ratios = []
        job_start_mono = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=max(1, len(assignments)))
        pending = []

        for target, indices in assignments.items():
            if not indices:
                continue
            fut = pool.submit(self._process_chunk, job_id, target, indices, items, task_type)
            pending.append((target, indices, fut))

        # Resolve futures, handling fallback for any helper chunk that fails.
        fallback_futs = []
        for target, indices, fut in pending:
            try:
                chunk_results, elapsed, compression = fut.result()
                for idx, item in zip(indices, chunk_results):
                    results[idx] = item
                device_counts[target] = device_counts.get(target, 0) + len(indices)
                device_elapsed[target] = device_elapsed.get(target, 0.0) + elapsed
                if compression:
                    compression_ratios.append(compression["response_ratio"])
                comp_note = f", compressed to {compression['response_ratio']*100:.0f}% of original" if compression else ""
                self.log(
                    f"Job {job_id}: {target} finished {len(indices)} item(s) in {elapsed:.2f}s{comp_note}"
                )
                self._emit(
                    "chunk_done", job_id=job_id, target=target, count=len(indices), elapsed=elapsed, compression=compression
                )
            except net_client.HelperError as exc:
                self.log(
                    f"Job {job_id}: helper {target} dropped mid-job ({exc}) — "
                    f"reassigning its {len(indices)} item(s) to local",
                    level="warn",
                )
                self._emit("chunk_failed", job_id=job_id, target=target, count=len(indices), reason=str(exc))
                fb_fut = pool.submit(self._process_chunk, job_id, "local", indices, items, task_type)
                fallback_futs.append(("local", indices, fb_fut))

        for target, indices, fut in fallback_futs:
            try:
                chunk_results, elapsed, _compression = fut.result()
                for idx, item in zip(indices, chunk_results):
                    results[idx] = item
                device_counts[target] = device_counts.get(target, 0) + len(indices)
                device_elapsed[target] = device_elapsed.get(target, 0.0) + elapsed
                self.log(f"Job {job_id}: local fallback finished {len(indices)} item(s) in {elapsed:.2f}s")
                self._emit(
                    "chunk_done", job_id=job_id, target=target, count=len(indices), elapsed=elapsed, fallback=True
                )
            except Exception as exc:
                # Local processing must never fail for a demo batch; if it
                # somehow does, surface it loudly rather than silently
                # dropping items from the output.
                self.log(f"Job {job_id}: local fallback FAILED for {len(indices)} item(s): {exc}", level="error")

        pool.shutdown(wait=True)
        job_wall_seconds = time.monotonic() - job_start_mono

        device_load = {}
        for name in tracked_targets:
            rec = self.monitor.get(name)
            if rec:
                load = rec.stop_tracking()
                device_load[name] = load
                if device_counts.get(name):
                    self.log(
                        f"Job {job_id}: {name} CPU {load['baseline_cpu']:.0f}% -> peaked at "
                        f"{load['peak_cpu']:.0f}% while processing"
                    )

        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            self.log(
                f"Job {job_id}: {len(missing)} item(s) could not be processed by any device", level="error"
            )

        saved = self._save_outputs(job_id, results, task_mod)
        breakdown = ", ".join(f"{name}: {c}" for name, c in device_counts.items() if c > 0)
        self.log(f"Job {job_id}: complete — {saved} item(s) saved to {OUTPUT_DIR}/ ({breakdown})")

        # Session summary: estimate what fully-local processing would have
        # cost, using the local device's own measured per-item rate this run.
        local_count = device_counts.get("local", 0)
        estimated_local_seconds = None
        time_saved_seconds = None
        if local_count > 0:
            local_rate = device_elapsed.get("local", 0.0) / local_count
            estimated_local_seconds = round(local_rate * num_images, 2)
            time_saved_seconds = round(max(0.0, estimated_local_seconds - job_wall_seconds), 2)
            if time_saved_seconds > 0:
                self.log(
                    f"Job {job_id}: took {job_wall_seconds:.2f}s vs an estimated {estimated_local_seconds:.2f}s "
                    f"fully local — saved ~{time_saved_seconds:.2f}s"
                )

        self._emit(
            "done",
            job_id=job_id,
            ok=len(missing) == 0,
            saved=saved,
            missing=len(missing),
            breakdown=device_counts,
            device_elapsed={k: round(v, 2) for k, v in device_elapsed.items()},
            device_load=device_load,
            job_wall_seconds=round(job_wall_seconds, 2),
            estimated_local_seconds=estimated_local_seconds,
            time_saved_seconds=time_saved_seconds,
            avg_compression_ratio=round(sum(compression_ratios) / len(compression_ratios), 4) if compression_ratios else None,
        )

    def _probe_helpers(self, helper_addresses: list) -> tuple:
        """Fresh reachability probe at job start (not just relying on the
        1s-cadence background poll), so brand-new helpers configured in the
        same Start click are picked up immediately."""
        reachable = []
        summary = {"unreachable": 0, "no_capacity": 0, "paused": 0}
        for addr in helper_addresses:
            self.monitor.add_helper(addr)
            rec = self.monitor.get(addr)
            try:
                data = net_client.fetch_stats(addr, passphrase=self.monitor.passphrase)
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
                elif snap["status"] == "paused":
                    summary["paused"] += 1
                    self.log(f"Helper {addr} is paused by its operator — not sending it work", level="warn")
                else:
                    summary["no_capacity"] += 1
                    self.log(f"Helper {addr} reachable but reports no spare capacity right now", level="warn")
            except net_client.HelperError as exc:
                summary["unreachable"] += 1
                rec.record_failure(str(exc))
                self.log(f"Helper {addr} unreachable at job start ({exc})", level="warn")
        return reachable, summary

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

    def _process_chunk(self, job_id: str, target: str, indices: list, items: list, task_type: str):
        chunk = [items[i] for i in indices]
        start = time.monotonic()
        if target == "local":
            self.monitor.local.set_manual_busy(True)
            try:
                result = get_task(task_type).process_batch(chunk)
            finally:
                self.monitor.local.set_manual_busy(False)
            return result["items"], time.monotonic() - start, None
        else:
            data = net_client.send_process_chunk(
                target, job_id, f"{indices[0]}-{indices[-1]}", chunk, task_type, passphrase=self.monitor.passphrase
            )
            return data["items"], time.monotonic() - start, data.get("_compression")

    @staticmethod
    def _save_outputs(job_id: str, results: list, task_mod) -> int:
        saved = 0
        job_dir = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        for idx, item in enumerate(results):
            if item is None:
                continue
            try:
                task_mod.save_result(item, os.path.join(job_dir, f"item_{idx:03d}"))
                saved += 1
            except Exception:
                continue
        return saved
