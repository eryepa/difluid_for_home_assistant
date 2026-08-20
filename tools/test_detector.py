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

#: State value marking the moment the BLE proxy went quiet.  See _stream below.
GAP_MARKER = "gap"


def _stream(rows: list[dict]):
    """Yield ``(t, weight)`` for the detector, honouring explicit stream gaps.

    ``zero_order_hold`` exists to undo the recorder's change-point-only storage, and
    it cannot tell a held value from a dead link — both are silence — so it fills
    across every hole it sees.  That is right for replaying real history and it is
    exactly what makes a stall impossible to write down as ordinary rows: fill across
    the stall and the detector never sees the discontinuity that production saw.

    A ``gap`` row says "the stream stopped here and nothing was observed until the
    next row".  The segments either side are reconstructed independently, so no
    filler is invented across the hole and ``feed`` receives the real jump in
    timestamps.  The detector is *not* reset — a proxy stalling is not a disconnect,
    and deciding what to do about it is the whole of the gap branch under test.

    ``unavailable`` rows are untouched by this and still mean a genuine disconnect.
    """
    segment: list[dict] = []
    for row in rows:
        if row["state"] == GAP_MARKER:
            yield from replay_brew.zero_order_hold(replay_brew.weight_readings(segment))
            segment = []
            continue
        segment.append(row)
    yield from replay_brew.zero_order_hold(replay_brew.weight_readings(segment))


def run(csv_path: Path, detail: bool = False, config=None):
    """Feed a fixture through the detector.

    Returns ``(events, pairs)``, or ``(events, pairs, weighings)`` with ``detail``
    for the assertions that need to look at a weighing's steps.

    ``config`` replays the same fixture under settings other than the shipped ones.
    Every threshold here is exposed through the options flow, so "the defaults happen
    to make this branch unreachable" is a statement about one configuration and not
    about the code; the overlap fixtures below use this to check a rule that at the
    defaults is live only on a single exact mass.
    """
    detector = brew_detect.BrewDetector(config)
    pairer = brew_detect.BrewPairer(config)
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

    # Flow is carried through rather than dropped, and through replay_brew's own
    # helper rather than a second copy of it here.  Production reads weight and flow
    # out of one notification and hands both to the detector; a harness that builds
    # Sample without a flow pins peak_flow to 0.0 for every fixture it will ever run,
    # so the peak_flow tiebreak in classify would be tested as a constant.  That is
    # the ZOH_PERIOD defect again — a quantity the harness holds still while
    # production varies it — and it is the reason the two paths share this code
    # instead of each reconstructing the stream their own way.
    flows = replay_brew.flow_readings(rows)
    for ts, weight, flow in replay_brew.with_flow(_stream(rows), flows):
        if weight is None:
            absorb(detector.flush())
            detector.reset()
            continue
        absorb(detector.feed(brew_detect.Sample(t=ts, weight=weight, flow=flow)))
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
    # Pins *why* it passes, which the assertion above does not.  Here the last drops
    # landed with 3.1 s of quiet left before the lift, so the reading stood for a full
    # stability window and 38.4 is the running median of one ordinary plateau — no
    # step was ever banked and the settle recovery never ran.  topped_up.csv covers
    # the other path, where the lift comes too soon for that.  Without this, either
    # fixture could quietly start passing for the other one's reason.
    settled = weighings[0] if weighings else None
    ok &= check(
        "and it got there as a plateau, not by settle recovery",
        settled is not None
        and settled.final_hold_seconds >= brew_detect.DetectorConfig().stable_seconds
        and len(settled.steps) == 1,
        f"steps {settled.steps} final hold {settled.final_hold_seconds:.2f} s"
        if settled else "no weighing",
    )
    # Measured, not guessed: pulling the lift-off earlier keeps reporting 38.4 down to
    # a shift of 1.5 s and flips to 38.1 at 1.8 s.  That flip is NOT this suite's
    # settle-recovery defect and fixing that defect does not move it — episode F's
    # increment is 0.3 g, exactly one stable_tol, and stable_tol is also the width of
    # the band _settle_from_window walks back through, so the run swallows both levels
    # and its median is 38.1 whatever the gate says.  Doctoring this capture into a
    # tighter fixture would therefore produce one that stays red after a correct fix,
    # which is worse than no fixture.  topped_up.csv uses a 2.4 g top-up instead,
    # where the two levels are separable, and leaves this real capture alone.

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
    # `is not None` first, and not merely to avoid a TypeError.  Both arrivals really
    # were observed in this capture, so an unknown here would mean the detector had
    # stopped seeing the zeros it separates these two by — a failure of exactly the
    # thing being asserted, which should read as FAIL rather than as a traceback.
    rises = [w.rise_seconds for w in doses]
    ok &= check(
        "the two are told apart by rise, not by a hair of hold time",
        len(doses) == 2 and all(r is not None for r in rises)
        and abs(rises[0] - rises[1]) > 4.0
        and abs(doses[0].duration - doses[1].duration) < 6.0,
        f"rise {[r if r is None else round(r, 1) for r in rises]} "
        f"hold {[round(w.duration, 1) for w in doses]}",
    )
    ok &= check(
        "the 65 g cup tared at 19:54:41 is still not a weighing",
        not any(60.0 <= v <= 70.0 for _, v in events),
        str(events),
    )

    print("\ngap_pour.csv — a 20 s proxy stall in the middle of the pour")
    events, pairs, weighings = run(HERE / "testdata" / "gap_pour.csv", detail=True)
    ok &= check(
        "one pour, not two — the 30.0 g fragment is not a weighing of its own",
        not any(k == "yield" and abs(v - 30.0) < 1.0 for k, v in events),
        str(events),
    )
    ok &= check("exactly one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        p = pairs[0]
        ok &= check(
            "pair is dose 18.0 g / yield 37.0 g, ratio about 1:2.06 — not the "
            "1:1.67 the fragment produced",
            abs(p.dose - 18.0) < 0.1 and abs(p.yield_g - 37.0) < 0.2
            and abs(p.ratio - 2.06) < 0.03,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )
    ok &= check(
        "the pre-infusion pause survives as a step of the resumed weighing",
        any(
            abs(w.value - 37.0) < 0.2 and any(abs(s - 30.0) < 0.2 for s in w.steps)
            for w in weighings
        ),
        str([w.steps for w in weighings]),
    )
    ok &= check(
        "the stall left the arrival of the pour unobserved, and that is recorded "
        "as unknown rather than as zero",
        all(w.rise_seconds is None for w in weighings if abs(w.value - 37.0) < 0.2),
        str([(round(w.value, 1), w.rise_seconds) for w in weighings]),
    )

    print("\ngap_dose.csv — a 20 s proxy stall while the dose sits on the scale")
    events, pairs, weighings = run(HERE / "testdata" / "gap_dose.csv", detail=True)
    ok &= check(
        "both candidates are detected — 18.0 g ground on, 19.3 g set back down",
        ("dose", 18.0) in events and ("dose", 19.3) in events,
        str(events),
    )
    candidates = [w for w in weighings if 17.0 <= w.value <= 20.0]
    ok &= check(
        "the stalled dose is one weighing, not two",
        len(candidates) == 2,
        str([round(w.value, 1) for w in candidates]),
    )
    beans = next((w for w in candidates if abs(w.value - 18.0) < 0.15), None)
    holder = next((w for w in candidates if abs(w.value - 19.3) < 0.15), None)
    shown = str([(round(w.value, 1), w.rise_seconds) for w in candidates])
    ok &= check(
        "the beans' arrival is unknown, not zero — nothing in the resumed stream "
        "ever went through zero",
        beans is not None and beans.rise_seconds is None,
        shown,
    )
    ok &= check(
        "the holder's arrival is known and immediate",
        holder is not None
        and holder.rise_seconds is not None
        and holder.rise_seconds < brew_detect.DetectorConfig().dose_min_rise_seconds,
        shown,
    )
    # The margin, and its sign.  What was left of the dose's hold after the stall is
    # shorter than the holder's, so longest-hold gets this wrong — which is what it
    # did.  An unknown arrival must not be scored as a set-down one.
    ok &= check(
        "and it wins despite being held for less time than the holder",
        beans is not None and holder is not None
        and beans.duration < holder.duration,
        f"hold {[(round(w.value, 1), round(w.duration, 1)) for w in candidates]}",
    )
    ok &= check("one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        p = pairs[0]
        ok &= check(
            "pair is the beans: dose 18.0 g / yield 37.0 g, ratio about 1:2.06 — "
            "not the holder's 1:1.92",
            abs(p.dose - 18.0) < 0.1 and abs(p.yield_g - 37.0) < 0.2
            and abs(p.ratio - 2.06) < 0.03,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )

    print("\ntopped_up.csv — the last drops land on a pour that had already settled")
    events, pairs, weighings = run(HERE / "testdata" / "topped_up.csv", detail=True)
    ok &= check(
        "two weighings, not three",
        len(events) == 2,
        str(events),
    )
    ok &= check(
        "reports the 38.4 g the last drops made it, not the 36.0 g banked before them",
        any(k == "yield" and abs(v - 38.4) < 0.15 for k, v in events),
        str(events),
    )
    ok &= check(
        "36.0 g is a step of that weighing, not an event of its own",
        any(
            abs(w.value - 38.4) < 0.15 and any(abs(s - 36.0) < 0.15 for s in w.steps)
            for w in weighings
        ),
        str([w.steps for w in weighings]),
    )
    # Pins the mechanism, not just the answer.  Here the 38.4 never stood for a full
    # stability window and can only have come from the settle recovery; in
    # settling.csv it stood for one and came from an ordinary plateau.  Asserting the
    # value alone would let either fixture pass for the other one's reason.
    pour = next((w for w in weighings if abs(w.value - 38.4) < 0.15), None)
    ok &= check(
        "and it came from the settle recovery — it never stood a full window",
        pour is not None
        and pour.final_hold_seconds < brew_detect.DetectorConfig().stable_seconds,
        f"final hold {pour.final_hold_seconds:.2f} s" if pour else "no 38.4 weighing",
    )
    ok &= check("one pair", len(pairs) == 1, f"{len(pairs)}")
    if pairs:
        p = pairs[0]
        ok &= check(
            "pair is dose 18.0 g / yield 38.4 g, ratio about 1:2.13 — not the "
            "1:2.00 the stale step produced",
            abs(p.dose - 18.0) < 0.1 and abs(p.yield_g - 38.4) < 0.2
            and abs(p.ratio - 2.13) < 0.03,
            f"dose {p.dose} yield {p.yield_g} ratio {p.ratio:.3f}",
        )

    # The overlap tiebreak in classify — `duration >= 30.0 and peak_flow < 0.5` — was
    # until now exercised by nothing at all.  Not because it was unreachable: because
    # no fixture in this directory carried the flow entity, so peak_flow was 0.0 in
    # every replay while production feeds a real rate off the scale, and the branch
    # could only ever be taken one way.  Two fixtures, identical in every term the
    # rule reads except the flow, are what make it a test rather than a constant.
    #
    # Two files and not one cycle on purpose: classify short-circuits to "yield" the
    # moment a dose is waiting to be paired, so a dose followed by a pour never
    # reaches the flow comparison.  Each fixture therefore gets its own pairer, which
    # is what run() gives it.
    print("\noverlap_dose.csv — 25.0 g ground on and left standing, no flow")
    events, pairs, weighings = run(HERE / "testdata" / "overlap_dose.csv", detail=True)
    dose_w = weighings[0] if weighings else None
    ok &= check(
        "one weighing, 25.0 g — the one mass the shipped dose and yield ranges share",
        len(events) == 1 and events[0][1] == 25.0,
        str(events),
    )
    ok &= check(
        "nothing was flowing while the beans sat there",
        dose_w is not None and dose_w.peak_flow < 0.5,
        f"peak flow {dose_w.peak_flow}" if dose_w else "no weighing",
    )
    ok &= check("labelled dose", ("dose", 25.0) in events, str(events))

    print("\noverlap_pour.csv — the same 25.0 g held the same 40 s, but poured")
    events, pairs, weighings = run(HERE / "testdata" / "overlap_pour.csv", detail=True)
    pour_w = weighings[0] if weighings else None
    ok &= check(
        "one weighing, 25.0 g — the same mass, the same overlap",
        len(events) == 1 and events[0][1] == 25.0,
        str(events),
    )
    # The assertion that catches the harness rather than the detector.  If Sample is
    # ever built without a flow again, or the fixture's flow rows stop matching what
    # replay_brew looks for, this is 0.0 and everything below it goes on passing for
    # the wrong reason.
    ok &= check(
        "the 2.5 g/s of the last drops reached the detector — not the 0.0 a harness "
        "that dropped flow would show",
        pour_w is not None and abs(pour_w.peak_flow - 2.5) < 0.05,
        f"peak flow {pour_w.peak_flow}" if pour_w else "no weighing",
    )
    ok &= check("labelled yield", ("yield", 25.0) in events, str(events))
    # The margin: both are past the tiebreak's 30 s, so hold time cannot be what told
    # them apart and only the flow is left.  Without this the pair of fixtures could
    # quietly start passing on duration if either one's timing drifted.
    ok &= check(
        "and the two are told apart by flow alone — both are held well past the 30 s "
        "the rule also asks for",
        dose_w is not None and pour_w is not None
        and dose_w.duration >= 30.0 and pour_w.duration >= 30.0,
        f"hold {round(dose_w.duration, 1) if dose_w else None} / "
        f"{round(pour_w.duration, 1) if pour_w else None} s",
    )

    # And again with the ranges widened, which is the case the branch exists for.  At
    # the shipped defaults dose_max and yield_min are both 25.0, so the overlap is one
    # exact value and these fixtures sit on it; a user who widens either range through
    # the options flow turns the branch on for a whole band of masses.  Replaying the
    # same two captures with dose_max at 30 g keeps the coverage honest if a default
    # ever moves — otherwise a one-tenth change to dose_max would leave both fixtures
    # green and the rule untested.
    widened = brew_detect.DetectorConfig()
    widened.dose_max = 30.0
    print("\nboth again with dose_max widened to 30 g — the overlap a user can create")
    events, _ = run(HERE / "testdata" / "overlap_dose.csv", config=widened)
    ok &= check("the ground-on 25.0 g is still a dose", ("dose", 25.0) in events, str(events))
    events, _ = run(HERE / "testdata" / "overlap_pour.csv", config=widened)
    ok &= check("the poured 25.0 g is still a yield", ("yield", 25.0) in events, str(events))

    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
