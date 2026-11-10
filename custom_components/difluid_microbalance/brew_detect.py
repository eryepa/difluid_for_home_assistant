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
    t: float
    weight: float
    flow: float = 0.0

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
    rise_seconds: Optional[float] = None
    peak_flow: float = 0.0
    steps: tuple[float, ...] = ()
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
    pour_seconds: Optional[float] = None

    @property
    def ratio(self) -> float:
        return self.yield_g / self.dose

@dataclass
class DetectorConfig:
    stable_tol: float = 0.3
    stable_seconds: float = 3.0
    stable_time_fraction: float = 0.9
    hampel_window: int = 5
    hampel_sigmas: float = 3.0

    settle_min_seconds: float = 1.0

    min_mass: float = 5.0
    max_mass: float = 500.0

    dose_min: float = 12.0
    dose_max: float = 25.0
    yield_min: float = 25.0
    yield_max: float = 80.0

    dose_min_hold_seconds: float = 3.0

    dose_min_rise_seconds: float = 2.0

    pair_window_seconds: float = 1800.0
    ratio_min: float = 1.2
    ratio_max: float = 6.0

    gap_reset_seconds: float = 15.0

    tare_quiet_seconds: float = 0.5

    zero_band: float = 1.0

TUNABLE_FIELDS = (
    "stable_tol",
    "stable_seconds",
    "stable_time_fraction",
    "settle_min_seconds",
    "min_mass",
    "dose_min",
    "dose_max",
    "dose_min_hold_seconds",
    "dose_min_rise_seconds",
    "yield_min",
    "yield_max",
    "pair_window_seconds",
    "ratio_min",
    "ratio_max",
    "gap_reset_seconds",
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

SECONDS_PER_DAY = 86400.0

@dataclass
class BrewTotals:
    """Cumulative brew statistics: an odometer, a trip meter, and a daily rate.

    Lives here rather than in brew_session for the same reason everything else in this
    module does — no Home Assistant imports, so the arithmetic can be exercised offline
    in test_detector.py instead of only by watching the dashboard for a week.

    Only the *odometers* are stored.  The trip figures are derived by subtracting a
    snapshot taken at the last reset, and that is the whole design: a second counter
    with its own ``+= 1`` can fall out of step with the first — one missed event, one
    field that failed to restore — and once it has, nothing brings the two back
    together.  Subtraction cannot disagree with the number it subtracts from.  It is
    also literally how a car's trip meter works, which is what was asked for.

    The count of brews is deliberately *not* a field here.  BrewSession.brew_count
    already owns it and feeds a TOTAL_INCREASING sensor whose accidental reset is
    permanent (see BrewSession.async_load); a copy kept alongside would be a second
    answer to a question that must only have one.  The methods that need it take it as
    an argument.
    """

    total_dose_g: float = 0.0
    period_started: float = 0.0
    brews_at_period_start: int = 0
    dose_at_period_start: float = 0.0

    def add(self, dose_g: float) -> None:
        """Record one completed brew's dose against the odometer.

        Rounded on every step rather than once at the end: the odometer is written to
        Store and read back thousands of times over its life, and unrounded float
        addition would let it drift into 1234.5600000000002 and publish that.
        """
        self.total_dose_g = round(self.total_dose_g + float(dose_g), 2)

    def reset_period(self, now: float, brew_count: int) -> None:
        """Start a new period — the trip-meter button.

        Snapshots the odometers instead of zeroing anything, so a reset can never cost
        a brew: the all-time figures are untouched by construction, not by care.
        """
        self.period_started = now
        self.brews_at_period_start = brew_count
        self.dose_at_period_start = self.total_dose_g

    def period_brews(self, brew_count: int) -> int:
        """Brews since the last reset.

        Clamped at zero.  The subtraction can only go negative if the stored snapshot
        outruns the counter — a half-restored Store, or a brew_count that was reset to
        0 by the recovery path in BrewSession.  A negative count on a TOTAL_INCREASING
        sensor is not a number anyone can act on, and zero at least says "the period
        has nothing in it yet", which after that kind of failure is true.
        """
        return max(0, brew_count - self.brews_at_period_start)

    def period_dose_g(self) -> float:
        return max(0.0, round(self.total_dose_g - self.dose_at_period_start, 2))

    def elapsed_days(self, now: float) -> float:
        """Length of the current period in days, floored at one.

        The floor is what keeps the first morning honest.  Two cups three hours after a
        reset is 0.125 days, and dividing by that publishes "16 brews per day" — an
        extrapolation from a single morning, printed with the same authority as a
        figure averaged over a month.  Flooring reports 2/day on day one and converges
        to the truth as the period grows, which is the direction an average should
        approach from.
        """
        return max((now - self.period_started) / SECONDS_PER_DAY, 1.0)

    def per_day(self, value: float, now: float) -> float:
        """`value` averaged over the current period."""
        return round(value / self.elapsed_days(now), 2)

    STORED_FIELDS = {
        "total_dose_g": float,
        "period_started": float,
        "brews_at_period_start": int,
        "dose_at_period_start": float,
    }

    @classmethod
    def from_stored(cls, stored: object) -> tuple["BrewTotals", list[tuple[str, object]]]:
        """Rebuild from a stored dict, keeping every field that can still be read.

        Deliberately forgiving, field by field, rather than ``cls(**stored)``: an
        unknown key from a newer version, a missing key from an older one, or a single
        unparseable value costs only that field instead of the whole odometer.  The
        asymmetry against the other stored records is the point — for last_dose,
        "drop it and wait for the next shot" is a complete recovery; for a cumulative
        total it is not a recovery at all, and one field added in a later version
        would otherwise be enough to wipe it.

        Returns the totals plus the fields that could not be read, so the caller can
        log them.  Reporting rather than logging keeps this module free of Home
        Assistant and free of logging policy, and makes the behaviour testable.
        """
        totals = cls()
        problems: list[tuple[str, object]] = []
        if not isinstance(stored, dict):
            return totals, problems
        for name, coerce in cls.STORED_FIELDS.items():
            if name not in stored:
                continue
            try:
                setattr(totals, name, coerce(stored[name]))
            except (TypeError, ValueError):
                problems.append((name, stored[name]))
        return totals, problems

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
        self._last_ring_t: float = float("-inf")

    def reset(self) -> None:
        """Drop all state — call on BLE disconnect or a gap in the stream."""
        self._raw.clear()
        self._window.clear()
        self._in_plateau = False
        self._steps = []
        self._last_zero_t = None
        self._last_t = None
        self._last_ring_t = float("-inf")

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
        threshold = max(self.cfg.hampel_sigmas * 1.4826 * spread, self.cfg.stable_tol)
        if abs(sample.weight - centre) > threshold:
            if self._is_ring(sample, centre):
                self._last_ring_t = sample.t
            return None
        return sample

    def _is_ring(self, rejected: Sample, centre: float) -> bool:
        """Is this rejected sample the cell ringing, or just the step to a new value?

        Both get rejected, and only one of them is evidence of anything.  A Hampel
        filter judges a sample against the spread of its neighbours, so on a settled
        scale — where the spread is nil — it rejects *any* change, including a
        perfectly clean step down to zero.  It keeps rejecting until the new value
        fills its five-sample window.

        That matters because _is_tare asks how long the cell has been quiet, and
        counting those rejections made the answer depend on the sample rate rather
        than on the signal: at 5 Hz five samples take 0.4 s, so the "quiet" was over
        before it began and every tare read as a removal.  That is the whole of the
        2026-08-15 ratio 1:2.35 — a tared 42.8 g cup reported as the pour.

        Ringing is not just any excursion, it is an excursion *outside the two
        levels*.  A cell settling from 42.8 g to zero passes through the values in
        between and stops; a cup coming off swings hundreds of grams past both ends
        (-583 g and +737 g on the same morning).  So the band from zero to wherever
        the reading was is innocent, and anything beyond it is the cell ringing.
        """
        cfg = self.cfg
        low = min(0.0, centre) - cfg.zero_band
        high = max(0.0, centre) + cfg.stable_tol
        return not low <= rejected.weight <= high

    def _is_tare(self, clean: Sample) -> bool:
        """True when this zero is the scale being re-zeroed, not the load leaving it.

        The two are identical in shape — a settled reading, then zero — and the
        whole weighing model rests on zero meaning "the scale is empty".  Taring
        breaks that: on 2026-08-14 an empty cup read 64.4 g, was tared, and that
        64.35 g "weighing" paired with the morning's dose and emailed 1:3.56.

        Two things separate them and both must hold.

        A tare lands on *exactly* zero, because that is what taring means.  A load
        coming off lands wherever the empty scale happens to sit — here -35 g with
        the portafilter away — and usually hundreds of grams out first.

        And a tare is silent.  A load cannot leave a scale without the cell
        ringing, so there is always an excursion past the two levels just before a
        real removal and none at all before a tare.  Across both recorded tares the
        last ring was 2.2 s and 2.6 s old; at the 19:15 lift-off, 0.2 s.

        "Ring" and not "rejected sample" — see _is_ring for why the difference is
        the whole rule rather than a refinement of it.
        """
        cfg = self.cfg
        if abs(clean.weight) > cfg.stable_tol:
            return False
        if clean.t - self._last_ring_t < cfg.tare_quiet_seconds:
            return False
        return bool(self._window) and self._window[-1].weight >= cfg.min_mass

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

    def feed(self, sample: Sample) -> Optional[Plateau]:
        cfg = self.cfg

        if self._last_t is not None and sample.t - self._last_t > cfg.gap_reset_seconds:
            self.reset()
        self._last_t = sample.t

        clean = self._despike(sample)
        if clean is None:
            return None

        if abs(clean.weight) > cfg.max_mass:
            self._end_step(clean.t)
            self._settle_from_window(clean.t)
            closed = self._close_weighing()
            self._window.clear()
            return closed

        if clean.weight <= cfg.zero_band and self._is_tare(clean):
            self._in_plateau = False
            self._steps = []
            self._last_zero_t = clean.t
            self._window.clear()
            self._window.append(clean)
            return None

        if clean.weight <= cfg.zero_band:
            self._end_step(clean.t)
            self._settle_from_window(clean.t)
            closed = self._close_weighing()
            self._last_zero_t = clean.t
            self._window.clear()
            self._window.append(clean)
            return closed

        self._window.append(clean)
        self._trim_window(clean.t)

        window_start = clean.t - cfg.stable_seconds
        if self._window[0].t > window_start:
            return None

        centre = median([s.weight for s in self._window])
        held = self._time_within_tol(centre, window_start, clean.t)
        held_fraction = held / cfg.stable_seconds
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
        rise: Optional[float] = None
        if self._last_zero_t is not None:
            rise = max(0.0, self._plateau_start - self._last_zero_t)
        self._steps.append(
            Plateau(
                value=self._plateau_value,
                t_start=self._plateau_start,
                t_end=max(self._plateau_last_t, end_t) if end_t else self._plateau_last_t,
                rise_seconds=rise,
                peak_flow=self._plateau_peak_flow,
            )
        )

    def _settle_from_window(self, end_t: float) -> None:
        """Recover the reading the scale was showing when the load came off.

        Only runs when the weighing is about to close with nothing banked at all.
        A step needs stable_seconds of held reading before it counts, and a cup
        lifted the moment the pour stops never provides one — on 2026-08-13 a
        perfectly ordinary shot reached 37.6 g, was picked up half a second later,
        and the entire cycle was reported as nothing at all.

        Removal is itself evidence that the weighing was finished, so the question
        is not whether to report but what value to report.  Walking back from the
        last clean sample gives it: the run of readings that agree with the final
        one, which is the pour after the flow stopped and before the lift.  It has
        to be a run and not a single sample, or a value caught mid-rise would
        qualify.

        Deliberately not a lower `stable_seconds`.  A confirmed step keeps its full
        three seconds of evidence; this weaker rule applies only where the
        alternative is silence.

        Earlier steps do not switch this off.  They used to, and that quietly undid
        the rule for exactly the weighings it was written for: Plateau.value is "the
        reading that stood when the load was removed", which only follows from the
        last banked step if every increment stood for a full stable_seconds, and the
        last one is the one most likely not to have.  A pour that reached 36.0 g,
        stood five seconds, took the last drops to 38.4 g and was lifted 1.4 s later
        reported 36.0 — while the identical waveform with nothing banked before it
        reported 38.4.  The bias runs one way only, because a cup never gets lighter
        while it settles.

        What the steps gate was really guarding against is the recovery handing back
        a reading that has already been banked, which would append the same value
        twice and count one settling as two.  That is a question about the value, not
        about whether any step exists: a run that agrees with the last step is the
        same reading seen again, and a run meaningfully above it is the load having
        grown since — new evidence by the same argument that makes removal evidence.
        Meaningfully means stable_tol, the width at which this detector calls
        anything movement at all.
        """
        cfg = self.cfg
        if self._in_plateau or len(self._window) < 2:
            return

        samples = list(self._window)
        centre = samples[-1].weight
        first = len(samples) - 1
        while first > 0 and abs(samples[first - 1].weight - centre) <= cfg.stable_tol:
            first -= 1
        run = samples[first:]

        if run[-1].t - run[0].t < cfg.settle_min_seconds:
            return

        value = median([s.weight for s in run])
        if value < cfg.min_mass:
            return
        if self._steps and value <= self._steps[-1].value + cfg.stable_tol:
            return

        self._in_plateau = True
        self._plateau_value = value
        self._plateau_start = run[0].t
        self._plateau_last_t = run[-1].t
        self._plateau_peak_flow = max(abs(s.flow) for s in run)
        self._end_step(end_t)

    def _close_weighing(self) -> Optional[Plateau]:
        """Fold the banked steps into the one result for this weighing."""
        steps = self._steps
        self._steps = []
        if not steps:
            return None
        last = steps[-1]
        return Plateau(
            value=last.value,
            t_start=steps[0].t_start,
            t_end=last.t_end,
            rise_seconds=steps[0].rise_seconds,
            peak_flow=max(s.peak_flow for s in steps),
            steps=tuple(round(s.value, 2) for s in steps),
            final_hold_seconds=last.duration,
        )

    def flush(self) -> Optional[Plateau]:
        """Close any open weighing — end of a replay file, or a clean shutdown.

        No settle recovery here, nor on a stream gap: both mean the stream stopped,
        which is not evidence that the load was ever taken off the scale.
        """
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
        if pending_dose is not None:
            return "yield"
        if plateau.duration >= 30.0 and plateau.peak_flow < 0.5:
            return "dose"
        return "yield"
    return "other"

def _arrival_rank(plateau: Plateau, cfg: DetectorConfig) -> int:
    """Score how well this load's arrival argues that it is the dose.

    Three states, not two, because ``rise_seconds`` has three:

      2  ground on — the load accumulated over dose_min_rise_seconds or more, which
         is what a grinder running onto the scale looks like and nothing else does.
      1  unknown — no zero was observed before it, so how it arrived was never seen.
      0  set down — the scale was observed empty and the load was there in a single
         sample, so it came out of a container.

    Unknown sits between them because it is the absence of evidence and the other two
    are evidence pointing opposite ways.  Ranking it with "set down", which is what
    collapsing None to 0.0 used to do, turns silence into an accusation: a dose the
    detector picked up mid-session would be scored as a portafilter.  Ranking it with
    "ground on" would do the reverse and let a cup that happened to be sitting there
    at startup outrank beans we actually watched accumulate.

    So a known-ground candidate still beats an unknown one, an unknown one still
    beats a known set-down one, and two candidates in the same state are left to the
    hold-time tiebreak below — which is a weak rule, but between two loads we know
    equally little about it is the only one left.
    """
    if plateau.rise_seconds is None:
        return 1
    return 2 if plateau.rise_seconds >= cfg.dose_min_rise_seconds else 0

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
            previous = self._pending_dose
            stale = (
                previous is not None
                and plateau.t_start - previous.t_end > cfg.pair_window_seconds
            )
            if previous is None or stale:
                self._pending_dose = plateau
            else:
                rank = _arrival_rank(plateau, cfg)
                rank_before = _arrival_rank(previous, cfg)
                if rank != rank_before:
                    if rank > rank_before:
                        self._pending_dose = plateau
                elif plateau.duration > previous.duration:
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
                        pour_seconds=(
                            None
                            if plateau.rise_seconds is None
                            else round(plateau.rise_seconds, 1)
                        ),
                    )
            self._pending_dose = None

        return kind, None

    def reset(self) -> None:
        self._pending_dose = None
