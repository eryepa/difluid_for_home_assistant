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
        # 36.7, not the 36.6 this expected while replay resampled at 1 Hz. One scale
        # increment, and the finer stream is the honest one: at 5 Hz the detector
        # sees the sample production saw. The value moved because the harness was
        # corrected, not because the detector changed.
        ok &= check(
            "pair is dose 18.0 g / yield 36.7 g, ratio about 1:2.04",
            abs(p.dose - 18.0) < 0.05 and abs(p.yield_g - 36.7) < 0.1
            and abs(p.ratio - 2.04) < 0.02,
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

    print("\nsnatched.csv — the 2026-08-13 19:15 shot whose cup was lifted at once")
    events, pairs, weighings = run(HERE / "testdata" / "snatched.csv", detail=True)
    ok &= check(
        "the pour is reported even though it never held for stable_seconds",
        any(k == "yield" and 37.2 <= v <= 37.7 for k, v in events),
        str(events),
    )
    ok &= check("the 18.2 g dose is recognised", ("dose", 18.2) in events)
    ok &= check("one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        p = pairs[0]
        ok &= check(
            "pair is dose 18.2 g / yield ~37.5 g, ratio about 1:2.06",
            abs(p.dose - 18.2) < 0.05 and abs(p.yield_g - 37.5) < 0.2
            and abs(p.ratio - 2.06) < 0.03,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )
    ok &= check(
        "the portafilter sitting at -35 g is not an event",
        all(v > 0 for _, v in events),
        str(events),
    )

    print("\ntare.csv — the 2026-08-14 11:19 empty cup tared to zero")
    events, pairs, weighings = run(HERE / "testdata" / "tare.csv", detail=True)
    ok &= check(
        "the tared cup is not a weighing — 64 g must not appear at all",
        not any(60.0 <= v <= 70.0 for _, v in events),
        str(events),
    )
    ok &= check("one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        p = pairs[0]
        ok &= check(
            "pair is the real shot: dose 18.1 g / yield 37.7 g, ratio about 1:2.08",
            abs(p.dose - 18.1) < 0.1 and abs(p.yield_g - 37.7) < 0.2
            and abs(p.ratio - 2.08) < 0.03,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )
    ok &= check(
        "the real removals either side of the tare still end their weighings",
        any(k == "dose" and abs(v - 18.1) < 0.1 for k, v in events),
        str(events),
    )

    print("\ntare_still.csv — the 2026-08-15 11:16 tare of a dead-still cup")
    events, pairs, weighings = run(HERE / "testdata" / "tare_still.csv", detail=True)
    ok &= check(
        "the tared cup is not a weighing — 42.8 g must not appear at all",
        not any(42.0 <= v <= 43.5 for _, v in events),
        str(events),
    )
    ok &= check("one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        p = pairs[0]
        ok &= check(
            "pair is the real shot: dose 18.2 g / yield 53.5 g, ratio about 1:2.94",
            abs(p.dose - 18.2) < 0.1 and abs(p.yield_g - 53.5) < 0.2
            and abs(p.ratio - 2.94) < 0.03,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )
    ok &= check(
        "the portafilter coming off at -35 g still ends the dose weighing",
        any(k == "dose" and abs(v - 18.2) < 0.1 for k, v in events),
        str(events),
    )

    print("\ntwo_doses.csv — the 2026-08-17 cycle with two candidates for the dose")
    events, pairs, weighings = run(HERE / "testdata" / "two_doses.csv", detail=True)
    ok &= check(
        "both candidates are detected — 18.0 g ground, 19.3 g set back down",
        ("dose", 18.0) in events and ("dose", 19.3) in events,
        str(events),
    )
    # Asserted on the pairer's choice rather than on a finished pair, because this
    # capture's pour is not detected offline at all.  It crept from 59.8 g to 60.2 g
    # in the last half second as the crema settled, which is neither a full plateau
    # nor a second of quiet, so it lands exactly on the stability boundary — and
    # production, whose 5 Hz jitter put another sample or two at 59.8 inside the
    # window, did detect it and reported 59.8 g.  Replay reconstructs holds on an
    # even 0.2 s grid and cannot reproduce that jitter, so the two disagree here.
    # Left as it is deliberately: moving a threshold to make this fixture pair would
    # be tuning to the harness again, which is the mistake that cost three betas.
    pairer = brew_detect.BrewPairer()
    for w in weighings:
        pairer.offer(w)
    chosen = pairer.pending_dose
    ok &= check(
        "the pairer holds the beans that were ground on, not the holder put back down",
        chosen is not None and abs(chosen.value - 18.0) < 0.1,
        f"chose {chosen.value if chosen else None}",
    )
    # The margin, not just the answer.  Hold time separated these two by 1.3 s and
    # would flip the moment the holder sat on the scale a little longer; how the
    # load arrived separates them by 5.4 s.
    doses = [w for w in weighings if 17.0 <= w.value <= 20.0]
    ok &= check(
        "the two are told apart by rise, not by a hair of hold time",
        len(doses) == 2 and abs(doses[0].rise_seconds - doses[1].rise_seconds) > 4.0
        and abs(doses[0].duration - doses[1].duration) < 6.0,
        f"rise {[round(w.rise_seconds, 1) for w in doses]} "
        f"hold {[round(w.duration, 1) for w in doses]}",
    )
    ok &= check(
        "the 65 g cup tared at 19:54:41 is still not a weighing",
        not any(60.0 <= v <= 70.0 for _, v in events),
        str(events),
    )

    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
