"""Home Assistant glue around the pure detector in `brew_detect`.

Holds the most recent dose, pour and paired shot, survives restarts and BLE drops,
and fires a bus event so results are visible in Developer Tools -> Events without
any UI.  One instance per Home Assistant, kept in `hass.data[DOMAIN][BREW_KEY]`, so
the R2 coordinator can read the pair in iteration 2 without reaching into the scale
coordinator.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .brew_detect import (
    BrewDetector,
    BrewPair,
    BrewPairer,
    DetectorConfig,
    Plateau,
    Sample,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BREW_KEY = "brew"
EVENT_BREW_DETECTED = f"{DOMAIN}_brew_detected"
EVENT_PLATEAU_DETECTED = f"{DOMAIN}_plateau_detected"

_STORE_VERSION = 1
_STORE_KEY = f"{DOMAIN}.brew"
_SAVE_DELAY = 5  # seconds; the scale streams fast, no need to hit disk per event

DATASET_FILENAME = "difluid_brew_dataset.jsonl"
# Raw window kept per plateau for later analysis.  A brew window is ~120 samples of
# two floats, so this is negligible even after months of daily use.
_DATASET_WINDOW_SECONDS = 180.0
_DATASET_MAX_SAMPLES = 400


@dataclass
class WeighEvent:
    value: float
    at: float
    hold_seconds: float
    rise_seconds: float

    @classmethod
    def from_plateau(cls, plateau: Plateau) -> "WeighEvent":
        return cls(
            value=round(plateau.value, 2),
            at=plateau.t_end,
            hold_seconds=round(plateau.duration, 1),
            rise_seconds=round(plateau.rise_seconds, 1),
        )


class BrewSession:
    """Detects dose/pour plateaus on the weight stream and remembers the last pair."""

    def __init__(self, hass: HomeAssistant, config: Optional[DetectorConfig] = None):
        self.hass = hass
        self.cfg = config or DetectorConfig()
        self._detector = BrewDetector(self.cfg)
        self._pairer = BrewPairer(self.cfg)
        self._store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        self._listeners: list = []
        self._recent: list[Sample] = []
        self.record_dataset = False

        self.last_dose: Optional[WeighEvent] = None
        self.last_yield: Optional[WeighEvent] = None
        self.last_pair: Optional[BrewPair] = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        try:
            if data.get("last_dose"):
                self.last_dose = WeighEvent(**data["last_dose"])
            if data.get("last_yield"):
                self.last_yield = WeighEvent(**data["last_yield"])
            if data.get("last_pair"):
                self.last_pair = BrewPair(**data["last_pair"])
        except (TypeError, ValueError) as err:
            _LOGGER.warning("Ignoring unreadable stored brew state: %s", err)

    def apply_config(self, config: DetectorConfig, record_dataset: bool) -> None:
        """Swap thresholds at runtime — the options flow calls this."""
        self.cfg = config
        self.record_dataset = record_dataset
        self._detector = BrewDetector(config)
        self._pairer = BrewPairer(config)

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def reset(self) -> None:
        """Stream broke — flush any finished plateau, then drop detector state.

        Flushing first matters: the BLE link routinely drops within a second or two
        of the cup being lifted, and an open plateau at that moment is complete data
        — the pour already happened and was held long enough to qualify. Discarding
        it silently loses the shot, which is exactly what happened on 2026-08-10
        19:47 (pour held 5.8 s at 36.7 g, never reported).
        """
        try:
            plateau = self._detector.flush()
            if plateau is not None:
                self._on_plateau(plateau)
        except Exception:  # noqa: BLE001 - never let cleanup break the disconnect path
            _LOGGER.exception("Failed to flush plateau on reset")
        self._detector.reset()
        self._recent.clear()

    # ── the hot path ─────────────────────────────────────────────────────────

    def feed(self, weight: float, flow: float) -> None:
        """Called from the BLE notification handler, ~5 times per second.

        Must stay cheap and must never raise: an exception here would break sensor
        updates for the whole device.
        """
        try:
            sample = Sample(t=time.time(), weight=weight, flow=flow)
            if self.record_dataset:
                self._remember(sample)
            plateau = self._detector.feed(sample)
            if plateau is not None:
                self._on_plateau(plateau)
        except Exception:  # noqa: BLE001 - detector must not break the coordinator
            _LOGGER.exception("Brew detector raised; ignoring this sample")

    def _remember(self, sample: Sample) -> None:
        self._recent.append(sample)
        cutoff = sample.t - _DATASET_WINDOW_SECONDS
        while self._recent and self._recent[0].t < cutoff:
            self._recent.pop(0)
        if len(self._recent) > _DATASET_MAX_SAMPLES:
            del self._recent[: len(self._recent) - _DATASET_MAX_SAMPLES]

    def _on_plateau(self, plateau: Plateau) -> None:
        kind, pair = self._pairer.offer(plateau)
        event = WeighEvent.from_plateau(plateau)

        if kind == "dose":
            self.last_dose = event
        elif kind == "yield":
            self.last_yield = event

        _LOGGER.info(
            "Plateau: %s %.1f g (held %.1f s, rise %.1f s)",
            kind, plateau.value, plateau.duration, plateau.rise_seconds,
        )
        self.hass.bus.async_fire(
            EVENT_PLATEAU_DETECTED, {"kind": kind, **asdict(event)}
        )

        if pair is not None:
            self.last_pair = pair
            _LOGGER.info(
                "Brew pair: dose %.1f g -> yield %.1f g (1:%.2f)",
                pair.dose, pair.yield_g, pair.ratio,
            )
            self.hass.bus.async_fire(
                EVENT_BREW_DETECTED,
                {
                    "dose": round(pair.dose, 2),
                    "yield": round(pair.yield_g, 2),
                    "ratio": round(pair.ratio, 3),
                    "dose_at": pair.dose_at,
                    "yield_at": pair.yield_at,
                },
            )

        if self.record_dataset:
            self.hass.async_add_executor_job(self._append_dataset, kind, plateau)

        self._save()
        for callback in list(self._listeners):
            callback()

    # ── persistence ──────────────────────────────────────────────────────────

    def _save(self) -> None:
        self._store.async_delay_save(self._snapshot, _SAVE_DELAY)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "last_dose": asdict(self.last_dose) if self.last_dose else None,
            "last_yield": asdict(self.last_yield) if self.last_yield else None,
            "last_pair": asdict(self.last_pair) if self.last_pair else None,
        }

    def _append_dataset(self, kind: str, plateau: Plateau) -> None:
        """Append one labelled plateau with its raw window (executor thread)."""
        path = self.hass.config.path(DATASET_FILENAME)
        record = {
            "kind": kind,
            "value": round(plateau.value, 2),
            "t_start": plateau.t_start,
            "t_end": plateau.t_end,
            "hold_seconds": round(plateau.duration, 2),
            "rise_seconds": round(plateau.rise_seconds, 2),
            "peak_flow": round(plateau.peak_flow, 2),
            "window": [
                [round(s.t, 3), s.weight, s.flow]
                for s in self._recent
                if plateau.t_start - 60.0 <= s.t <= plateau.t_end + 10.0
            ],
        }
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as err:  # noqa: BLE001 - dataset capture is best-effort
            _LOGGER.warning("Could not append brew dataset to %s: %s", path, err)


async def async_get_session(
    hass: HomeAssistant, config: DetectorConfig, record_dataset: bool
) -> BrewSession:
    """Return the shared session, creating and loading it on first use."""
    bucket = hass.data.setdefault(DOMAIN, {})
    session: Optional[BrewSession] = bucket.get(BREW_KEY)
    if session is None:
        session = BrewSession(hass, config)
        await session.async_load()
        bucket[BREW_KEY] = session
    session.apply_config(config, record_dataset)
    return session
