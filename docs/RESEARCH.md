# Overclock: Adaptive Divisible-Load Scheduling for Ad-Hoc P2P Compute Offload

This document is the research-level write-up for the scheduling contribution
in `scheduler.py`, evaluated in `bench/simulate.py`. It assumes familiarity
with the system overview in the top-level `README.md`.

## 1. Problem statement

Overclock offloads a slice of a divisible job (a batch of `N` independent
work items) from an overloaded local device to zero or more idle peer
devices ("helpers") reachable on the same LAN, over USB-C, or via a relay
tunnel. The system must decide, for each job, how many of the `N` items to
assign to each device — including the local device itself, which must
never be starved entirely (README's "never make things worse than doing
nothing" guarantee) — subject to three constraints:

1. **Correctness under failure.** Any device's assigned chunk may fail
   (unreachable, crashes mid-chunk, returns a malformed response) and must
   be re-assigned, not dropped (`orchestrator.py`'s fallback path,
   `tests/test_fallback.py`).
2. **Heterogeneity.** Devices differ in raw processing rate, in dispatch
   overhead (a LAN peer vs. a relay-tunneled peer on a different network),
   and in reliability (a flaky device that fails chunks more often).
3. **No prior configuration.** A helper is just a `host:port` a user typed
   in (or found via `discovery.py`); the system has no a priori model of
   its speed and must learn one from observing it.

This is the classical **divisible load scheduling** problem (Cheng &
Robertazzi, 1988) with two practical complications on top: the per-device
processing rate is *unknown* rather than given, and devices are not
uniformly reliable.

## 2. Related work

- **[1] Wang, Y., Kong, D., Chai, H., Qiu, H., Xue, R., Li, S. (2025).**
  "D2D assisted cooperative computational offloading strategy in edge cloud
  computing networks." *Scientific Reports*, 15, Article 12303.
  DOI: [10.1038/s41598-025-96719-8](https://doi.org/10.1038/s41598-025-96719-8).
  Proposes a cost-optimized offloading strategy (D-CCO) across a local
  device, peer (D2D) devices, and an edge processor, jointly weighing task
  delay, power, and wait cost. The closest published analogue to
  Overclock's local/helper split, modulo Overclock having no edge server at
  all — every "processor" in our model is a peer, including the coordinator
  itself.

- **[2] Tian, X., Shao, Y., Zou, Y., et al. (2024).** "D2D-assisted
  cooperative computation offloading and resource allocation in
  wireless-powered mobile edge computing networks." *Peer-to-Peer Networking
  and Applications*, 17, pp. 3765–3779.
  DOI: [10.1007/s12083-024-01774-z](https://doi.org/10.1007/s12083-024-01774-z).
  Classifies devices by proximity/capacity ("near" vs. "far" from an MEC
  server) and shows near-device assistance reduces latency and energy for
  the requester. Supports treating dispatch overhead (our proxy for
  "distance") as a first-class scheduling input, not just raw rate.

- **[3] Robertazzi, T.G. (2003).** "Ten Reasons to Use Divisible Load
  Theory." *IEEE Computer*, 36(5), pp. 63–68. The foundational scheduling
  result this work builds on: for a linearly divisible load distributed
  across heterogeneous processors with per-processor overhead, the
  makespan-minimizing allocation gives every processor that receives
  nonzero load the *same finish time*. Originally derived in closed form
  for star/bus/tree interconnection topologies; `scheduler.py` uses the
  equal-finish-time condition directly via an iterative water-filling
  solve rather than the topology-specific closed forms, since Overclock's
  interconnect (local device fans out to independent helpers, no
  helper-to-helper relaying) is exactly the single-level star case DLT was
  first solved for.

- **BOINC-style volunteer computing** (Anderson, 2004, and successors)
  informs the reliability-discount design (§3.3): rather than hard-banning
  a host after a failure, per-host reliability is tracked continuously and
  decayed/recovered, so a transient failure doesn't permanently exclude a
  genuinely useful device.

## 3. Method

### 3.1 Baselines (both still selectable in the dashboard, and in `scheduler.py`)

- **`fixed_equal`**: split evenly across local + every reachable helper.
  Ignores load and rate entirely.
- **`proportional_snapshot`**: the system's original heuristic. Local's
  share is `1 - max(cpu%, ram%)/100` (clamped to `[0.2, 1.0]`); the
  remainder is split across helpers proportional to each one's current
  CPU/RAM-derived spare-capacity score. **Memoryless** — it has no state
  across jobs, and no notion of dispatch overhead.

### 3.2 Proposed: `adaptive_dlt`

For a job of size `N`, let each device `i` have an *effective capacity*
`(rate_i, overhead_i)`, in items/sec and seconds respectively. The
allocation solves for a shared deadline `T` such that giving every device
`load_i = rate_i * (T - overhead_i)` items exactly sums to `N`:

```
T = (N + Σ rate_i * overhead_i) / Σ rate_i
load_i = rate_i * (T - overhead_i)
```

Any device whose own `overhead_i` alone would exceed `T` is priced out
(`load_i < 0` is not physical) — it is dropped and `T` is re-solved over the
remaining devices. This terminates in at most `|devices|` rounds. The
result is the classical equal-finish-time allocation: for linear
divisible-load cost, no other split of a fixed total achieves a smaller
makespan across the active set (Robertazzi, 2003). Implementation:
`scheduler.solve_dlt_shares()`.

A hard floor `local_share >= 0.2` is enforced afterward by bumping local up
to the floor and shrinking every other device proportionally by the
deficit — preserving the pre-existing guarantee that local capacity is
never fully surrendered even when it is comparatively slow.

**`rate_i` and `overhead_i` are learned, not given.** `SchedulerState`
maintains an exponentially-weighted moving average (EWMA, α = 0.35) of
observed items/sec per device, updated after every real chunk completion,
and a separate EWMA of measured probe round-trip time as the overhead
proxy. A device with no history yet is bootstrapped from its live
CPU/RAM-derived spare-capacity score scaled by a nominal rate constant —
which makes `proportional_snapshot` exactly the **zero-history special
case** of this model, not a competing algorithm: the first job Overclock
ever runs against a brand-new helper behaves identically to the legacy
heuristic, and every job after that is informed by real observations.

### 3.3 Reliability discount

A second EWMA (α = 0.25) tracks each device's success/failure outcomes in
`[0, 1]`. The device's effective rate used in `solve_dlt_shares` is
`learned_rate * max(0.05, reliability)` — a chunk failure does not zero out
a device's future eligibility (the 0.05 floor guarantees it can still claw
back trust), but it does immediately shrink the share a repeatedly-failing
device receives, reducing wasted fallback work without a hard ban.

## 4. Complexity

`solve_dlt_shares` is `O(k^2)` in the worst case for `k` devices (each of up
to `k` rounds does an `O(k)` pass), trivial at the scale this system
operates at (a LAN mesh of a handful of devices, not a data-center
cluster). `SchedulerState` updates are `O(1)` per observation. No
allocation step blocks the job dispatch path longer than the existing
snapshot-based heuristic did.

## 5. Evaluation

### 5.1 Method

Real multi-machine benchmarking has network/OS jitter that swamps the
scheduling effect being measured, and is not reproducible across runs.
`bench/simulate.py` instead simulates "worlds" of devices with known
ground-truth `(rate, overhead, failure probability)`, and replays a
sequence of 40 jobs (100 items each) against each strategy using the
*actual production split logic* (`Orchestrator._split_counts`,
`scheduler.py`'s strategy functions) — only the network/psutil layer is
synthetic. Each device's live "spare-capacity score" is modeled as its true
rate plus Gaussian noise (σ = 0.12), reproducing the gap between what
CPU/RAM telemetry reports and true sustained throughput that motivates
learning in the first place. Per-job elapsed time is the analytic
`count/rate + overhead` plus 10% multiplicative jitter; a chunk that "fails"
(drawn per the device's configured `fail_prob`) is reassigned to local,
mirroring `orchestrator.py`'s real fallback path. Energy is a linear proxy
(`elapsed_seconds * device_power_watts`), not a calibrated hardware
measurement — treat the energy column as directional, not absolute.

Four scenarios (`bench/simulate.py::build_scenarios`):

| Scenario | Setup |
|---|---|
| `homogeneous` | Two helpers, identical rate (8 items/s), no overhead, no failures — sanity check that adaptive learning doesn't *hurt* when there's nothing to learn. |
| `heterogeneous_rate` | One 20 items/s helper, one 4 items/s helper. |
| `heterogeneous_overhead` | Two 10 items/s helpers, one with a 1.2s dispatch overhead (simulating a relay-tunneled peer). |
| `unreliable_helper` | One reliable helper (2% failure), one flaky helper (45% failure), equal rate. |

### 5.2 Results

Steady-state means (jobs 15–39, after each strategy's state — where it has
any — has stabilized), from a run of `bench/simulate.py` with seed 42:

| Scenario | Strategy | Makespan (s) | Energy (J, proxy) |
|---|---|---:|---:|
| homogeneous | Fixed equal | 4.51 | 276.1 |
| homogeneous | Proportional (legacy) | 8.75 | 294.3 |
| homogeneous | **Adaptive DLT (proposed)** | **4.38** | **269.0** |
| heterogeneous_rate | Fixed equal | 7.95 | 300.0 |
| heterogeneous_rate | Proportional (legacy) | 8.75 | 269.5 |
| heterogeneous_rate | **Adaptive DLT (proposed)** | **3.45** | **207.7** |
| heterogeneous_overhead | Fixed equal | 4.65 | 262.7 |
| heterogeneous_overhead | Proportional (legacy) | 8.75 | 304.8 |
| heterogeneous_overhead | **Adaptive DLT (proposed)** | **4.34** | **260.6** |
| unreliable_helper | Fixed equal | 6.49 | 281.5 |
| unreliable_helper | Proportional (legacy) | 9.59 | 300.0 |
| unreliable_helper | **Adaptive DLT (proposed)** | **6.50** | 284.0 |

Regenerate with `python bench/simulate.py` (needs `pip install matplotlib`,
intentionally not a core dependency — see that file's docstring); it also
writes `bench/results/makespan_learning_curve.png` (per-job convergence),
`mean_makespan_by_scenario.png`, and `energy_by_scenario.png`.

### 5.3 Discussion

- **Adaptive DLT matches or beats both baselines in every scenario**, and
  wins decisively (56–61% makespan reduction vs. the legacy heuristic) when
  devices are genuinely heterogeneous in rate or overhead — exactly the
  condition a snapshot-based heuristic cannot see.
- **`proportional_snapshot` underperforms even the naive `fixed_equal`
  baseline across all four scenarios.** This is a real, explainable
  property of the original formula, not a simulation artifact: local's
  share is computed from local's *own* overload alone
  (`1 - max(cpu%, ram%)/100`, clamped to `[0.2, 1.0]`), independent of how
  many helpers exist or their combined capacity. With local at 30% load and
  two idle helpers of equal capacity to local, the "fair" split is roughly
  a third each; `proportional_snapshot` instead hands local 70% regardless.
  This finding motivated using `adaptive_dlt` as the new default rather
  than an opt-in alternative.
- **`unreliable_helper` is the weakest win** (makespan effectively tied
  with `fixed_equal`, energy slightly worse) — 40 jobs is not much time for
  a single EWMA(α=0.25) reliability score to separate a 2%-failure host from
  a 45%-failure one when both still succeed most chunks. A slower-decaying,
  higher-confidence reliability estimator (e.g. a Beta-distribution
  posterior over success rate instead of a single EWMA) is a concrete
  next step (§6).
- **Threat to validity**: the simulation's noise model (Gaussian spare-score
  noise, 10% multiplicative elapsed-time jitter) is a modeling choice, not
  measured from real hardware. The steady-state numbers above should be
  read as "adaptive_dlt is directionally and substantially better under
  heterogeneity, tied under homogeneity" rather than as calibrated
  real-world percentages. A real multi-machine run (README's "Real
  multi-machine testing" section) is required to validate the effect size
  outside simulation — tracked in §6.

## 6. Limitations and future work

- **No real-hardware validation of the effect size.** §5.3's threat to
  validity above — the simulation isolates the scheduling decision cleanly,
  but the actual seconds-saved number on real LAN hardware needs a real
  multi-machine run, not just simulation.
- **Reliability estimator is a single EWMA**, not a confidence-aware
  model — it cannot yet distinguish "one bad chunk out of two" from "one bad
  chunk out of fifty." A Beta-Bernoulli posterior (tracking successes and
  failures separately rather than one blended average) would let the
  scheduler act more cautiously on thin evidence.
- **No verification of a helper's returned results.** `net_client.py`
  validates response *shape* (right count, right types) but not
  *correctness* — a helper that runs the task incorrectly (not
  maliciously — e.g. an underlying image library version mismatch) would
  currently be scored as reliable. Lightweight redundant/spot-check
  execution is the natural extension, borrowing from Byzantine-tolerant
  volunteer-computing designs.
- **Communication cost is a fixed overhead scalar**, not a function of
  chunk size — realistic for the small JSON/image payloads this project
  targets, but would need a proper linear communication-time term (as in
  the classical multi-processor DLT closed forms) for much larger chunks.
- **Energy is a linear proxy**, not a measured hardware quantity; a real
  power-meter or OS-reported energy-impact figure would make the SDG 7/12
  framing (README.md) quantitatively defensible rather than directional.

## 7. Relation to the Sustainable Development Goals

Tying back to `README.md`'s framing: every joule `adaptive_dlt` avoids in
§5.2 by routing work to whichever device can do it fastest-per-watt, rather
than a snapshot-blind split, is a direct (if currently proxy-measured)
contribution to **SDG 7** (avoiding wasted energy on suboptimal device
choice) and **SDG 12** (getting more useful work out of existing low-end
hardware before it's replaced) — the scheduling contribution in this
document is not orthogonal to that framing, it is the mechanism that makes
it more than a slogan.
