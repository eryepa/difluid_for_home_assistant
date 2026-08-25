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

#: Where the one and only session stored itself back when there was one and only
#: session.  A detector entry created by importing an existing install keeps this
#: key, which is the whole reason brew_count and the last pair survive the move to
#: a config entry of their own; a detector created from scratch gets a key derived
#: from its entry_id.  Either way the key is decided once, at entry creation, and
#: stored in entry.data as CONF_STORE_KEY — see const.py.
DEFAULT_STORE_KEY = f"{DOMAIN}.brew"
_SAVE_DELAY = 5  # seconds; the scale streams fast, no need to hit disk per event

DATASET_FILENAME = "difluid_brew_dataset.jsonl"
# Raw window kept per plateau for later analysis.  A brew window is ~120 samples of
# two floats, so this is negligible even after months of daily use.
_DATASET_WINDOW_SECONDS = 180.0
_DATASET_MAX_SAMPLES = 400


#: How many measured brews to keep.  One point per refractometer reading, and nobody
#: measures every shot, so this is months of them; the chart plots the tail of it.
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

    #: yield_at of the brew this belongs to; also the identity of the point, so a
    #: second reading of the same brew replaces the first instead of adding a twin.
    at: float
    dose: float
    yield_g: float
    tds: float
    #: Extraction percentage: TDS × yield / dose.  The same figure the DiFluid app
    #: plots, and the one its ratio diagonals are drawn for — a 1:2 line is TDS = EXT/2.
    ext: float
    measured_at: float

    @property
    def ratio(self) -> float:
        return self.yield_g / self.dose if self.dose else 0.0

    @classmethod
    def build(cls, pair: BrewPair, tds: float, measured_at: float) -> "BrewMeasurement":
        return cls(
            at=pair.yield_at,
            dose=pair.dose,
            yield_g=pair.yield_g,
            tds=tds,
            ext=round(tds * pair.ratio, 2),
            measured_at=measured_at,
        )


@dataclass
class WeighEvent:
    value: float
    #: When the load came off the scale, as a POSIX wall-clock timestamp — *not* the
    #: detector's own clock, which is monotonic and means nothing outside this
    #: process.  See the "clocks" section of BrewSession for why the two exist and
    #: where the conversion happens.
    at: float
    hold_seconds: float
    #: How long the load took to arrive, or ``None`` when that was never observed.
    #:
    #: Three-state on purpose, and ``None`` is not a missing ``0.0``.  A rise of 0.0
    #: is a positive observation — the scale was seen empty and the load was there in
    #: the very next sample, so it came out of a container and was set down.  ``None``
    #: says the detector never saw the scale empty before this weighing and therefore
    #: knows nothing at all about how the load got there: it was already sitting on
    #: the scale after an HA restart mid-session, after a BLE reconnect, or after a
    #: gap in the stream reset the detector.
    #:
    #: The two are opposite findings and BrewPairer.offer reads the difference as
    #: evidence for which candidate is the real dose.  Collapsing ``None`` to 0.0 is
    #: what silently reverted that rule to the hold-time heuristic and let a
    #: portafilter set back on the scale beat the beans on 2026-08-17.  See
    #: brew_detect.Plateau.rise_seconds and brew_detect._arrival_rank.
    #:
    #: Survives Store unchanged: ``asdict`` yields None, ``json`` writes ``null``, and
    #: _restore hands None straight back to this field.  Defaulted for the same reason
    #: ``steps`` is — state written by an earlier version still loads — and here the
    #: default is also the honest answer, since a stored record that carries no rise
    #: is exactly one whose rise is unknown.
    rise_seconds: Optional[float] = None
    #: Every stable reading of this weighing; more than one means it was topped up
    #: or was still settling.  Kept so a surprising value can be explained after the
    #: fact without digging through recorder history.  Defaulted so that state
    #: stored by an earlier version still loads.
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
            # None stays None.  round() raises on it, which is the immediate bug, but
            # substituting 0.0 to avoid that would be worse than the crash: it would
            # publish "set down in one go" as the arrival of a load nobody watched
            # arrive, which is the claim the 2026-08-17 wrong ratio was built on.
            # sensor.py already publishes the attribute as null, so nothing downstream
            # needs a number here.
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
        #: Wall clock minus detector clock, maintained by _sync_clock.  Seeded here
        #: so that a flush triggered before any sample has been fed still converts,
        #: rather than depending on feed() having run at least once.
        self._clock_offset: float = time.time() - time.monotonic()

        self.last_dose: Optional[WeighEvent] = None
        self.last_yield: Optional[WeighEvent] = None
        self.last_pair: Optional[BrewPair] = None
        # Monotonic count of completed pairs.  Exists to give alerting something
        # unambiguous to trigger on: "the ratio changed" misses two shots in a row
        # that happen to land on the same ratio, and "the yield changed" fires for
        # anything at all put on the scale — it emailed about a bowl of porridge.
        self.brew_count: int = 0
        # Odometer + trip meter.  brew_count stays the sole owner of the brew count;
        # see BrewTotals for why the trip figures are derived rather than counted.
        self.totals = BrewTotals()
        #: Brews that were measured on the refractometer, oldest first.
        self.measurements: list[BrewMeasurement] = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            # Nothing stored: this is a first install, so the period starts now rather
            # than at the epoch.  Done here as well as below so that the two paths
            # cannot disagree about what an unstarted period means.
            self.totals.period_started = time.time()
            return
        # Every stored field is restored independently, and that is the whole point
        # of the four calls below rather than one try block around all of them.
        #
        # They used to share a block, in the order last_dose, last_yield, last_pair,
        # brew_count.  A stored dict that no longer matches its dataclass raises
        # TypeError — one field added to BrewPair in a future version is enough —
        # and the handler then skipped everything after it, so a stale last_pair
        # took brew_count down with it and it came back as 0.  brew_count feeds a
        # TOTAL_INCREASING sensor: Home Assistant reads a drop to zero as a counter
        # reset, writes that into long-term statistics, and nothing afterwards can
        # undo it.  Losing a last_pair is cosmetic and self-healing — the next shot
        # replaces it — while losing the count is permanent, so the field with the
        # worst failure must not depend on the field most likely to fail.
        self.last_dose = self._restore(WeighEvent, data, "last_dose")
        self.last_yield = self._restore(WeighEvent, data, "last_yield")
        self.last_pair = self._restore(BrewPair, data, "last_pair")
        self.brew_count = self._restore_count(data)
        # Its own call for the same reason as brew_count's, and field-by-field rather
        # than through _restore: the odometer of ground coffee cannot be rebuilt from
        # anything else.  last_dose and last_pair are replaced by the next shot, so
        # dropping one of those wholesale on a TypeError is cheap; dropping the
        # odometer is permanent, and one field added to BrewTotals in a later version
        # would be enough to do it.  _restore_totals keeps whatever it recognises.
        self.totals = self._restore_totals(data)
        # Per-record rather than all-or-nothing, and for the same reason the fields
        # above are restored separately: these are the only copy.  A refractometer
        # reading cannot be recovered from anywhere else — the R2 keeps its own log,
        # but nothing here can re-associate it with the brew it belonged to — so one
        # record written by a future version must not discard the months before it.
        self.measurements = self._restore_measurements(data)
        if not self.totals.period_started:
            # Either a first run, or state written before this field existed.  Starting
            # the period now is the only defensible reading: the alternative — leaving
            # it at 0.0 — makes elapsed_days ~20000 and publishes a daily average of
            # zero indefinitely, which looks like a working sensor reporting no coffee.
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
        # A fresh period rather than BrewTotals(): this only runs when the config entry
        # is deleted, and the next install must not inherit a period that started
        # before it existed.  async_load reseeds it too, but only when there is no
        # stored state to load — and there is none precisely because of the line below.
        self.totals = BrewTotals(period_started=time.time())
        self.measurements = []
        await self._store.async_remove()

    def record_measurement(self, tds: float) -> Optional[BrewMeasurement]:
        """Attach a refractometer reading to the most recent brew.

        Always the most recent one, with no time window — measuring is something you
        do after pulling a shot, and a window would only ever be a guess about how
        long you took to get round to it.

        Re-measuring replaces that brew's point rather than adding a second: the rule
        is one reading per brew, and a stirred-and-remeasured sample is a correction,
        not a second cup.  A reading taken before any brew was ever detected has
        nothing to attach to and is dropped.
        """
        if self.last_pair is None:
            _LOGGER.info(
                "Refractometer read %.2f%% with no brew to attach it to; ignoring", tds
            )
            return None

        point = BrewMeasurement.build(self.last_pair, tds, time.time())
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
            "Brew measured: %.1f g in, %.1f g out, TDS %.2f%%, extraction %.2f%% (%s)",
            point.dose, point.yield_g, point.tds, point.ext,
            "re-measured" if replaced else "new",
        )
        self._save()
        for callback in list(self._listeners):
            callback()
        return point

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

    # ── clocks ───────────────────────────────────────────────────────────────
    #
    # There are two of them here on purpose, and this must not be "simplified" back
    # into one.  They answer different questions and neither can do the other's job.
    #
    # The detector needs a clock that only ever moves forward, at one second per
    # second.  Every rule in brew_detect is written as elapsed time — the trailing
    # stability window, the hold thresholds, the gap reset — and _trim_window drops
    # samples older than ``now - stable_seconds``.  Feed it a ``now`` that steps
    # *backwards* and the cutoff steps back with it, so nothing is ever popped and
    # the window grows without bound.  That is not hypothetical: this ran on
    # time.time(), and an NTP correction of -600 s — routine on a Raspberry Pi that
    # has just come back from a power cut, which is what this box is — took the
    # window from 16 samples to 317 over sixty seconds of stream and it was still
    # climbing.  _time_within_tol is O(window) per sample at 5 Hz, so the cost grows
    # quadratically for as long as the wall clock takes to catch up.  A *forward*
    # jump was always handled correctly, because it looks like a long hole in the
    # stream and trips the gap-reset path — which is the other half of the argument:
    # that reset is only meaningful if a gap in ``t`` is real elapsed time, and
    # wall-clock deltas are not.
    #
    # But the timestamps that come back out of the detector are read by people.
    # BrewPair.dose_at / yield_at and WeighEvent.at become entity attributes that
    # sensor.py renders as ISO timestamps on the dashboard and in the notification
    # email, and all three are written through Store, where they have to still name
    # the same instant in the next process.  time.monotonic() counts from an
    # arbitrary epoch — uptime, on Linux — that changes at every reboot, so a
    # monotonic timestamp is meaningless the moment it leaves this process, and a
    # persisted one is actively wrong.
    #
    # So: monotonic goes in, wall clock comes out, and the offset between the two is
    # the only thing that has to be carried.  Nothing monotonic is ever persisted or
    # handed to a consumer — see _to_wall_clock, which is the single place the two
    # meet.

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

    # ── the hot path ─────────────────────────────────────────────────────────

    def feed(self, weight: float, flow: float) -> None:
        """Called from the BLE notification handler, ~5 times per second.

        Must stay cheap and must never raise: an exception here would break sensor
        updates for the whole device.
        """
        try:
            # _sync_clock, not time.time(): brew_detect.Sample documents ``t`` as
            # "seconds, monotonic within a session" and means it.  See the clocks
            # section above for what a backwards wall-clock step did to the window.
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
        # `plateau` arrives on the detector's monotonic clock and the pairer has to
        # see it that way: BrewPairer.offer measures this weighing's t_start from the
        # pending dose's t_end and checks it against pair_window_seconds, and both
        # sides must be on one clock.  So offer the original first and convert only
        # what comes back out — from here down everything is wall-clock and is on its
        # way out of the session.
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

        # "unknown", not 0.0 and not the empty string: the whole point of the three
        # states is that somebody reading this line can tell a load that was set down
        # in a single sample (rise 0.0 s) from one whose arrival was never observed,
        # and %.1f can express only the first of those — it raises on the second.
        # Formatted eagerly rather than passed lazily because this runs once per
        # weighing, not once per sample.
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
            self.brew_count += 1
            # Only paired doses count towards the odometer, so it and brew_count always
            # describe the same population and total_dose_g / brew_count is the average
            # dose.  A dose with no pour after it is an aborted brew or a misdetection;
            # either way no cup came of it, and adding it would make the two disagree
            # by an amount nothing records.
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
            # Snapshot the window here, on the event loop, and hand the copy to the
            # writer.  _recent is appended to and pop(0)'d from by _remember on every
            # sample, i.e. five times a second, and the writer used to iterate the
            # live list from an executor thread.  CPython will not crash on that, but
            # a list that shifts under a comprehension silently skips or truncates
            # elements — so the recorded window could come back missing exactly the
            # samples around the plateau it exists to document, and nothing would
            # indicate the file was short.
            #
            # Filtering here too keeps the copy to the ~70 s that gets written rather
            # than the full 180 s buffer.
            #
            # The filter compares the *unconverted* plateau against the samples'
            # own timestamps, because both are on the detector's monotonic clock;
            # `reported` must not be used here, as mixing the two clocks in one
            # comparison is a difference of decades and would select nothing.  The
            # timestamps that get written out are converted, because the file exists
            # to be read next to recorder history and the log, and those are
            # wall-clock — a monotonic column could not be lined up against either,
            # and every line already in an existing file is wall-clock.
            window = [
                [round(self._to_wall_clock(s.t), 3), s.weight, s.flow]
                for s in self._recent
                if plateau.t_start - 60.0 <= s.t <= plateau.t_end + 10.0
            ]
            # async_create_task, not a bare async_add_executor_job whose future is
            # dropped on the floor: Home Assistant waits for tracked tasks while
            # shutting down, so a stop that lands mid-write still gets the row out.
            # Not async_create_background_task — those are cancelled at shutdown,
            # which is the case this is meant to survive.
            self.hass.async_create_task(
                self.hass.async_add_executor_job(
                    self._append_dataset, kind, reported, window
                ),
                f"{DOMAIN}_append_brew_dataset",
            )

        self._save()
        for callback in list(self._listeners):
            callback()

    # ── persistence ──────────────────────────────────────────────────────────

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
            # ``null``, not 0, when the arrival was never observed.  This is analysis
            # data and the two are different findings — a row claiming a rise of zero
            # is a row claiming the load was set down out of a container, which is the
            # single feature that separates a dose from a portafilter.  Writing 0 here
            # would put that lie into every offline study of the file, including the
            # ones used to tune dose_min_rise_seconds itself.
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
