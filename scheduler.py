"""Adaptive, theoretically-grounded work-splitting.

The original heuristic (kept here as `proportional_snapshot`, still fully
selectable) sizes every device's share purely from its *current* CPU/RAM
snapshot. That is reasonable but memoryless: it never learns a device's
actual sustained throughput once real chunks start finishing, and it has no
notion of dispatch/round-trip overhead at all — a helper across a slow relay
link is scored identically to one on the same switch.

`adaptive_dlt` replaces that lookup with a small, well-established piece of
scheduling theory: Divisible Load Theory (DLT) balances a divisible job
across heterogeneous workers so that every worker given nonzero load finishes
at the same time — the makespan-minimising allocation for linear per-item
cost with a fixed per-worker dispatch overhead (closed-form for two
processors in Cheng & Robertazzi, 1988; generalised to arbitrary numbers of
processors via the same equal-finish-time argument in the survey literature,
e.g. Robertazzi, "Ten Reasons to Use Divisible Load Theory", IEEE Computer,
2003). Full references and the derivation used here are in docs/RESEARCH.md.

Two practical layers sit on top of the closed-form allocation:
  - EWMA-learned per-device rate (items/sec) and overhead (seconds), fed by
    real chunk completions and probe round-trips. A device with no history
    yet is bootstrapped from its live spare-capacity score — which makes
    `proportional_snapshot` exactly the zero-history special case of this
    model, not a competing algorithm.
  - An EWMA reliability score per device that discounts a flaky/failing
    device's *effective* rate without ever hard-banning it, the same idea
    volunteer/grid-computing schedulers (e.g. BOINC's per-host scheduling)
    use to avoid permanently starving a device that just had one bad run.

`solve_dlt_shares()` is the pure, stateless allocation core — also used
directly by bench/simulate.py for offline evaluation with synthetic
rates/overheads, no Flask/psutil involved. `SchedulerState` is the small
stateful wrapper orchestrator.py drives during a real run.
"""
import threading

MIN_LOCAL_SHARE = 0.2   # local always keeps at least this fraction, even fully loaded
MAX_LOCAL_SHARE = 1.0

BOOTSTRAP_RATE = 5.0             # assumed items/sec for a device with zero history, scaled by its spare-score
EWMA_ALPHA_RATE = 0.35           # weight on the newest observation when learning throughput
EWMA_ALPHA_OVERHEAD = 0.35       # weight on the newest observation when learning dispatch overhead
EWMA_ALPHA_RELIABILITY = 0.25    # weight on the newest outcome when learning reliability
MIN_EFFECTIVE_FRACTION = 0.05    # floor applied to spare-score/reliability so a device is never priced out permanently
MIN_RATE = 1e-6

STRATEGIES = ("adaptive_dlt", "proportional_snapshot", "fixed_equal")
DEFAULT_STRATEGY = "adaptive_dlt"


def _local_overload_fraction(local_snapshot: dict) -> float:
    return max(local_snapshot["cpu_percent"], local_snapshot["ram_percent"]) / 100.0


def fixed_equal(local_snapshot: dict, reachable_helpers: list) -> dict:
    """Naive baseline: split evenly across local + every reachable helper,
    ignoring load entirely. Kept only as a comparison point for
    bench/simulate.py — selectable from the dashboard for demoing *why*
    load-aware splitting matters, never a good default."""
    names = ["local"] + [addr for addr, _score in reachable_helpers]
    n = len(names)
    return {name: 1.0 / n for name in names}


def proportional_snapshot(local_snapshot: dict, reachable_helpers: list) -> dict:
    """Original heuristic: local share from current overload, helper shares
    split proportional to their current spare-score snapshot. Memoryless —
    two helpers with an identical snapshot get an identical share even if
    one has a much faster CPU that just hasn't shown up in this instant's
    reading yet."""
    overload = _local_overload_fraction(local_snapshot)
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


def solve_dlt_shares(capacities: dict, min_shares: dict = None) -> dict:
    """Pure Divisible Load allocation via water-filling over a shared deadline.

    `capacities`: {device_id: (rate, overhead)} where `rate` is items/sec and
    `overhead` is a fixed per-dispatch cost in seconds (0 for local).
    `min_shares`: optional {device_id: minimum fraction of the load it must
    keep}, used to enforce guarantees like "local never drops below 20%".

    Returns {device_id: fraction}, fractions summing to 1.0 over every key in
    `capacities` (0.0 for any device priced out of the deadline).

    Method: pick a shared deadline T such that giving every active device
    load = rate * (T - overhead) exactly consumes the whole job (one unit of
    work). Any device whose own overhead exceeds T would need negative load,
    which isn't physical — drop it and re-solve for the remaining devices.
    This terminates in at most len(capacities) rounds (each round either
    finishes or removes >= 1 device). The result is the classical
    equal-finish-time allocation: for linear divisible-load cost, no other
    split of a fixed total achieves a smaller makespan across the active set.
    """
    active = {d: cap for d, cap in capacities.items() if cap[0] > MIN_RATE}
    if not active:
        return {d: 0.0 for d in capacities}

    loads = {}
    while True:
        total_rate = sum(rate for rate, _overhead in active.values())
        weighted_overhead = sum(rate * overhead for rate, overhead in active.values())
        deadline = (1.0 + weighted_overhead) / total_rate
        loads = {d: rate * (deadline - overhead) for d, (rate, overhead) in active.items()}
        negative = [d for d, load in loads.items() if load < 0]
        if not negative:
            break
        for d in negative:
            active.pop(d)
        if not active:
            return {d: 0.0 for d in capacities}

    shares = {d: 0.0 for d in capacities}
    shares.update(loads)

    if min_shares:
        # Enforce floors (we only ever set one, for "local") by bumping any
        # device below its floor up to it and shrinking every other device
        # proportionally by the deficit. A single pass is enough since only
        # one device ever has a floor here, so it can't cascade into a
        # second violation.
        for d, floor in min_shares.items():
            current = shares.get(d, 0.0)
            if current < floor:
                deficit = floor - current
                others = [k for k in shares if k != d]
                other_total = sum(shares[k] for k in others)
                shares[d] = floor
                if other_total > 0:
                    for k in others:
                        shares[k] = max(0.0, shares[k] - deficit * (shares[k] / other_total))

    total = sum(shares.values())
    if total > 0:
        shares = {d: v / total for d, v in shares.items()}
    return shares


class SchedulerState:
    """Per-process learned state: EWMA throughput/overhead/reliability per
    device, fed by real chunk completions and probe latencies. Not persisted
    across restarts — a cold start just falls back to the spare-capacity
    bootstrap, which is deliberately how `proportional_snapshot` behaves for
    every device, always."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rate = {}          # device -> EWMA items/sec
        self._overhead = {}      # device -> EWMA seconds
        self._reliability = {}   # device -> EWMA in [0, 1]

    def record_probe_latency(self, device: str, seconds: float):
        with self._lock:
            prev = self._overhead.get(device)
            self._overhead[device] = seconds if prev is None else (
                EWMA_ALPHA_OVERHEAD * seconds + (1 - EWMA_ALPHA_OVERHEAD) * prev
            )

    def record_chunk_result(self, device: str, success: bool, count: int = 0, elapsed: float = 0.0):
        with self._lock:
            prev_rel = self._reliability.get(device, 1.0)
            outcome = 1.0 if success else 0.0
            self._reliability[device] = (
                EWMA_ALPHA_RELIABILITY * outcome + (1 - EWMA_ALPHA_RELIABILITY) * prev_rel
            )
            if success and count > 0 and elapsed > 0:
                observed_rate = count / elapsed
                prev_rate = self._rate.get(device)
                self._rate[device] = observed_rate if prev_rate is None else (
                    EWMA_ALPHA_RATE * observed_rate + (1 - EWMA_ALPHA_RATE) * prev_rate
                )

    def snapshot(self) -> dict:
        with self._lock:
            devices = set(self._rate) | set(self._overhead) | set(self._reliability)
            return {
                d: {
                    "learned_rate_items_per_sec": round(self._rate.get(d, 0.0), 3),
                    "overhead_seconds": round(self._overhead.get(d, 0.0), 3),
                    "reliability": round(self._reliability.get(d, 1.0), 3),
                }
                for d in sorted(devices)
            }

    def _effective_capacity(self, device: str, spare_score: float) -> tuple:
        with self._lock:
            rate = self._rate.get(device)
            overhead = self._overhead.get(device, 0.0)
            reliability = self._reliability.get(device, 1.0)
        if rate is None:
            # Cold start: bootstrap purely from the live spare-capacity
            # score — the same signal proportional_snapshot relies on
            # exclusively — until real throughput observations arrive.
            rate = BOOTSTRAP_RATE * max(MIN_EFFECTIVE_FRACTION, spare_score)
        return rate * max(MIN_EFFECTIVE_FRACTION, reliability), overhead

    def adaptive_dlt(self, local_snapshot: dict, reachable_helpers: list) -> dict:
        local_spare = 1.0 - _local_overload_fraction(local_snapshot)
        capacities = {"local": self._effective_capacity("local", local_spare)}
        for addr, score in reachable_helpers:
            capacities[addr] = self._effective_capacity(addr, score)
        return solve_dlt_shares(capacities, min_shares={"local": MIN_LOCAL_SHARE})

    def compute_shares(self, strategy: str, local_snapshot: dict, reachable_helpers: list) -> dict:
        if strategy == "fixed_equal":
            return fixed_equal(local_snapshot, reachable_helpers)
        if strategy == "proportional_snapshot":
            return proportional_snapshot(local_snapshot, reachable_helpers)
        return self.adaptive_dlt(local_snapshot, reachable_helpers)
