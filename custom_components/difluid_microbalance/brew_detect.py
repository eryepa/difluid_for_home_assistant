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
    #: How long the load took to arrive, or ``None`` when that was never observed.
    #:
    #: The two are different findings and must not share a value.  A rise of zero is
    #: a positive observation — the scale was seen empty and the load was there in
    #: the next sample, so it was set down rather than ground on.  ``None`` means the
    #: detector never saw the scale empty before this weighing and so knows nothing
    #: about how the load got there: it was already sitting on the scale when the
    #: detector started, after an HA restart mid-session, after a BLE reconnect, or
    #: after a gap in the stream reset it.
    #:
    #: Recording the second as the first states the opposite of what happened, and
    #: BrewPairer.offer reads it as evidence.  That is how a portafilter set back on
    #: the scale beat the real dose on 2026-08-17.
    rise_seconds: Optional[float] = None
    peak_flow: float = 0.0
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

    # How long the reading must have stood still for the value it showed at the moment
    # of removal to count, when no full stable reading was ever established.  A cup
    # lifted as soon as the pour stops never gives stable_seconds of quiet: on
    # 2026-08-13 the shot reached 37.6 g and was picked up 0.5 s later, and the whole
    # brew went unreported.  One second is well past what any pour covers by accident
    # — at the 2.5 g/s observed here a second of flow is 2.5 g, eight times stable_tol.
    settle_min_seconds: float = 1.0

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
    #
    # Was 10 s until 2026-08-21, which is a floor against momentary taps in the
    # comment and something else entirely in practice.  That morning the beans were
    # weighed for 7.2 s — an ordinary weighing, nothing stalled or snatched — so the
    # 18.2 g was labelled "other", nothing was pending when the pour arrived, and a
    # complete brew was dropped without a trace.  The failure mode is the bad one:
    # silent, and indistinguishable from the scale simply not having been used.
    #
    # 3 s is the honest reading of what this field is for.  It is stable_seconds, the
    # shortest span that can become a plateau at all, so it still rejects a tap while
    # asserting nothing about how long a person chooses to leave beans on a scale.
    # Every hold above it is a candidate and the choosing happens in BrewPairer, which
    # is where the comment above already said it happens.
    #
    # The cost is real and worth naming: a 12-25 g object set down for seven seconds
    # is now a dose candidate where it used to be filtered out here.  Alone in a cycle
    # it can mispair, and _arrival_rank only sorts candidates against each other, it
    # never rejects a lone one.  That is the trade — a wrong ratio is visible in the
    # email and can be argued with, a dropped brew is not.  See short_dose.csv, which
    # is the only fixture that constrains this value: at 3, 5 or 10 s every other file
    # in testdata/ stays green.
    dose_min_hold_seconds: float = 3.0

    # How long a load must have taken to arrive for it to count as ground onto the
    # scale rather than set down on it.  Beans accumulate while the grinder runs;
    # anything already in a container lands in a single sample.  Measured across
    # every capture so far the two do not overlap at all — real doses rose over
    # 2.7 to 10.8 s, incidental placements over 0.0 to 1.3 s.
    #
    # This only ever picks between competing candidates, never rejects a lone one:
    # grinding off the scale and setting the dose down would give a rise near zero,
    # and that must still pair.
    dose_min_rise_seconds: float = 2.0

    # Pairing
    pair_window_seconds: float = 1800.0
    ratio_min: float = 1.2
    ratio_max: float = 6.0

    # Stream discontinuity: how long a hole has to be before the detector stops
    # believing the two sides of it belong to the same weighing.
    #
    # Left at 15 s even though the ESPHome proxies in this deployment are known to
    # stall for 20, which trips it every time.  That looks wrong until you price both
    # sides.  Tripping now costs only history — feed() discards the open weighing and
    # rebuilds it from the resumed stream, so the value and the removal are still
    # detected, and what is lost is the earlier steps and how the load arrived (which
    # is recorded as unknown, not invented).  Not tripping costs accuracy: nothing
    # calls _end_step across a gap, so the hold is measured straight through the
    # silence.  Raising this to 30 s and replaying the two gap fixtures turns a
    # 12.4 s pour into a 37.0 s one and a 15.2 s dose into a 46.8 s one — twenty
    # seconds of hold that nobody observed, fed straight into dose_min_hold_seconds
    # and into the hold-time tiebreak in BrewPairer.offer.
    #
    # Losing history is visible and bounded; inventing hold time is neither, and it
    # is the mistake this module's comments keep returning to.  So the default stays
    # conservative and the field is tunable instead, for anyone whose proxies are bad
    # enough that they would rather make the other trade knowingly.
    gap_reset_seconds: float = 15.0

    # How long the stream must have been free of the load cell ringing for a return
    # to zero to count as a tare rather than a removal.  See _is_tare and _is_ring.
    tare_quiet_seconds: float = 0.5

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
    # Exposed because the right value is a property of the user's BLE path, not of
    # coffee, and nothing in the integration can measure it for them: a proxy that
    # stalls longer than this loses the history of every weighing it interrupts, and
    # until now there was no way to say so.  See the field for what raising it costs.
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
        #: When the load cell was last seen ringing.  See _is_ring.
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
            #
            # The open weighing is discarded, not emitted.  flush() states the rule
            # this follows: a stopped stream "is not evidence that the load was ever
            # taken off the scale", and a weighing is by definition everything from
            # the scale being empty to the load leaving it.  Handing out what had
            # accumulated so far would be reporting a fragment as if it were the
            # whole, which is the same mistake as reporting a stable reading instead
            # of a weighing — 30.0 g of a pour that was going to reach 37.0 g, paired
            # with the waiting dose, emailed as 1:1.67.
            #
            # Here we know more than flush() does, and it points the same way.  The
            # stream *resumed*: the samples after the gap say what is on the scale
            # now, so nothing has to be guessed.  If the load really did come off
            # during the stall the resumed stream reads ~0 and there was nothing to
            # report anyway; if it is still there, the weighing rebuilds itself from
            # the resumed stream and closes properly when the load finally leaves.
            # Either way the fragment is redundant, and emitting it can only invent
            # an event that did not happen.
            #
            # What is genuinely lost is the history before the gap — the earlier
            # steps and, because no zero was observed after the reset, how the load
            # arrived.  That last one is recorded as unknown rather than as zero; see
            # _end_step.
            self.reset()
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
            self._settle_from_window(clean.t)
            closed = self._close_weighing()
            self._window.clear()
            return closed

        if clean.weight <= cfg.zero_band and self._is_tare(clean):
            # Re-zeroing, not a removal.  Whatever is standing on the scale has just
            # been declared the baseline, which makes it a container and not a
            # weighing — so drop the open one rather than reporting it.
            self._in_plateau = False
            self._steps = []
            self._last_zero_t = clean.t
            self._window.clear()
            self._window.append(clean)
            return None

        if clean.weight <= cfg.zero_band:
            # The scale is empty again, so whatever was on it has been taken off and
            # the weighing is finished.  This — not a stable reading — is what ends
            # one: a reading that stops changing may simply be a pause while the next
            # packet is opened.
            self._end_step(clean.t)
            self._settle_from_window(clean.t)
            closed = self._close_weighing()
            # Only now.  _end_step measures rise_seconds from the zero the weighing
            # started at, and updating it first overwrote that with the zero the
            # weighing was ending on — which is always later than the plateau, so the
            # rise came out as 0.0 for every weighing a removal closed.
            self._last_zero_t = clean.t
            # Everything still in the window belongs to the weighing that just
            # ended, so it must not survive into the next one.  A lift-off throws
            # the reading across zero several times over a second or two, and each
            # crossing would otherwise reconstruct the same pour again from the
            # same stale samples — lost_shot.csv reported its 36.6 g three times.
            self._window.clear()
            self._window.append(clean)
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
        # None, not 0.0, when no zero was ever seen before this plateau.  _last_zero_t
        # is cleared by reset() and only ever set by a sample at or below zero_band,
        # so a weighing whose load was already on the scale when the detector started
        # has nothing to measure a rise against.  "It arrived instantly" and "we did
        # not see it arrive" are opposite findings; see Plateau.rise_seconds.
        #
        # A plateau that starts at or before the zero keeps 0.0, which is the honest
        # answer there: the scale *was* observed empty, and nothing accumulated
        # between that and the plateau's own start.
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

        # Measured across the clean samples only, never up to end_t.  The lift-off
        # transient is swallowed by the Hampel filter, and counting the time it
        # occupied would credit the reading with a hold nobody observed.
        if run[-1].t - run[0].t < cfg.settle_min_seconds:
            return

        value = median([s.weight for s in run])
        if value < cfg.min_mass:
            return
        if self._steps and value <= self._steps[-1].value + cfg.stable_tol:
            # The same reading the last step already recorded, or a lower one.  Not a
            # top-up, so there is nothing here that the weighing does not already
            # know — and appending it would report one settling as two steps.
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
        # Overlapping ranges: an unpaired dose already waiting means this is the pour.
        if pending_dose is not None:
            return "yield"
        # Otherwise fall back to shape — beans sit on the scale while grinding,
        # and no liquid is flowing while they do.
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
            # Between weighing the beans and pulling the shot other things land on
            # the scale in the same mass range — the holder going back down, a cup.
            # Deciding which candidate is the dose is therefore a real question, and
            # how long each was held answers it badly: on 2026-08-17 the beans were
            # held 12.1 s and the holder set back down 10.8 s, so the right answer
            # won by 1.3 seconds, which is luck rather than evidence.
            #
            # How the load *arrived* answers it properly.  Beans accumulate while
            # the grinder runs; anything already in a container is simply put down
            # and is there in one sample.  That distinction has no overlap in any
            # capture so far, where hold time overlaps outright.
            #
            # dose_min_hold_seconds still applies in classify() as a floor against
            # momentary taps; this only decides which qualifying candidate wins.
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
                    # The two arrived differently, or one of them was never seen to
                    # arrive at all.  See _arrival_rank for why that is three cases.
                    if rank > rank_before:
                        self._pending_dose = plateau
                elif plateau.duration > previous.duration:
                    # Nothing to choose between them on arrival — the same evidence,
                    # or the same absence of it — so fall back to the longer hold.
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
