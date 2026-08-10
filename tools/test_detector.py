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
_MODULE_PATH = (
    HERE.parent / "custom_components" / "difluid_microbalance" / "brew_detect.py"
)
_spec = importlib.util.spec_from_file_location("brew_detect", _MODULE_PATH)
brew_detect = importlib.util.module_from_spec(_spec)
sys.modules["brew_detect"] = brew_detect
_spec.loader.exec_module(brew_detect)


def run(csv_path: Path):
    """Feed a fixture through the detector, returning (labelled plateaus, pairs)."""
    detector = brew_detect.BrewDetector()
    pairer = brew_detect.BrewPairer()
    events, pairs = [], []

    with csv_path.open(newline="") as fh:
        rows = sorted(csv.DictReader(fh), key=lambda r: r["ts"])

    def absorb(plateau):
        if plateau is None:
            return
        kind, pair = pairer.offer(plateau)
        events.append((kind, round(plateau.value, 1)))
        if pair is not None:
            pairs.append(pair)

    for row in rows:
        if row["state"] in ("unavailable", "unknown", ""):
            absorb(detector.flush())
            detector.reset()
            continue
        absorb(
            detector.feed(
                brew_detect.Sample(
                    t=datetime.fromisoformat(row["ts"]).timestamp(),
                    weight=float(row["state"]),
                )
            )
        )
    absorb(detector.flush())
    return events, pairs


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
    ok &= check("beans of episode B detected at 18.2 g", ("dose", 18.2) in events)
    ok &= check(
        "pour of episode C detected near 37 g despite its 5 s plateau",
        any(k == "yield" and 36.5 <= v <= 38.0 for k, v in events),
    )
    ok &= check(
        "lift-off transients (1355 g, 1282 g, -319 g) produce no plateau",
        all(abs(v) < 100 for v in masses),
        f"non-other masses {masses}",
    )
    # Deliberately NOT asserting that only one plateau is labelled "dose". The
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
            "pairs the 18.2 g beans (held ~3 min), not the 17.9 g placement (16 s)",
            abs(pair.dose - 18.2) < 0.05,
            f"dose {pair.dose}",
        )
        ok &= check("yield 37.5 g", abs(pair.yield_g - 37.5) < 0.05, f"{pair.yield_g}")
        ok &= check("ratio about 1:2.06", abs(pair.ratio - 2.06) < 0.02, f"{pair.ratio:.3f}")

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
            "pair is dose 18.0 g / yield 36.7 g, ratio about 1:2.04",
            abs(p.dose - 18.0) < 0.05 and abs(p.yield_g - 36.7) < 0.1
            and abs(p.ratio - 2.04) < 0.02,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )

    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
