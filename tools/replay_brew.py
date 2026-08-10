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


def parse_ts(raw: str) -> float:
    return datetime.fromisoformat(raw).timestamp()


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
    flow_by_time: dict[float, float] = {}
    for row in rows:
        if row["entity_id"].endswith(FLOW_SUFFIX):
            try:
                flow_by_time[parse_ts(row["ts"])] = float(row["state"])
            except ValueError:
                continue

    last_flow = 0.0
    plateaus = 0
    pairs = 0

    def report(plateau, kind, pair) -> None:
        stamp = datetime.fromtimestamp(plateau.t_start).strftime("%Y-%m-%d %H:%M:%S")
        if args.verbose or kind != "other":
            print(
                f"{stamp}  {kind:>5}  {plateau.value:7.1f} g  "
                f"hold {plateau.duration:5.1f} s  rise {plateau.rise_seconds:5.1f} s  "
                f"peak flow {plateau.peak_flow:4.1f}"
            )
        if pair is not None:
            print(
                f"    -> PAIR  dose {pair.dose:.1f} g  yield {pair.yield_g:.1f} g  "
                f"ratio 1:{pair.ratio:.2f}"
            )

    for row in rows:
        if not row["entity_id"].endswith(WEIGHT_SUFFIX):
            continue
        state = row["state"]
        if state in ("unavailable", "unknown", "", None):
            closed = detector.flush()
            detector.reset()
            if closed is not None:
                plateaus += 1
                kind, pair = pairer.offer(closed)
                pairs += pair is not None
                report(closed, kind, pair)
            continue
        try:
            weight = float(state)
        except ValueError:
            continue

        ts = parse_ts(row["ts"])
        last_flow = flow_by_time.get(ts, last_flow)
        closed = detector.feed(Sample(t=ts, weight=weight, flow=last_flow))
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
