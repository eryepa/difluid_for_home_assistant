"""Home Assistant glue around the pure detector in `brew_detect`.

Holds the most recent dose, pour and paired shot, survives restarts and BLE drops,
and fires a bus event so results are visible in Developer Tools -> Events without
any UI.

One instance per detector config entry, owned by that entry and kept in
`hass.data[DOMAIN][entry.entry_id]` alongside the coordinators.  It used to be a
singleton under a `BREW_KEY` bucket, on the theory that the R2 would one day read
the pair without reaching into the scale coordinator.  That day never came, and the
singleton cost more than it saved: the session's entities were registered against
the *scale's* device because the scale entry was the only thing that could register
them, and unloading an entry could not tell whether anyone else still needed the
session.  Giving the detector its own entry answers both — the owner is explicit,
and the entities belong to a device that is the detector rather than the scale.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .brew_detect import (
    BrewDetector,
    BrewPair,
    BrewPairer,
    BrewTotals,
    DetectorConfig,
    Plateau,
    Sample,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

EVENT_BREW_DETECTED = f"{DOMAIN}_brew_detected"
EVENT_PLATEAU_DETECTED = f"{DOMAIN}_plateau_detected"

_STORE_VERSION = 1

DEFAULT_STORE_KEY = f"{DOMAIN}.brew"
_SAVE_DELAY = 5

DATASET_FILENAME = "difluid_brew_dataset.jsonl"
_DATASET_WINDOW_SECONDS = 180.0
_DATASET_MAX_SAMPLES = 400

_SAME_DOSE_SECONDS = 1.0

MAX_MEASUREMENTS = 50

@dataclass
class BrewMeasurement:
    """One brew that was put under the refractometer.

    Kept rather than read live, because the R2 is a handheld that spends almost all of
    its life switched off: its sensors go `unavailable` minutes after a reading, so a
    chart that read the sensor would be empty except in the moments right after a
    measurement — which is exactly when you are not looking at a dashboard.

    `ext` is stored alongside the inputs it comes from even though it is derived.  It
    costs one float and it means a point plotted a month from now is the number that
    was true then, rather than the number this version's formula would produce today.
    """

    at: float
    dose: float
    yield_g: Optional[float]
    tds: float
    ext: Optional[float]
    measured_at: float
    seconds: Optional[float] = None

    @property
    def ratio(self) -> Optional[float]:
        if not self.dose or self.yield_g is None:
            return None
        return self.yield_g / self.dose

    @staticmethod
    def extraction(dose: float, yield_g: Optional[float], tds: float) -> Optional[float]:
        """TDS × yield / dose, or None when there is no yield to divide by."""
        if not dose or yield_g is None:
            return None
        return round(tds * yield_g / dose, 2)

    @classmethod
    def build(cls, brew: "BrewAnchor", tds: float, measured_at: float) -> "BrewMeasurement":
        return cls(
            at=brew.at,
            dose=brew.dose,
            yield_g=brew.yield_g,
            tds=tds,
            ext=cls.extraction(brew.dose, brew.yield_g, tds),
            measured_at=measured_at,
            seconds=brew.pour_seconds,
        )

@dataclass(frozen=True)
class BrewAnchor:
    """The brew a reading taken right now belongs to.

    Not a BrewPair, because the whole point is that it does not need to be one.  A
    reading arrives after the coffee is made, and by then the scale may have seen both
    halves of the brew, or only the dose.
    """

    at: float
    dose: float
    yield_g: Optional[float]
    pour_seconds: Optional[float]

@dataclass
class WeighEvent:
    value: float
    at: float
    hold_seconds: float
    rise_seconds: Optional[float] = None
    steps: list = field(default_factory=list)

    @classmethod
    def from_plateau(cls, plateau: Plateau) -> "WeighEvent":
        """Build the reportable form of one weighing.

        ``plateau`` must already be on the wall clock: _on_plateau converts it before
        calling this, so that ``at`` is something sensor.py can hand to
        dt_util.utc_from_timestamp and a stored record still names the same instant
        after a restart.
        """
        rise = plateau.rise_seconds
        return cls(
            value=round(plateau.value, 2),
            at=plateau.t_end,
            hold_seconds=round(plateau.duration, 1),
            rise_seconds=None if rise is None else round(rise, 1),
            steps=list(plateau.steps),
        )

class BrewSession:
    """Detects dose/pour plateaus on the weight stream and remembers the last pair."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: Optional[DetectorConfig] = None,
        store_key: str = DEFAULT_STORE_KEY,
    ):
        self.hass = hass
        self.cfg = config or DetectorConfig()
        self._detector = BrewDetector(self.cfg)
        self._pairer = BrewPairer(self.cfg)
        self._store: Store = Store(hass, _STORE_VERSION, store_key)
        self._listeners: list = []
        self._recent: list[Sample] = []
        self.record_dataset = False
        self._clock_offset: float = time.time() - time.monotonic()

        self.last_dose: Optional[WeighEvent] = None
        self.last_yield: Optional[WeighEvent] = None
        self.last_pair: Optional[BrewPair] = None
        self.brew_count: int = 0
        self.totals = BrewTotals()
        self.measurements: list[BrewMeasurement] = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            self.totals.period_started = time.time()
            return
        self.last_dose = self._restore(WeighEvent, data, "last_dose")
        self.last_yield = self._restore(WeighEvent, data, "last_yield")
        self.last_pair = self._restore(BrewPair, data, "last_pair")
        self.brew_count = self._restore_count(data)
        self.totals = self._restore_totals(data)
        self.measurements = self._restore_measurements(data)
        if not self.totals.period_started:
            self.totals.period_started = time.time()

    @staticmethod
    def _restore(factory, data: dict[str, Any], key: str):  # noqa: N805 - staticmethod
        """Rebuild one stored dataclass, returning None if it cannot be read.

        Logged at warning rather than debug: the field is being dropped, and a
        last_dose that quietly reverts to "unknown" after a restart is otherwise
        indistinguishable from the detector having missed the weighing.
        """
        stored = data.get(key)
        if not stored:
            return None
        try:
            return factory(**stored)
        except (TypeError, ValueError) as err:
            _LOGGER.warning(
                "Dropping unreadable stored brew field %s (%s): %s — the other "
                "stored fields were kept",
                key, stored, err,
            )
            return None

    @staticmethod
    def _restore_measurements(data: dict[str, Any]) -> list["BrewMeasurement"]:
        """Rebuild the measured brews, keeping every record that still reads."""
        stored = data.get("measurements")
        if not isinstance(stored, list):
            return []
        kept: list[BrewMeasurement] = []
        dropped = 0
        for record in stored:
            try:
                kept.append(BrewMeasurement(**record))
            except (TypeError, ValueError):
                dropped += 1
        if dropped:
            _LOGGER.warning(
                "Dropped %d unreadable measured brew(s); kept %d", dropped, len(kept)
            )
        kept.sort(key=lambda m: m.at)
        return kept[-MAX_MEASUREMENTS:]

    @staticmethod
    def _restore_count(data: dict[str, Any]) -> int:
        """Rebuild brew_count, which must survive anything the other fields do."""
        stored = data.get("brew_count")
        try:
            count = int(stored or 0)
        except (TypeError, ValueError) as err:
            _LOGGER.warning(
                "Stored brew_count %r is unreadable (%s); restarting the count at 0. "
                "The brew counter is TOTAL_INCREASING, so long-term statistics will "
                "record this as a counter reset",
                stored, err,
            )
            return 0
        if count < 0:
            _LOGGER.warning(
                "Stored brew_count %r is negative; restarting the count at 0", stored
            )
            return 0
        return count

    @staticmethod
    def _restore_totals(data: dict[str, Any]) -> BrewTotals:
        """Rebuild the odometers, keeping every field that can still be read.

        The recovery itself lives in BrewTotals.from_stored, which is pure and
        therefore testable offline; this only turns what it could not read into log
        lines.  Warning rather than debug for the same reason _restore does: a total
        that quietly drops back to a default is indistinguishable from one that was
        never counting.
        """
        totals, problems = BrewTotals.from_stored(data.get("totals"))
        for name, value in problems:
            _LOGGER.warning(
                "Stored brew total %s is unreadable (%r); keeping its default of %r. "
                "Cumulative figures will under-report from here",
                name, value, getattr(totals, name),
            )
        return totals

    def apply_config(self, config: DetectorConfig, record_dataset: bool) -> None:
        """Swap thresholds at runtime — the options flow calls this."""
        self.cfg = config
        self.record_dataset = record_dataset
        self._detector = BrewDetector(config)
        self._pairer = BrewPairer(config)

    async def async_remove(self) -> None:
        """Forget everything, in memory and on disk.

        Only ever called when the detector's config entry is being removed, never on a
        reload — see async_remove_entry in __init__.py.

        Store.async_remove cancels the pending delayed save before deleting the file,
        which matters here: _save schedules a write five seconds out, so without that
        a session removed just after a brew would rewrite the state file seconds
        after it was deleted and the next install would still find it.
        """
        self._listeners.clear()
        self._recent.clear()
        self.last_dose = None
        self.last_yield = None
        self.last_pair = None
        self.brew_count = 0
        self.totals = BrewTotals(period_started=time.time())
        self.measurements = []
        await self._store.async_remove()

    def current_brew(self) -> Optional[BrewAnchor]:
        """The brew a reading taken now belongs to: the newest one there is evidence of.

        Usually the last completed pair.  But a dose weighed *after* that pair is a
        brew of its own, in progress or finished with its pour unseen, and it is the
        one being measured — the pair is then the cup before it.

        Anchoring on the pair alone is what produced the wrong answer on 2026-08-26:
        18.1 g was ground at 10:06, the BLE link died 25 seconds later and stayed down
        through the shot, and the two refractometer readings taken at 10:12 were
        divided by the dose and yield of an 08:44 brew.  Every number was right and
        every one was about the wrong coffee.

        The dose comes from last_dose, which is whatever was last weighed in the dose
        range and *not* the pairer's considered choice between competing candidates.
        So a portafilter set back on the scale can become the anchor, the way one did
        on 2026-08-17.  That is a visible mistake — Last Dose shows it on the device
        page and the measurement carries its time — where the alternative, using the
        pairer's pending dose, is invisible from outside and does not survive a
        restart, which is exactly the situation this exists to survive.
        """
        pair = self.last_pair
        dose = self.last_dose
        if dose is not None and (pair is None or dose.at > pair.yield_at):
            return BrewAnchor(at=dose.at, dose=dose.value, yield_g=None, pour_seconds=None)
        if pair is not None:
            return BrewAnchor(
                at=pair.yield_at,
                dose=pair.dose,
                yield_g=pair.yield_g,
                pour_seconds=pair.pour_seconds,
            )
        return None

    def record_measurement(self, tds: float) -> Optional[BrewMeasurement]:
        """Attach a refractometer reading to the most recent brew.

        Always the most recent one, with no time window — measuring is something you
        do after pulling a shot, and a window would only ever be a guess about how
        long you took to get round to it.  What counts as "the most recent brew" is
        current_brew's business.

        Re-measuring replaces that brew's point rather than adding a second: the rule
        is one reading per brew, and a stirred-and-remeasured sample is a correction,
        not a second cup.  A reading taken before anything was ever weighed has
        nothing to attach to and is dropped.
        """
        brew = self.current_brew()
        if brew is None:
            _LOGGER.info(
                "Refractometer read %.2f%% with no brew to attach it to; ignoring", tds
            )
            return None

        point = BrewMeasurement.build(brew, tds, time.time())
        replaced = False
        for i, existing in enumerate(self.measurements):
            if existing.at == point.at:
                self.measurements[i] = point
                replaced = True
                break
        if not replaced:
            self.measurements.append(point)
            self.measurements.sort(key=lambda m: m.at)
            del self.measurements[:-MAX_MEASUREMENTS]

        _LOGGER.info(
            "Brew measured: %.1f g in, %s g out, TDS %.2f%%, extraction %s (%s)",
            point.dose,
            "?" if point.yield_g is None else f"{point.yield_g:.1f}",
            point.tds,
            "unknown — no pour was detected" if point.ext is None else f"{point.ext:.2f}%",
            "re-measured" if replaced else "new",
        )
        self._save()
        for callback in list(self._listeners):
            callback()
        return point

    def set_measured_yield(self, yield_g: float) -> Optional[BrewMeasurement]:
        """Fill in, or correct, the yield of the brew that was measured last.

        The manual half of the pour going missing: the BLE link on this install drops
        for minutes at a time, and a shot pulled through one of those gaps has a dose
        and a TDS but nothing that came out.
        """
        return self._amend_measurement("Measured Yield", yield_g, yield_g=yield_g)

    def set_measured_dose(self, dose: float) -> Optional[BrewMeasurement]:
        """Fill in, or correct, the dose of the brew that was measured last.

        The other half of set_measured_yield, and it exists for the opposite failure.
        A yield goes missing when the scale is off the air; a dose goes *wrong* while
        the scale is working perfectly.  current_brew anchors on last_dose, which is
        whatever was last weighed in the dose range and deliberately not the pairer's
        considered choice between candidates — so a portafilter set back on the scale
        becomes the dose, as one did on 2026-08-17 when Last Dose read 19.3 g for a
        shot that was ground at 18.0 g.  The reading is then arithmetically perfect
        about the wrong quantity of beans, and every figure derived from it — the
        ratio, the extraction, the dot's place on the control chart — is wrong with
        it.  Correcting the dose is the only way to make that brew mean anything, and
        it is a correction the refractometer cannot make for you.

        Zero is accepted and is not a rejection: extraction() and ratio both treat a
        falsy dose as "not computable" and return None, so typing 0 says the dose is
        unknown rather than asserting the beans weighed nothing.
        """
        return self._amend_measurement("Measured Dose", dose, dose=dose)

    def _amend_measurement(
        self, what: str, value: float, **changes
    ) -> Optional[BrewMeasurement]:
        """Edit one field of the last measured brew and recompute what depends on it.

        Shared by the two manual controls, because they are one operation on different
        fields and the part that is easy to get wrong is the part they have in common:
        `ext` is stored rather than derived at read time (see BrewMeasurement), so any
        edit to a dose or a yield that does not recompute it leaves the point carrying
        an extraction from the numbers it used to have.

        It edits the stored point rather than the brew, deliberately.  brew_count and
        the ground-coffee odometer count what the detector actually weighed, and a
        number typed in afterwards is not that — mixing the two would make the odometer
        a total of two different things.

        Typing anything before a brew has been measured does nothing, also
        deliberately.  There is no cup to attach it to yet, and remembering it for the
        next reading would mean a figure typed an hour ago silently overriding what the
        scale did see.
        """
        point = self.last_measurement
        if point is None:
            _LOGGER.warning(
                "%s set to %.1f g with nothing measured yet; ignoring", what, value
            )
            return None

        merged = replace(point, **changes)
        updated = replace(
            merged,
            ext=BrewMeasurement.extraction(merged.dose, merged.yield_g, merged.tds),
        )
        self.measurements[-1] = updated
        _LOGGER.info(
            "%s set by hand: %s g in, %s g out, TDS %.2f%%, extraction %s",
            what,
            "?" if not updated.dose else f"{updated.dose:.1f}",
            "?" if updated.yield_g is None else f"{updated.yield_g:.1f}",
            updated.tds,
            "unknown" if updated.ext is None else f"{updated.ext:.2f}%",
        )
        self._save()
        for callback in list(self._listeners):
            callback()
        return updated

    def _complete_measurement(self, pair: BrewPair) -> None:
        """Give a measured-but-yieldless brew its pour, once the scale reports one.

        The case this closes: the dose is weighed, the link drops, the shot is pulled
        and measured — a point with a TDS and no extraction — and then the scale comes
        back and reports the pour after all.  Without this the reading would sit
        incomplete forever while a second reading of the same cup started a second
        point, because a dose-anchored point is stamped with the dose's time and a
        paired one with the pour's.

        Matched on the dose's time, which is the one thing about a brew that does not
        move: the pour's time does not exist yet when the dose-anchored point is
        written.

        Matched with a tolerance rather than for equality, and that is not defensive
        rounding.  The two numbers are independent conversions of the same monotonic
        instant: last_dose.at was mapped to the wall clock when the dose closed, and
        pair.dose_at when the pour closed minutes later, each with the offset
        _sync_clock had measured at the time.  Any correction to the host clock in
        between — an NTP step is the obvious one — moves them apart, and an equality
        test would then simply never match, silently, leaving every such brew
        incomplete with nothing in the log to say why.  A second is orders of
        magnitude more than that drift and orders of magnitude less than the gap
        between two brews: a weighing alone takes longer than that.

        Matched on the time *alone*, and no longer on the yield still being missing.
        That extra condition looked like a safety rail and was the opposite: typing the
        yield into Measured Yield — the documented remedy for exactly this situation —
        made the point permanently unmatchable, so its `at` was never moved onto the
        pour.  `last_brew_at` then pointed at the pour while the point still carried
        the dose's time, so the card showed "a newer brew has been pulled since" for
        the cup in your hand, and a second reading of that same cup anchored on the
        pour's time, found no point there and started the twin this method exists to
        prevent.  The time match already implies the point has not been completed: a
        completed one carries its pour's time, not its dose's.

        The point adopts the pair whole — dose, yield and pour time together — rather
        than keeping whatever it was anchored on.  Two reasons, and the first is not
        negotiable: `ext` is stored rather than derived, so it must be computed from
        the numbers that end up in the same record.  It was computed from `pair.dose`
        while `dose` kept its old value, which left a stored brew whose own fields did
        not satisfy TDS x yield / dose — the relation the chart's ratio diagonals are
        drawn from.  The second is that the pair's dose is the better answer anyway:
        the point was anchored on `last_dose`, whatever was weighed last in the dose
        range, while the pair's is the pairer's considered choice between candidates.
        A figure typed in by hand is superseded here, deliberately — it was a stand-in
        for a pour the scale had not seen, and the scale has now seen it.
        """
        for i, point in enumerate(self.measurements):
            if abs(point.at - pair.dose_at) >= _SAME_DOSE_SECONDS:
                continue
            self.measurements[i] = replace(
                point,
                at=pair.yield_at,
                dose=pair.dose,
                yield_g=pair.yield_g,
                ext=BrewMeasurement.extraction(pair.dose, pair.yield_g, point.tds),
                seconds=pair.pour_seconds,
            )
            _LOGGER.info(
                "The pour for an already-measured brew arrived: %.1f g in, %.1f g out, "
                "extraction %s",
                pair.dose,
                pair.yield_g,
                "unknown" if self.measurements[i].ext is None
                else f"{self.measurements[i].ext:.2f}%",
            )
            self.measurements.sort(key=lambda m: m.at)
            return

    @property
    def last_measurement(self) -> Optional[BrewMeasurement]:
        return self.measurements[-1] if self.measurements else None

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def reset_period(self) -> None:
        """Start a new statistics period — what the Reset Period button calls.

        Note what this does *not* touch: brew_count, totals.total_dose_g, or anything
        the detector holds.  Only the trip snapshot moves, so a mis-pressed button
        costs the period and nothing else.  Named apart from reset() above, which is
        the BLE-disconnect path and an entirely different operation — the two must
        never be confused at a call site.
        """
        self.totals.reset_period(time.time(), self.brew_count)
        _LOGGER.info(
            "Brew statistics period reset at %d brews / %.1f g total",
            self.brew_count, self.totals.total_dose_g,
        )
        self._save()
        for callback in list(self._listeners):
            callback()

    def reset(self) -> None:
        """Stream broke — flush any finished weighing, then drop detector state.

        Flushing first matters: the BLE link routinely drops within a second or two
        of the cup being lifted, and an open weighing at that moment is complete data
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

    def _sync_clock(self) -> float:
        """Read the detector's clock, re-measuring its offset from the wall clock.

        Re-measured on every sample, i.e. about five times a second, and that is what
        keeps the conversion honest.  A wall-clock correction is picked up within one
        sample, so results are always mapped onto the clock that is in force *now*
        rather than the one that happened to be in force when the session started —
        which matters because Home Assistant timestamps the very same state change
        with its own clock, and a stale offset would put our ``detected_at`` ten
        minutes away from the recorder's own idea of when it happened.

        It also means the offset needs no explicit handling in the two places that
        look like they would need it.  Across reset() — a BLE disconnect — the
        monotonic clock never restarted, so the offset was never invalidated, and the
        first sample of the resumed stream re-measures it anyway; the flush that
        reset() performs first is deliberately converted with the offset from the last
        sample before the drop, which is the one that was in force when those samples
        were observed.  Across a Home Assistant restart there is nothing to carry: a
        fresh process gets a fresh monotonic epoch and measures a fresh offset with
        it, and because only wall-clock values were ever persisted, no stored
        timestamp is ever converted with an offset from a previous process.
        """
        now = time.monotonic()
        self._clock_offset = time.time() - now
        return now

    def _to_wall_clock(self, t: float) -> float:
        """Convert one detector timestamp into a POSIX timestamp.

        The single boundary between the two clocks: every consumer outside this class
        — sensor attributes, the bus events, Store, the JSONL dataset — is fed through
        here, so a timestamp that comes out wrong has exactly one place to look.

        Durations are unaffected either way, because both ends of one shift by the
        same offset: plateau.duration, hold_seconds and the pairing gaps mean the same
        thing on either clock.  Only absolute instants need this.
        """
        return t + self._clock_offset

    def _plateau_to_wall_clock(self, plateau: Plateau) -> Plateau:
        """The same weighing with its two absolute timestamps moved to the wall clock.

        A copy rather than a mutation: Plateau is frozen, and the original has to stay
        on the detector's clock for as long as the pairer might still compare it
        against another one.
        """
        return replace(
            plateau,
            t_start=self._to_wall_clock(plateau.t_start),
            t_end=self._to_wall_clock(plateau.t_end),
        )

    def feed(self, weight: float, flow: float) -> None:
        """Called from the BLE notification handler, ~5 times per second.

        Must stay cheap and must never raise: an exception here would break sensor
        updates for the whole device.
        """
        try:
            sample = Sample(t=self._sync_clock(), weight=weight, flow=flow)
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
        reported = self._plateau_to_wall_clock(plateau)
        if pair is not None:
            pair = replace(
                pair,
                dose_at=self._to_wall_clock(pair.dose_at),
                yield_at=self._to_wall_clock(pair.yield_at),
            )
        event = WeighEvent.from_plateau(reported)

        if kind == "dose":
            self.last_dose = event
        elif kind == "yield":
            self.last_yield = event

        rise = plateau.rise_seconds
        rise_text = "unknown" if rise is None else f"{rise:.1f} s"
        _LOGGER.info(
            "Weighing: %s %.1f g (steps %s, on the scale %.1f s, rise %s)",
            kind, plateau.value, plateau.steps, plateau.duration, rise_text,
        )
        self.hass.bus.async_fire(
            EVENT_PLATEAU_DETECTED, {"kind": kind, **asdict(event)}
        )

        if pair is not None:
            self.last_pair = pair
            self._complete_measurement(pair)
            self.brew_count += 1
            self.totals.add(pair.dose)
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
                    "count": self.brew_count,
                    "dose_at": pair.dose_at,
                    "yield_at": pair.yield_at,
                },
            )

        if self.record_dataset:
            window = [
                [round(self._to_wall_clock(s.t), 3), s.weight, s.flow]
                for s in self._recent
                if plateau.t_start - 60.0 <= s.t <= plateau.t_end + 10.0
            ]
            self.hass.async_create_task(
                self.hass.async_add_executor_job(
                    self._append_dataset, kind, reported, window
                ),
                f"{DOMAIN}_append_brew_dataset",
            )

        self._save()
        for callback in list(self._listeners):
            callback()

    def _save(self) -> None:
        self._store.async_delay_save(self._snapshot, _SAVE_DELAY)

    async def async_flush(self) -> None:
        """Write the pending snapshot now instead of in _SAVE_DELAY seconds.

        Called when the detector entry is unloaded.  Every write goes through
        async_delay_save, which is right while the scale is streaming — five samples a
        second must not be five disk writes a second — but it means up to five seconds
        of state is only in memory at any moment.

        That was free when the session was a singleton: an entry reload dropped the
        entry, never the session object, so the unwritten snapshot was still there
        afterwards.  Now the entry owns the session and a reload rebuilds it from
        disk — and a reload is exactly what changing a threshold does.  Without this
        flush, tightening a threshold in the seconds after weighing the beans would
        drop the dose, which is the specific failure the old singleton comment warned
        about and the one it was shaped to avoid.
        """
        await self._store.async_save(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        return {
            "last_dose": asdict(self.last_dose) if self.last_dose else None,
            "last_yield": asdict(self.last_yield) if self.last_yield else None,
            "last_pair": asdict(self.last_pair) if self.last_pair else None,
            "brew_count": self.brew_count,
            "totals": asdict(self.totals),
            "measurements": [asdict(m) for m in self.measurements],
        }

    def _append_dataset(
        self, kind: str, plateau: Plateau, window: list[list[float]]
    ) -> None:
        """Append one labelled plateau with its raw window (executor thread).

        `window` is passed in already snapshotted rather than read from
        ``self._recent`` here — see the call site in _on_plateau for why touching
        that list from this thread is not safe.

        Both `plateau` and `window` arrive already converted to the wall clock, so
        this method never touches _clock_offset: it runs on an executor thread and
        the offset is written from the event loop on every sample.  Doing the
        conversion at the call site keeps that a non-question.
        """
        rise = plateau.rise_seconds
        path = self.hass.config.path(DATASET_FILENAME)
        record = {
            "kind": kind,
            "value": round(plateau.value, 2),
            "t_start": plateau.t_start,
            "t_end": plateau.t_end,
            "hold_seconds": round(plateau.duration, 2),
            "final_hold_seconds": round(plateau.final_hold_seconds, 2),
            "steps": list(plateau.steps),
            "rise_seconds": None if rise is None else round(rise, 2),
            "peak_flow": round(plateau.peak_flow, 2),
            "window": window,
        }
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as err:  # noqa: BLE001 - dataset capture is best-effort
            _LOGGER.warning("Could not append brew dataset to %s: %s", path, err)

def detector_device_info(entry) -> "DeviceInfo":
    """The device a detector entry's entities belong to.

    A service device rather than a physical one, hung off the scale with via_device so
    the relationship is visible in the UI and the card can find one from the other.

    It exists because Home Assistant's device page renders exactly four cards and
    decides which one an entity lands in from its domain plus entity_category — there
    is no "statistics" card to ask for, and no way to order them.  On the scale's page
    the statistics could only ever be squeezed in beside the weight and the flow rate,
    or hidden under Diagnostic.  On a page of their own they are simply what the
    device reads, which is what they are.
    """
    from homeassistant.helpers.device_registry import DeviceEntryType
    from homeassistant.helpers.entity import DeviceInfo

    from .const import CONF_SCALE_ENTRY

    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Brew Detector",
        manufacturer="Difluid",
        model="Brew Detector",
        entry_type=DeviceEntryType.SERVICE,
        via_device=(DOMAIN, entry.data[CONF_SCALE_ENTRY]),
    )

async def async_create_session(
    hass: HomeAssistant, config: DetectorConfig, record_dataset: bool, store_key: str
) -> BrewSession:
    """Build a detector entry's session and restore whatever it stored last time."""
    session = BrewSession(hass, config, store_key)
    await session.async_load()
    session.apply_config(config, record_dataset)
    return session
