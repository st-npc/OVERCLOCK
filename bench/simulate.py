"""Offline evaluation harness for scheduler.py's strategies.

Spinning up real Flask helper processes to compare scheduling strategies
would be slow, noisy (real OS scheduling jitter dwarfs the effect we're
trying to measure), and hard to reproduce. Instead this module simulates a
"world" of devices with known ground-truth (rate, dispatch overhead,
per-chunk failure probability), and replays a sequence of jobs against each
strategy using the *actual* production split logic
(`Orchestrator._split_counts`, `scheduler.py`'s strategy functions) — only
the network/psutil layer is synthetic. This is standard practice in
scheduling-theory evaluation (see docs/RESEARCH.md's Evaluation section):
a controlled simulation isolates the scheduling *decision* from real-world
network/OS noise, at the cost of not exercising the real HTTP stack (which
tests/test_fallback.py and a real multi-machine run already cover).

Run: `python bench/simulate.py` (from the repo root, or anywhere — it fixes
up sys.path itself). Writes bench/results/summary.csv and three PNG charts.
Needs matplotlib, which is intentionally NOT in requirements.txt (the app
itself stays dependency-light) — install it separately to run this:
`pip install matplotlib`.
"""
import csv
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler
from orchestrator import Orchestrator

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
STRATEGIES = ("fixed_equal", "proportional_snapshot", "adaptive_dlt")
STRATEGY_LABELS = {
    "fixed_equal": "Fixed equal",
    "proportional_snapshot": "Proportional (legacy)",
    "adaptive_dlt": "Adaptive DLT (proposed)",
}
N_JOBS_PER_WORLD = 40
JOB_SIZE = 100          # items per job
STEADY_STATE_FROM = 15  # jobs before this index are "warm-up", excluded from steady-state means
LOCAL_RATE = 8.0        # items/sec, this device
LOCAL_POWER_W = 25.0    # rough laptop-under-load draw, for the energy proxy
HELPER_POWER_W = 20.0


class Device:
    def __init__(self, name, true_rate, overhead, fail_prob, power_w):
        self.name = name
        self.true_rate = true_rate       # items/sec, ground truth
        self.overhead = overhead         # seconds, ground truth dispatch/RTT cost
        self.fail_prob = fail_prob       # probability a chunk sent to this device fails
        self.power_w = power_w

    def spare_score_proxy(self, rng, rate_scale):
        """A noisy stand-in for what device_monitor.py's live CPU/RAM-derived
        spare_score would report: correlated with true_rate but not equal to
        it — exactly the gap adaptive_dlt's learning is meant to close."""
        noisy = self.true_rate / rate_scale + rng.gauss(0, 0.12)
        return max(0.0, min(1.0, noisy))


def _local_snapshot_for(overload_fraction: float) -> dict:
    pct = overload_fraction * 100.0
    return {"cpu_percent": pct, "ram_percent": pct}


def run_world(rng, helpers, local_overload, rate_scale):
    """Run N_JOBS_PER_WORLD jobs for every strategy against the same
    `helpers` ground truth, returning a list of per-job result dicts."""
    rows = []
    for strategy in STRATEGIES:
        state = scheduler.SchedulerState()  # fresh learned state per strategy per world
        for job_index in range(N_JOBS_PER_WORLD):
            local_snap = _local_snapshot_for(local_overload)
            reachable = [(h.name, h.spare_score_proxy(rng, rate_scale)) for h in helpers]

            if strategy == "adaptive_dlt":
                shares = state.adaptive_dlt(local_snap, reachable)
            else:
                shares = state.compute_shares(strategy, local_snap, reachable)

            counts = Orchestrator._split_counts(JOB_SIZE, shares)

            finish_times = {}
            energy = 0.0
            local_extra_seconds = 0.0  # fallback work that lands on local

            for h in helpers:
                count = counts.get(h.name, 0)
                if count <= 0:
                    continue
                base_elapsed = count / h.true_rate + h.overhead
                jittered = max(0.01, rng.gauss(base_elapsed, base_elapsed * 0.1))
                failed = rng.random() < h.fail_prob
                if failed:
                    state.record_chunk_result(h.name, success=False)
                    # Fallback: same guarantee orchestrator.py enforces —
                    # the chunk is reassigned to local and still completes.
                    local_extra_seconds += count / LOCAL_RATE
                    energy += count / LOCAL_RATE * LOCAL_POWER_W
                else:
                    state.record_chunk_result(h.name, success=True, count=count, elapsed=jittered)
                    finish_times[h.name] = jittered
                    energy += jittered * h.power_w

            local_count = counts.get("local", 0)
            local_elapsed = local_count / LOCAL_RATE
            local_total = local_elapsed + local_extra_seconds  # local runs its own chunk, then any fallbacks
            finish_times["local"] = local_total
            energy += local_elapsed * LOCAL_POWER_W
            if local_count > 0:
                # orchestrator.py feeds back every target's real elapsed
                # time, "local" included — the sim must mirror that, or
                # adaptive_dlt never learns local's true rate and instead
                # stays on the cold-start bootstrap forever.
                state.record_chunk_result("local", success=True, count=local_count, elapsed=local_elapsed)

            makespan = max(finish_times.values()) if finish_times else 0.0
            rows.append(
                {
                    "strategy": strategy,
                    "job_index": job_index,
                    "makespan_seconds": round(makespan, 4),
                    "energy_joules": round(energy, 4),
                }
            )
    return rows


def build_scenarios():
    """Four scenarios spanning the conditions the write-up discusses:
    homogeneous baseline, heterogeneous rates, heterogeneous overhead
    (e.g. a relay-connected helper), and an unreliable helper."""
    scenarios = {}

    scenarios["homogeneous"] = [
        Device("helper_a", true_rate=8.0, overhead=0.05, fail_prob=0.0, power_w=HELPER_POWER_W),
        Device("helper_b", true_rate=8.0, overhead=0.05, fail_prob=0.0, power_w=HELPER_POWER_W),
    ]
    scenarios["heterogeneous_rate"] = [
        Device("fast_helper", true_rate=20.0, overhead=0.05, fail_prob=0.0, power_w=HELPER_POWER_W),
        Device("slow_helper", true_rate=4.0, overhead=0.05, fail_prob=0.0, power_w=HELPER_POWER_W),
    ]
    scenarios["heterogeneous_overhead"] = [
        Device("lan_helper", true_rate=10.0, overhead=0.05, fail_prob=0.0, power_w=HELPER_POWER_W),
        Device("relay_helper", true_rate=10.0, overhead=1.2, fail_prob=0.0, power_w=HELPER_POWER_W),
    ]
    scenarios["unreliable_helper"] = [
        Device("stable_helper", true_rate=8.0, overhead=0.05, fail_prob=0.02, power_w=HELPER_POWER_W),
        Device("flaky_helper", true_rate=8.0, overhead=0.05, fail_prob=0.45, power_w=HELPER_POWER_W),
    ]
    return scenarios


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    rng = random.Random(42)
    all_rows = []

    for scenario_name, helpers in build_scenarios().items():
        rate_scale = max(h.true_rate for h in helpers) * 1.2
        rows = run_world(rng, helpers, local_overload=0.3, rate_scale=rate_scale)
        for r in rows:
            r["scenario"] = scenario_name
        all_rows.extend(rows)
        print(f"simulated scenario '{scenario_name}' ({len(helpers)} helpers, {N_JOBS_PER_WORLD} jobs x {len(STRATEGIES)} strategies)")

    csv_path = os.path.join(RESULTS_DIR, "summary.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["scenario", "strategy", "job_index", "makespan_seconds", "energy_joules"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"wrote {csv_path} ({len(all_rows)} rows)")

    _print_steady_state_table(all_rows)
    _plot(all_rows)


def _print_steady_state_table(rows):
    print("\nSteady-state means (jobs "
          f"{STEADY_STATE_FROM}-{N_JOBS_PER_WORLD - 1}, lower makespan/energy is better):\n")
    scenarios = sorted(set(r["scenario"] for r in rows))
    header = f"  {'scenario':<22}{'strategy':<24}{'makespan (s)':<15}{'energy (J)':<12}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for scenario in scenarios:
        for strategy in STRATEGIES:
            subset = [r for r in rows if r["scenario"] == scenario and r["strategy"] == strategy
                      and r["job_index"] >= STEADY_STATE_FROM]
            mean_makespan = sum(r["makespan_seconds"] for r in subset) / len(subset)
            mean_energy = sum(r["energy_joules"] for r in subset) / len(subset)
            print(f"  {scenario:<22}{STRATEGY_LABELS[strategy]:<24}{mean_makespan:<15.2f}{mean_energy:<12.1f}")


def _plot(rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    scenarios = sorted(set(r["scenario"] for r in rows))
    colors = {"fixed_equal": "#B8B2CC", "proportional_snapshot": "#8B5CF6", "adaptive_dlt": "#22D3C5"}

    # 1. Learning curve: makespan per job index, one subplot per scenario.
    fig, axes = plt.subplots(1, len(scenarios), figsize=(5 * len(scenarios), 4), sharey=False)
    if len(scenarios) == 1:
        axes = [axes]
    for ax, scenario in zip(axes, scenarios):
        for strategy in STRATEGIES:
            subset = [r for r in rows if r["scenario"] == scenario and r["strategy"] == strategy]
            subset.sort(key=lambda r: r["job_index"])
            ax.plot(
                [r["job_index"] for r in subset],
                [r["makespan_seconds"] for r in subset],
                label=STRATEGY_LABELS[strategy],
                color=colors[strategy],
                linewidth=1.5,
            )
        ax.set_title(scenario.replace("_", " "))
        ax.set_xlabel("job index")
        ax.set_ylabel("makespan (s)")
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS_DIR, "makespan_learning_curve.png"), dpi=150)
    plt.close(fig)

    # 2. Steady-state mean makespan, grouped bar chart by scenario.
    _grouped_bar(rows, "makespan_seconds", "Mean steady-state makespan (s) — lower is better",
                 os.path.join(RESULTS_DIR, "mean_makespan_by_scenario.png"), colors)

    # 3. Steady-state mean energy, grouped bar chart by scenario.
    _grouped_bar(rows, "energy_joules", "Mean steady-state energy (J) — lower is better",
                 os.path.join(RESULTS_DIR, "energy_by_scenario.png"), colors)

    print(f"wrote 3 PNG charts to {RESULTS_DIR}/")


def _grouped_bar(rows, field, title, out_path, colors):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    scenarios = sorted(set(r["scenario"] for r in rows))
    x = np.arange(len(scenarios))
    width = 0.25

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for i, strategy in enumerate(STRATEGIES):
        means = []
        for scenario in scenarios:
            subset = [r[field] for r in rows if r["scenario"] == scenario and r["strategy"] == strategy
                      and r["job_index"] >= STEADY_STATE_FROM]
            means.append(sum(subset) / len(subset))
        ax.bar(x + (i - 1) * width, means, width, label=STRATEGY_LABELS[strategy], color=colors[strategy])

    ax.set_xticks(x)
    ax.set_xticklabels([s.replace("_", "\n") for s in scenarios], fontsize=8)
    ax.set_title(title)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
