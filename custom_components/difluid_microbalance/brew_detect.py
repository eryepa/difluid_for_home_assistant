"""Weighing detection over the scale's weight stream.

The unit of interest is a weighing — from the scale being empty to the load coming
off again — not a stable reading.  Anything put on the scale in more than one go
produces several stable readings and exactly one weighing, and it is the weighing
that has a meaning: 30 g of oats poured from two packets, a shot that settles as the
crema drops.  Reporting stable readings instead invents events and reports
intermediate values, which is what once emailed "coffee, ratio 1:1.27" about a bowl
of porridge.

Pure logic — no Home Assistant imports.  That is deliberate: the same module runs
inside the coordinator and inside ``tools/replay_brew.py`` against recorder history,
so thresholds tuned offline are the thresholds that run in production.

Everything is driven by timestamps rather than sample counts.  The BLE notification
rate is not constant (the proxy stalls and times out), so an "N samples" window
would silently stretch from 3 seconds to fifteen whenever the stream drops and would
merge unrelated events.  Recorder history is also change-point-only, so a
count-based detector would behave differently there than it does live.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Deque, Literal, Optional

PlateauKind = Literal["dose", "yield", "other"]


@dataclass(frozen=True)
class Sample:
    t: float  # seconds, monotonic within a session
    weight: float  # grams
    flow: float = 0.0  # grams/second


@dataclass(frozen=True)
class Plateau:
    """One complete weighing — everything that happened between the scale being
    empty and the load leaving it again.

    Not one stable reading.  A single weighing routinely settles more than once:
    the beans run out mid-pour and the packet is topped up, or the last drops of
    a shot land while the crema settles.  Each of those produces its own stable
    reading, and reporting them separately is wrong twice over — it invents events
    that never happened, and the value it reports is an intermediate one.

    ``value`` is therefore the reading that stood when the load was removed, and
    ``steps`` records the ladder that led to it.
    """

    value: float
    t_start: float
    t_end: float
    rise_seconds: float
    peak_flow: float
    #: Every stable reading in this weighing, oldest first; the last is ``value``.
    steps: tuple[float, ...] = ()
    #: How long the final reading itself stood, as opposed to the whole weighing.
    final_hold_seconds: float = 0.0

    @property
    def duration(self) -> float:
        """Seconds from the first stable reading to the load being removed."""
        return self.t_end - self.t_start


@dataclass(frozen=True)
class BrewPair:
    dose: float
    dose_at: float
    yield_g: float
    yield_at: float

    @property
    def ratio(self) -> float:
        return self.yield_g / self.dose


@dataclass
class DetectorConfig:
    # Stability
    stable_tol: float = 0.3  # grams around the window median
    stable_seconds: float = 3.0  # length of the trailing window
    stable_time_fraction: float = 0.9  # share of that window that must stay within tol
    hampel_window: int = 5  # samples in the spike prefilter
    hampel_sigmas: float = 3.0

    # Plausible masses
    min_mass: float = 5.0
    max_mass: float = 500.0

    # Classification
    dose_min: float = 12.0
    dose_max: float = 25.0
    yield_min: float = 25.0
    yield_max: float = 80.0

    # A floor against momentary taps only.  It is deliberately not the thing that
    # tells beans from a portafilter: measured over reconstructed streams the real
    # dose stayed 180 s but incidental placements ran 7 s, 17 s and 126 s, so the
    # two populations overlap and no absolute cutoff separates them.  BrewPairer
    # compares candidates within a cycle instead.  No equivalent floor applies to
    # the pour: it is held only as long as it takes to lift the cup (5 s in one
    # recorded shot).
    dose_min_hold_seconds: float = 10.0

    # Pairing
    pair_window_seconds: float = 1800.0
    ratio_min: float = 1.2
    ratio_max: float = 6.0

    # Stream discontinuity
    gap_reset_seconds: float = 15.0

    # At or below this the scale is empty and the weighing is over.  Compared
    # signed, not absolute: taking a tared container off reads well below zero
    # (-35 g for the portafilter here), and that is a removal just as much as 0 is.
    zero_band: float = 1.0


#: Fields the options flow exposes.  Kept next to the dataclass so the two cannot
#: drift apart.
TUNABLE_FIELDS = (
    "stable_tol",
    "stable_seconds",
    "stable_time_fraction",
    "min_mass",
    "dose_min",
    "dose_max",
    "dose_min_hold_seconds",
    "yield_min",
    "yield_max",
    "pair_window_seconds",
    "ratio_min",
    "ratio_max",
)


def config_from_options(options: dict) -> DetectorConfig:
    """Build a DetectorConfig from stored options, falling back to its defaults.

    Unparseable values are ignored rather than raising: a bad option must not stop
    the integration from loading.
    """
    cfg = DetectorConfig()
    for key in TUNABLE_FIELDS:
        if key in options:
            try:
                setattr(cfg, key, float(options[key]))
            except (TypeError, ValueError):
                pass
    return cfg


def _mad(values: list[float], centre: float) -> float:
    """Median absolute deviation — the scale-free spread Hampel needs."""
    return median([abs(v - centre) for v in values])


class BrewDetector:
    """Streaming weighing detector.

    Feed samples in chronological order; each call returns a ``Plateau`` — one
    complete weighing — at the moment the load leaves the scale, otherwise ``None``.

    Two levels are at work.  Internally the detector finds stable readings, as it
    always did.  Those are accumulated into the current weighing instead of being
    reported, and only the removal of the load produces a result.  Which reading is
    stable is a question about the signal; which weighing it belongs to is a
    question about what the scale was being used for, and the two have different
    answers whenever something is added in more than one go.
    """

    def __init__(self, config: Optional[DetectorConfig] = None) -> None:
        self.cfg = config or DetectorConfig()
        self._raw: Deque[Sample] = deque(maxlen=max(3, self.cfg.hampel_window))
        self._window: Deque[Sample] = deque()
        self._in_plateau = False
        self._plateau_value = 0.0
        self._plateau_start = 0.0
        self._plateau_last_t = 0.0
        self._plateau_peak_flow = 0.0
        self._steps: list[Plateau] = []
        self._last_zero_t: Optional[float] = None
        self._last_t: Optional[float] = None

    def reset(self) -> None:
        """Drop all state — call on BLE disconnect or a gap in the stream."""
        self._raw.clear()
        self._window.clear()
        self._in_plateau = False
        self._steps = []
        self._last_zero_t = None
        self._last_t = None

    # ── spike prefilter ──────────────────────────────────────────────────────

    def _despike(self, sample: Sample) -> Optional[Sample]:
        """Hampel filter: replace a sample that sits far outside the local median.

        This is what actually absorbs the ±300…1300 g transients thrown when a cup
        is lifted off the scale.  ``max_mass`` below is only a second line of
        defence, not the primary mechanism.
        """
        self._raw.append(sample)
        if len(self._raw) < self._raw.maxlen:
            return sample

        weights = [s.weight for s in self._raw]
        centre = median(weights)
        spread = _mad(weights, centre)
        # 1.4826 converts MAD to a standard-deviation-equivalent for normal data.
        #
        # The floor is what makes this work on a settled scale.  A perfectly steady
        # reading has a MAD of exactly zero, and a threshold of zero times anything
        # is still zero — so the filter used to give up precisely when the signal was
        # cleanest.  That is how a single 0.0 g glitch, gone again in 0.28 s, cut a
        # bowl of oats in half and turned it into a dose and a pour.  Below
        # stable_tol the movement would not count as movement anyway.
        threshold = max(self.cfg.hampel_sigmas * 1.4826 * spread, self.cfg.stable_tol)
        if abs(sample.weight - centre) > threshold:
            return None
        return sample

    # ── stability over a time window ─────────────────────────────────────────

    def _time_within_tol(self, centre: float, start: float, end: float) -> float:
        """Seconds in [start, end] during which the value held within tol.

        Each sample holds its value until the next one (zero-order hold), so a
        0.2 s blip contributes 0.2 s while a 3 s hold contributes 3 s.  Plain
        max-min would let a single bad sample veto an otherwise clean plateau.
        """
        if len(self._window) < 2:
            return 0.0
        good = 0.0
        samples = list(self._window)
        for prev, nxt in zip(samples, samples[1:]):
            # Clip to the window: the oldest sample usually starts before it.
            lo = max(prev.t, start)
            hi = min(nxt.t, end)
            if hi <= lo:
                continue
            if abs(prev.weight - centre) <= self.cfg.stable_tol:
                good += hi - lo
        return good

    def _trim_window(self, now: float) -> None:
        """Drop samples the window no longer needs.

        One sample at or before the cutoff is kept: under zero-order hold it is
        what defines the value at the start of the interval.  Dropping it would
        make the window shorter than stable_seconds and the length check below
        could then never pass.
        """
        cutoff = now - self.cfg.stable_seconds
        while len(self._window) > 1 and self._window[1].t <= cutoff:
            self._window.popleft()

    # ── main entry point ─────────────────────────────────────────────────────

    def feed(self, sample: Sample) -> Optional[Plateau]:
        cfg = self.cfg

        if self._last_t is not None and sample.t - self._last_t > cfg.gap_reset_seconds:
            # A gap this long means the stream broke; nothing before it is comparable.
            self._end_step()
            closed = self._close_weighing()
            self.reset()
            self._last_t = sample.t
            return closed
        self._last_t = sample.t

        clean = self._despike(sample)
        if clean is None:
            return None

        if abs(clean.weight) > cfg.max_mass:
            # Nothing in this workflow legitimately weighs this much, so the cup is
            # being lifted off. That ends the weighing — close it and hand it over.
            # Returning None here instead would lose the reading entirely: the
            # Hampel filter absorbs the first samples of the lift-off transient, so
            # this is often the only notice the detector gets that the pour is over.
            self._end_step(clean.t)
            return self._close_weighing()

        if clean.weight <= cfg.zero_band:
            # The scale is empty again, so whatever was on it has been taken off and
            # the weighing is finished.  This — not a stable reading — is what ends
            # one: a reading that stops changing may simply be a pause while the next
            # packet is opened.
            self._last_zero_t = clean.t
            self._end_step(clean.t)
            closed = self._close_weighing()
            self._window.append(clean)
            self._trim_window(clean.t)
            return closed

        self._window.append(clean)
        self._trim_window(clean.t)

        window_start = clean.t - cfg.stable_seconds
        if self._window[0].t > window_start:
            # Not enough history yet to judge stability over a full window.
            return None

        centre = median([s.weight for s in self._window])
        held = self._time_within_tol(centre, window_start, clean.t)
        held_fraction = held / cfg.stable_seconds
        # Signed, not absolute: a steady negative reading is the scale sitting below
        # its tare with the container removed, never something being weighed.
        stable = held_fraction >= cfg.stable_time_fraction and centre >= cfg.min_mass

        if stable:
            if not self._in_plateau:
                self._in_plateau = True
                self._plateau_value = centre
                self._plateau_start = self._window[0].t
                self._plateau_peak_flow = 0.0
            self._plateau_value = centre
            self._plateau_last_t = clean.t
            self._plateau_peak_flow = max(self._plateau_peak_flow, abs(clean.flow))
            return None

        if self._in_plateau and abs(clean.weight - self._plateau_value) > cfg.stable_tol:
            # The reading moved on, but the load is still on the scale — more was
            # added, or it is still settling.  Bank the step and keep the weighing
            # open; only removal ends it.
            self._end_step(clean.t)
        return None

    def _end_step(self, end_t: Optional[float] = None) -> None:
        """Bank the open stable reading as one step of the current weighing.

        `end_t` is the timestamp of the sample that ended it. Under zero-order hold
        the value was still being held right up to that moment, so that — not the
        last sample that happened to fall inside the plateau — is the true end.

        This matters because recorder history only stores change points: a value
        held for twenty seconds appears as one sample, then nothing until it moves.
        Measuring to the last in-plateau sample would report that as a fraction of a
        second offline while production, streaming at 5 Hz, measured the full hold —
        and thresholds tuned on replay would not match the ones that actually run.

        Pass nothing when the stream simply stopped (disconnect or a long gap):
        there is no evidence the value held through time we never observed.
        """
        if not self._in_plateau:
            return
        self._in_plateau = False
        rise = 0.0
        if self._last_zero_t is not None and self._plateau_start > self._last_zero_t:
            rise = self._plateau_start - self._last_zero_t
        self._steps.append(
            Plateau(
                value=self._plateau_value,
                t_start=self._plateau_start,
                t_end=max(self._plateau_last_t, end_t) if end_t else self._plateau_last_t,
                rise_seconds=rise,
                peak_flow=self._plateau_peak_flow,
            )
        )

    def _close_weighing(self) -> Optional[Plateau]:
        """Fold the banked steps into the one result for this weighing."""
        steps = self._steps
        self._steps = []
        if not steps:
            return None
        last = steps[-1]
        return Plateau(
            # What stood on the scale when it was taken off — 30 g of oats, not the
            # 23.7 g that was there when the first packet ran out; 38.4 g of coffee,
            # not the 38.1 g reached three seconds before the crema settled.
            value=last.value,
            t_start=steps[0].t_start,
            t_end=last.t_end,
            rise_seconds=steps[0].rise_seconds,
            peak_flow=max(s.peak_flow for s in steps),
            steps=tuple(round(s.value, 2) for s in steps),
            final_hold_seconds=last.duration,
        )

    def flush(self) -> Optional[Plateau]:
        """Close any open weighing — end of a replay file, or a clean shutdown."""
        self._end_step()
        return self._close_weighing()


def classify(
    plateau: Plateau, cfg: DetectorConfig, pending_dose: Optional[Plateau] = None
) -> PlateauKind:
    """Label a plateau by mass, falling back to context where ranges overlap."""
    v = plateau.value
    long_enough_for_dose = plateau.duration >= cfg.dose_min_hold_seconds
    in_dose = cfg.dose_min <= v <= cfg.dose_max and long_enough_for_dose
    in_yield = cfg.yield_min <= v <= cfg.yield_max

    if in_dose and not in_yield:
        return "dose"
    if in_yield and not in_dose:
        return "yield"
    if in_dose and in_yield:
        # Overlapping ranges: an unpaired dose already waiting means this is the pour.
        if pending_dose is not None:
            return "yield"
        # Otherwise fall back to shape — beans sit on the scale while grinding,
        # and no liquid is flowing while they do.
        if plateau.duration >= 30.0 and plateau.peak_flow < 0.5:
            return "dose"
        return "yield"
    return "other"


class BrewPairer:
    """Holds the most recent unpaired dose and matches the next plausible yield."""

    def __init__(self, config: Optional[DetectorConfig] = None) -> None:
        self.cfg = config or DetectorConfig()
        self._pending_dose: Optional[Plateau] = None

    @property
    def pending_dose(self) -> Optional[Plateau]:
        return self._pending_dose

    def offer(self, plateau: Plateau) -> tuple[PlateauKind, Optional[BrewPair]]:
        cfg = self.cfg
        kind = classify(plateau, cfg, self._pending_dose)

        if kind == "dose":
            # Keep the longest-held candidate, not simply the newest.
            #
            # Between weighing the beans and pulling the shot other things land on
            # the scale in the same mass range — the portafilter, a cup. Those are
            # brief; the beans sit there for the whole grind. Comparing holds within
            # one cycle separates them without an absolute cutoff, which matters
            # because the two populations overlap outright: over reconstructed
            # streams a real dose measured 180 s and incidental placements 7 s,
            # 17 s and 126 s.  Within one cycle the beans are still the longest.
            #
            # dose_min_hold_seconds still applies in classify() as a floor against
            # momentary taps; this only decides which qualifying candidate wins.
            previous = self._pending_dose
            stale = (
                previous is not None
                and plateau.t_start - previous.t_end > cfg.pair_window_seconds
            )
            if previous is None or stale or plateau.duration > previous.duration:
                self._pending_dose = plateau
            return kind, None

        if kind == "yield" and self._pending_dose is not None:
            dose = self._pending_dose
            gap = plateau.t_start - dose.t_end
            if 0 <= gap <= cfg.pair_window_seconds:
                ratio = plateau.value / dose.value if dose.value else 0.0
                if cfg.ratio_min <= ratio <= cfg.ratio_max:
                    self._pending_dose = None
                    return kind, BrewPair(
                        dose=dose.value,
                        dose_at=dose.t_end,
                        yield_g=plateau.value,
                        yield_at=plateau.t_end,
                    )
            # Stale or implausible — drop the dose rather than pair it wrongly.
            self._pending_dose = None

        return kind, None

    def reset(self) -> None:
        self._pending_dose = None
