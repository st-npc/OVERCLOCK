"""Scripted checkpoints for scheduler.py's Divisible Load allocation and its
EWMA learning layers.

Run directly: `python tests/test_scheduler.py`
No pytest dependency, same reasoning as the other tests/ scripts: plain
functions + assertions so this stays runnable with nothing beyond
requirements.txt.

Covers:
  1. Shares from every strategy always sum to 1.0 (a broken normalization
     here would silently shrink or inflate a job's total split).
  2. solve_dlt_shares gives two equal-rate, equal-overhead devices an equal
     split, and a faster device a proportionally larger one.
  3. The local-share floor (MIN_LOCAL_SHARE) is enforced even when DLT's
     unconstrained solution would starve local completely.
  4. A device with per-item overhead larger than the deadline is correctly
     priced out (gets 0), not given a negative/nonsensical share.
  5. SchedulerState cold-starts new devices from the spare-capacity
     bootstrap, then converges its learned rate toward repeated real
     observations, and discounts a repeatedly-failing device's reliability.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


LOCAL_IDLE = {"cpu_percent": 10.0, "ram_percent": 10.0}
LOCAL_BUSY = {"cpu_percent": 95.0, "ram_percent": 90.0}


def test_shares_sum_to_one_every_strategy():
    print("\n[1] Every strategy's shares sum to 1.0")
    helpers = [("h1:5001", 0.8), ("h2:5002", 0.4)]
    for strategy_fn in (scheduler.fixed_equal, scheduler.proportional_snapshot):
        shares = strategy_fn(LOCAL_IDLE, helpers)
        check(f"{strategy_fn.__name__} sums to 1.0", approx(sum(shares.values()), 1.0), shares)

    state = scheduler.SchedulerState()
    shares = state.adaptive_dlt(LOCAL_IDLE, helpers)
    check("adaptive_dlt (cold start) sums to 1.0", approx(sum(shares.values()), 1.0), shares)

    shares_no_helpers = state.adaptive_dlt(LOCAL_IDLE, [])
    check("adaptive_dlt with zero helpers sums to 1.0", approx(sum(shares_no_helpers.values()), 1.0), shares_no_helpers)
    check("adaptive_dlt with zero helpers gives local everything", approx(shares_no_helpers["local"], 1.0), shares_no_helpers)


def test_dlt_equalizes_equal_devices():
    print("\n[2] DLT gives equal-capacity devices an equal split")
    capacities = {"a": (10.0, 0.0), "b": (10.0, 0.0)}
    shares = scheduler.solve_dlt_shares(capacities)
    check("equal rates -> equal shares", approx(shares["a"], shares["b"], tol=1e-9), shares)

    faster = {"a": (20.0, 0.0), "b": (10.0, 0.0)}
    shares2 = scheduler.solve_dlt_shares(faster)
    check("2x rate -> ~2x share", approx(shares2["a"] / shares2["b"], 2.0, tol=1e-6), shares2)
    check("faster+slower still sums to 1.0", approx(sum(shares2.values()), 1.0), shares2)


def test_local_floor_enforced():
    print("\n[3] Local floor holds even when unconstrained DLT would starve it")
    # Local is far slower than the helper; unconstrained DLT would give it
    # a tiny sliver. The floor must still guarantee MIN_LOCAL_SHARE.
    capacities = {"local": (0.5, 0.0), "helper": (100.0, 0.0)}
    shares = scheduler.solve_dlt_shares(capacities, min_shares={"local": scheduler.MIN_LOCAL_SHARE})
    check(
        f"local share >= floor ({scheduler.MIN_LOCAL_SHARE})",
        shares["local"] >= scheduler.MIN_LOCAL_SHARE - 1e-9,
        shares,
    )
    check("shares still sum to 1.0 after floor enforcement", approx(sum(shares.values()), 1.0), shares)


def test_high_overhead_device_priced_out():
    print("\n[4] A device whose overhead alone exceeds the deadline gets 0, not negative")
    # A very slow relay-style device with huge fixed overhead relative to a
    # fast local device should be dropped from the active set entirely.
    capacities = {"local": (50.0, 0.0), "slow_relay": (0.2, 5.0)}
    shares = scheduler.solve_dlt_shares(capacities)
    check("priced-out device gets exactly 0.0, never negative", shares["slow_relay"] == 0.0, shares)
    check("remaining device absorbs the whole job", approx(shares["local"], 1.0), shares)


def test_scheduler_state_learns():
    print("\n[5] SchedulerState learns rate/reliability from observations")
    state = scheduler.SchedulerState()
    before = state.snapshot()
    check("no history before any observation", before == {}, before)

    # Repeated fast, successful chunks should pull the learned rate toward
    # the true observed rate (10 items in 1s each time).
    for _ in range(20):
        state.record_chunk_result("fast_helper", success=True, count=10, elapsed=1.0)
    learned = state.snapshot()["fast_helper"]["learned_rate_items_per_sec"]
    check(f"learned rate converges near 10.0 items/sec (got {learned})", approx(learned, 10.0, tol=0.5))

    # A device that starts failing should have its reliability decay well
    # below 1.0, even though its rate history was fine before.
    for _ in range(10):
        state.record_chunk_result("flaky_helper", success=False)
    reliability = state.snapshot()["flaky_helper"]["reliability"]
    check(f"reliability decays after repeated failures (got {reliability})", reliability < 0.3, reliability)

    # A cold device (no observations at all) must still get a nonzero
    # effective capacity from its spare-score, i.e. it can still be
    # scheduled work on its very first job.
    helpers = [("cold_helper", 0.9)]
    shares = state.adaptive_dlt(LOCAL_IDLE, helpers)
    check("a never-seen-before helper still gets scheduled work", shares.get("cold_helper", 0.0) > 0.0, shares)


if __name__ == "__main__":
    test_shares_sum_to_one_every_strategy()
    test_dlt_equalizes_equal_devices()
    test_local_floor_enforced()
    test_high_overhead_device_priced_out()
    test_scheduler_state_learns()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
