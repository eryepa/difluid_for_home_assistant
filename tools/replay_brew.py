#!/usr/bin/env python3
"""Replay recorded weight history through the production detector.

This runs the exact module the coordinator runs (`brew_detect.py`), so thresholds
that look right here are the thresholds that will fire on the real scale.

    ./fetch_history.py --days 7 --out history.csv
    ./replay_brew.py history.csv

Recorder history is change-point-only: consecutive identical readings are collapsed.
The detector is timestamp-driven and holds each value until the next change
(zero-order hold), which is what makes this replay equivalent to the live 5 Hz feed.
`unavailable` rows mark BLE disconnects and reset the detector.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

# Load brew_detect.py directly rather than importing the package: the package
# __init__ pulls in Home Assistant, and the whole point of brew_detect is that it
# runs without it.  If this import ever needs HA, that is a bug in brew_detect.
_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "difluid_microbalance"
    / "brew_detect.py"
)
_spec = importlib.util.spec_from_file_location("brew_detect", _MODULE_PATH)
brew_detect = importlib.util.module_from_spec(_spec)
# @dataclass resolves annotations through sys.modules, so register before exec.
sys.modules["brew_detect"] = brew_detect
_spec.loader.exec_module(brew_detect)

BrewDetector = brew_detect.BrewDetector
BrewPairer = brew_detect.BrewPairer
DetectorConfig = brew_detect.DetectorConfig
Sample = brew_detect.Sample

WEIGHT_SUFFIX = "_weight"
FLOW_SUFFIX = "_flow_rate"

#: How often to re-emit a held reading when reconstructing the stream, and how far
#: to carry one forward before accepting that the stream really did stop.  The scale
#: powers off after five minutes and that shows up as an `unavailable` row, so a
#: silent gap longer than this is a recorder artefact we should not invent data for.
# Must be the production notification period, not a round number.  The detector is
# written to be time-driven precisely so replay and production agree, but the Hampel
# prefilter has one sample-counted quantity in it — its window is five samples — and
# how long five samples take is exactly what this constant sets.  At 1.0 s a step to
# a new value was rejected for two seconds before the filter flushed; at the real
# 5 Hz it is rejected for four tenths.  Every rule that measures time since the last
# rejection therefore read five times too long offline, which is how the tare fix of
# v1.4.0-beta.7 passed its fixture and still emailed "ratio 1:3.56" the next morning.
ZOH_PERIOD = 0.2
ZOH_MAX_FILL = 300.0


def parse_ts(raw: str) -> float:
    return datetime.fromisoformat(raw).timestamp()


def zero_order_hold(readings, period: float = ZOH_PERIOD, max_fill: float = ZOH_MAX_FILL):
    """Reconstruct the live stream from change-point-only recorder history.

    The recorder collapses consecutive identical readings, so beans held on the
    scale for three minutes appear as one row followed by silence.  The detector
    cannot distinguish that from a dead BLE link — anything past
    ``gap_reset_seconds`` is treated as a broken stream — so offline a single long
    weighing gets chopped into fragments that production, streaming at 5 Hz, never
    sees.  Re-emitting the held value at a steady period restores the replay ⇄
    production equivalence the whole tuning approach rests on.

    ``readings`` yields ``(t, weight)`` pairs; ``weight is None`` marks a genuine
    break (an ``unavailable`` row) and nothing is filled across it.
    """
    prev_t = prev_w = None
    for t, weight in readings:
        if weight is None:
            yield t, None
            prev_t = prev_w = None
            continue
        if prev_t is not None:
            filler = prev_t + period
            while filler < t and filler - prev_t <= max_fill:
                yield filler, prev_w
                filler += period
        yield t, weight
        prev_t, prev_w = t, weight


def weight_readings(rows: list[dict]):
    """Yield ``(t, weight)`` for the weight entity; ``None`` weight marks a break."""
    for row in rows:
        if not row["entity_id"].endswith(WEIGHT_SUFFIX):
            continue
        state = row["state"]
        if state in ("unavailable", "unknown", "", None):
            yield parse_ts(row["ts"]), None
            continue
        try:
            yield parse_ts(row["ts"]), float(state)
        except ValueError:
            continue


def flow_readings(rows: list[dict]) -> list[tuple[float, float]]:
    """Collect ``(t, flow)`` for the flow entity, oldest first.

    Separate from the weight entity because that is how the recorder stores it: two
    entities updated from one BLE packet, written to the state machine microseconds
    apart and therefore never landing on identical timestamps.  Keeping the readings
    as an ordered list rather than a dict keyed on the exact instant is what makes
    `with_flow` able to hold a value forward instead of demanding an exact match —
    see there for why matching exactly is a trap.
    """
    out: list[tuple[float, float]] = []
    for row in rows:
        if not row["entity_id"].endswith(FLOW_SUFFIX):
            continue
        try:
            out.append((parse_ts(row["ts"]), float(row["state"])))
        except ValueError:
            continue
    return out


def with_flow(stream, flows: list[tuple[float, float]]):
    """Attach to each ``(t, weight)`` the flow rate that was current at ``t``.

    Production reads weight and flow out of the same notification and hands both to
    the detector together, so every sample it sees carries a flow rate.  Offline the
    two arrive as separate entities, change-point-only like everything else the
    recorder stores, and the reconstruction is the same zero-order hold that
    `zero_order_hold` applies to weight: the last reported flow stands until the next
    one, including across the filler samples, which have no flow row of their own by
    construction.

    Held forward rather than looked up by exact timestamp.  An exact match is the
    same class of defect as ZOH_PERIOD was: it appears to work — the code reads the
    flow entity, the fixture contains flow rows — while in fact almost never firing,
    because a filler sample lands on prev_t + ZOH_PERIOD and a real recorder writes
    the two entities a few microseconds apart.  Every flow would then silently read
    0.0 offline while production varied it, and the peak_flow tiebreak in
    brew_detect.classify would be exercised by nothing.

    Carried across an ``unavailable`` break on purpose.  The recorder stores change
    points, so if the flow really did change while the link was down there is a row
    at the far side saying so; if there is none, the value genuinely did not change
    and holding it is the honest reconstruction.
    """
    held = 0.0
    idx = 0
    for t, weight in stream:
        while idx < len(flows) and flows[idx][0] <= t:
            held = flows[idx][1]
            idx += 1
        yield t, weight, held


def format_rise(plateau) -> str:
    """Render ``rise_seconds`` for a human, keeping unknown distinct from zero.

    ``None`` and ``0.0`` are opposite findings, not two spellings of one — see the
    field's own comment in brew_detect — and which of the two a candidate carries is
    what decides whether BrewPairer prefers it, so anyone reading this output to work
    out why the wrong dose won has to be able to tell them apart at a glance.
    Coercing None to 0.0 to keep the format string happy would erase exactly the
    distinction the column exists to show, and would do it silently.

    "unknown" is seven characters, the same width as ``f"{x:5.1f} s"``, so the
    columns either side of it stay lined up.
    """
    if plateau.rise_seconds is None:
        return "unknown"
    return f"{plateau.rise_seconds:5.1f} s"


def load_rows(path: str) -> list[dict]:
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: r["ts"])
    return rows


def build_config(args: argparse.Namespace) -> DetectorConfig:
    cfg = DetectorConfig()
    for field_name in vars(cfg):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(cfg, field_name, value)
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--verbose", action="store_true", help="print every plateau")
    for field_name, default in vars(DetectorConfig()).items():
        parser.add_argument(
            f"--{field_name.replace('_', '-')}",
            dest=field_name,
            type=type(default),
            default=None,
            help=f"default {default}",
        )
    args = parser.parse_args()

    cfg = build_config(args)
    detector = BrewDetector(cfg)
    pairer = BrewPairer(cfg)

    rows = load_rows(args.csv_path)
    flows = flow_readings(rows)

    plateaus = 0
    pairs = 0

    def report(plateau, kind, pair) -> None:
        stamp = datetime.fromtimestamp(plateau.t_start).strftime("%Y-%m-%d %H:%M:%S")
        if args.verbose or kind != "other":
            print(
                f"{stamp}  {kind:>5}  {plateau.value:7.1f} g  "
                f"hold {plateau.duration:5.1f} s  rise {format_rise(plateau)}  "
                f"peak flow {plateau.peak_flow:4.1f}"
            )
        if pair is not None:
            print(
                f"    -> PAIR  dose {pair.dose:.1f} g  yield {pair.yield_g:.1f} g  "
                f"ratio 1:{pair.ratio:.2f}"
            )

    for ts, weight, flow in with_flow(zero_order_hold(weight_readings(rows)), flows):
        if weight is None:
            closed = detector.flush()
            detector.reset()
        else:
            closed = detector.feed(Sample(t=ts, weight=weight, flow=flow))
        if closed is not None:
            plateaus += 1
            kind, pair = pairer.offer(closed)
            pairs += pair is not None
            report(closed, kind, pair)

    closed = detector.flush()
    if closed is not None:
        plateaus += 1
        kind, pair = pairer.offer(closed)
        pairs += pair is not None
        report(closed, kind, pair)

    print(f"\n{plateaus} plateaus, {pairs} pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
