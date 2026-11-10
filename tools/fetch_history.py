#!/usr/bin/env python3
"""Dump Home Assistant recorder history to CSV for offline detector tuning.

The recorder keeps ~10 days of the scale's weight stream at full resolution, which
is a better tuning corpus than anything a downsampled metrics store would give.

Usage:
    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=<long-lived access token>
    ./fetch_history.py --days 7 --out history.csv

Creating the token: HA profile page -> Security -> Long-lived access tokens.

The output is deliberately raw — one row per recorded state change, `unavailable`
included, because the replay harness needs those to know where to break the buffer.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_ENTITIES = [
    "sensor.microbalance_304268_weight",
    "sensor.microbalance_304268_flow_rate",
    # Dev-only labelling aid.  Noisy (309 firings in 7 days) so it must never
    # become a production trigger, but it helps eyeball which plateau was a brew.
    "binary_sensor.coffee_vibration_vibratsiia",
]


def fetch(base_url: str, token: str, entities: list[str], days: int) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(days=days)
    path = f"/api/history/period/{start.isoformat()}"
    query = urllib.parse.urlencode(
        {"filter_entity_id": ",".join(entities), "minimal_response": ""}
    )
    url = f"{base_url.rstrip('/')}{path}?{query}"

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default="history.csv")
    parser.add_argument("--entity", action="append", dest="entities")
    args = parser.parse_args()

    base_url = os.environ.get("HA_URL")
    token = os.environ.get("HA_TOKEN")
    if not base_url or not token:
        print("Set HA_URL and HA_TOKEN environment variables.", file=sys.stderr)
        return 2

    entities = args.entities or DEFAULT_ENTITIES
    series = fetch(base_url, token, entities, args.days)

    rows = []
    for entity_states in series:
        for state in entity_states:
            rows.append(
                {
                    "entity_id": state.get("entity_id")
                    or entity_states[0].get("entity_id", ""),
                    "ts": state.get("last_updated") or state.get("last_changed"),
                    "state": state.get("state"),
                }
            )
    rows.sort(key=lambda r: (r["ts"] or ""))

    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["entity_id", "ts", "state"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
