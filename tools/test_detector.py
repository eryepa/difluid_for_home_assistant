#!/usr/bin/env python3
"""Regression test for brew_detect against recorded ground truth.

Run with no arguments:  ./test_detector.py
Exits non-zero on failure, so it works as a pre-commit / CI gate.

The expectations encode what a human reading the recorder history concluded, so a
threshold change that breaks one of them is a real behaviour change, not noise.
"""

from __future__ import annotations

import csv
import importlib.util
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Go through replay_brew so the test exercises the same stream reconstruction the
# replay tool uses; it also owns loading brew_detect without pulling in HA.
sys.path.insert(0, str(HERE))
import replay_brew  # noqa: E402

brew_detect = replay_brew.brew_detect


def run(csv_path: Path, detail: bool = False):
    """Feed a fixture through the detector.

    Returns ``(events, pairs)``, or ``(events, pairs, weighings)`` with ``detail``
    for the assertions that need to look at a weighing's steps.
    """
    detector = brew_detect.BrewDetector()
    pairer = brew_detect.BrewPairer()
    events, pairs, weighings = [], [], []

    with csv_path.open(newline="") as fh:
        rows = sorted(csv.DictReader(fh), key=lambda r: r["ts"])

    def absorb(plateau):
        if plateau is None:
            return
        kind, pair = pairer.offer(plateau)
        events.append((kind, round(plateau.value, 1)))
        weighings.append(plateau)
        if pair is not None:
            pairs.append(pair)

    for ts, weight in replay_brew.zero_order_hold(replay_brew.weight_readings(rows)):
        if weight is None:
            absorb(detector.flush())
            detector.reset()
            continue
        absorb(detector.feed(brew_detect.Sample(t=ts, weight=weight)))
    absorb(detector.flush())
    return (events, pairs, weighings) if detail else (events, pairs)


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    return condition


def main() -> int:
    subprocess.run(
        [sys.executable, str(HERE / "testdata" / "make_fixture.py")],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    ok = True

    print("known_events.csv — real recorder data, detection only")
    events, pairs = run(HERE / "testdata" / "known_events.csv")
    masses = [v for kind, v in events if kind != "other"]

    ok &= check(
        "pour of episode A detected at 37.5 g",
        ("yield", 37.5) in events,
        str(events),
    )
    # 18.4, not the 18.2 an earlier version of this test expected. The beans drift
    # upward over the three minutes they sit there, and replay used to be cut short
    # at 19:43:32 by gap_reset because held values leave no rows in the recorder.
    # Reconstructing the stream reads the whole hold, which is what production saw
    # all along — the old expectation measured the replay harness, not the detector.
    ok &= check("beans of episode B detected at 18.4 g", ("dose", 18.4) in events)
    ok &= check(
        "pour of episode C detected near 37 g despite its 5 s plateau",
        any(k == "yield" and 36.5 <= v <= 38.0 for k, v in events),
    )
    ok &= check(
        "lift-off transients (1355 g, 1282 g, -319 g) produce no weighing",
        all(abs(v) < 100 for v in masses),
        f"non-other masses {masses}",
    )
    ok &= check(
        "the portafilter parked at -35 g is not a weighing",
        all(v > 0 for _, v in events),
        str([v for _, v in events]),
    )
    # Deliberately NOT asserting that only one weighing is labelled "dose". The
    # 17.9 g placement at 19:46 really is held for 15.7 s and really is in the dose
    # mass range — an earlier version of this test claimed otherwise, but that rested
    # on a duration bug that under-measured holds in replay. What must hold is that
    # it does not win the pairing; that is asserted on synthetic_pair.csv below.
    ok &= check(
        "no pair claimed — this capture contains none within the window",
        not pairs,
    )

    print("\nsynthetic_pair.csv — same waveforms re-timed into one brew cycle")
    events, pairs = run(HERE / "testdata" / "synthetic_pair.csv")
    ok &= check("exactly one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        pair = pairs[0]
        ok &= check(
            "pairs the beans (held ~3 min), not the 17.9 g placement (17 s)",
            abs(pair.dose - 18.4) < 0.05,
            f"dose {pair.dose}",
        )
        ok &= check("yield 37.5 g", abs(pair.yield_g - 37.5) < 0.05, f"{pair.yield_g}")
        ok &= check("ratio about 1:2.04", abs(pair.ratio - 2.04) < 0.02, f"{pair.ratio:.3f}")

    print("\nlost_shot.csv — the real 2026-08-10 19:47 cup that went unreported")
    events, pairs = run(HERE / "testdata" / "lost_shot.csv")
    ok &= check(
        "the pour survives the lift-off transient and the BLE drop",
        any(k == "yield" and 36.0 <= v <= 37.5 for k, v in events),
        str(events),
    )
    ok &= check("the 18.0 g dose is recognised", ("dose", 18.0) in events)
    ok &= check("one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        p = pairs[0]
        ok &= check(
            "pair is dose 18.0 g / yield 36.6 g, ratio about 1:2.03",
            abs(p.dose - 18.0) < 0.05 and abs(p.yield_g - 36.6) < 0.1
            and abs(p.ratio - 2.03) < 0.02,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )

    print("\nporridge.csv — 30 g of oats poured from two packets, then milk and water")
    events, pairs, weighings = run(HERE / "testdata" / "porridge.csv", detail=True)
    ok &= check(
        "two weighings, not four",
        len(events) == 2,
        str(events),
    )
    ok &= check(
        "the oats read 30.0 g — what was in the bowl, not the 23.7 g the first "
        "packet ran out at",
        any(abs(v - 30.0) < 0.15 for _, v in events),
        str(events),
    )
    ok &= check(
        "23.7 g is reported as a step of that weighing, not as an event of its own",
        any(
            abs(w.value - 30.0) < 0.15 and any(abs(s - 23.7) < 0.15 for s in w.steps)
            for w in weighings
        ),
        str([w.steps for w in weighings]),
    )
    ok &= check(
        "milk topped up with water reads 111.4 g, not 68.7 g",
        any(abs(v - 111.4) < 0.15 for _, v in events),
        str(events),
    )
    ok &= check(
        "no brew reported — a bowl of porridge is not a cup of coffee",
        not pairs,
        str(pairs),
    )

    print("\nsettling.csv — the 2026-08-10 15:37 pour that settled twice")
    events, pairs, weighings = run(HERE / "testdata" / "settling.csv", detail=True)
    ok &= check("one weighing, not two", len(events) == 1, str(events))
    ok &= check(
        "reports the settled 38.4 g, not the 38.1 g reached four seconds earlier",
        any(k == "yield" and abs(v - 38.4) < 0.15 for k, v in events),
        str(events),
    )

    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
