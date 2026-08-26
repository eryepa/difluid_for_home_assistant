/**
 * DiFluid card — a Lovelace card bundled with the difluid_microbalance
 * integration.  It groups all entities of a chosen DiFluid device (scale or
 * R2) into an ordered sensor section + interactive control section.
 *
 * Registered automatically by the integration; appears in the "Add card"
 * picker as "DiFluid Microbalance / R2".
 */

const DOMAIN = "difluid_microbalance";

// Display order for the sensor rows (matched as substrings of the entity_id).
const SENSOR_ORDER = [
  "weight", "flow", "timer",
  "concentration", "refractive", "prism", "sample", "test_status",
  "status", "battery",
];
// Display order for the control rows.
const CONTROL_ORDER = [
  "tare", "start", "test", "mode",
];
// Control entities to hide from the card (still available on the device page).
const EXCLUDE_CONTROLS = ["auto_disconnect", "auto_shutdown"];

// The Statistics section, in display order: each quantity reads total -> period ->
// per day, so a column of numbers can be scanned down rather than hunted through.
//
// These are entity_id *suffixes*, and they are not the `key` of the sensor
// description they came from: entity_ids are built from the display name, so
// key="coffee_total" name="Coffee Ground" produces `..._coffee_ground`.  Writing the
// keys here is what put Coffee Ground and Coffee Ground (Period) in among the weight
// and the flow rate — they matched nothing, and unmatched entities fall through to
// the plain sensor list.
const STATS_ORDER = [
  "brew_count", "brew_count_period", "brews_per_day",
  "coffee_ground", "coffee_ground_period", "coffee_per_day",
  "reset_period",
];

// The working parts of the last shot.  Present but folded away: useful when a result
// looks wrong, noise every other day.  On the device page these share the Diagnostic
// card with the statistics above, because HA has nowhere else to put either; the split
// between the two exists only here.
const DIAG_ORDER = [
  "brew_dose", "brew_yield", "brew_ratio",
  "last_dose", "last_yield",
  "integration_version",
];

// ── pour curve ──────────────────────────────────────────────────────────────
// Seconds of the hold to keep after the pour stops.  Enough to show the reading
// settle, short enough to stay clear of what ends it — see pourWindow.
const POUR_TAIL_SECONDS = 4;
//: Seconds of flat line before the rise, so a pour does not start at the y-axis.
const POUR_LEAD_SECONDS = 2;

/**
 * The window of the last pour, from the attributes Last Yield publishes.
 *
 * Deliberately ends before `detected_at` rather than at it.  The detector confirms a
 * plateau after the fact, and what makes it confirm is the load coming off the scale
 * — so the moment it names is the moment the cup was already being lifted, and the
 * samples just before it are the lift: this install's last brew ends
 * -200.4, -142.7, 160.0, 685.4, 831.6 g inside a 37 g pour.  Drawing to detected_at
 * means drawing that.
 *
 * Returns null when rise_seconds is null, which is its documented value for a load
 * that was already on the scale before the detector was watching — after a restart or
 * a BLE gap.  There is no rise to plot in that case, and inventing one from a default
 * would draw a pour that never happened.
 */
const pourWindow = (attrs) => {
  if (!attrs) return null;
  const detected = Date.parse(attrs.detected_at);
  const plateau = Number(attrs.plateau_seconds);
  const rise = attrs.rise_seconds;
  if (!Number.isFinite(detected) || !Number.isFinite(plateau)) return null;
  if (rise === null || rise === undefined || !Number.isFinite(Number(rise))) return null;
  const riseSeconds = Number(rise);
  if (riseSeconds <= 0) return null;
  const pourEnd = detected - plateau * 1000;
  const riseStart = pourEnd - riseSeconds * 1000;
  return {
    start: riseStart - POUR_LEAD_SECONDS * 1000,
    end: pourEnd + Math.min(plateau, POUR_TAIL_SECONDS) * 1000,
    riseStart,
    pourEnd,
    riseSeconds,
  };
};

/**
 * Drop samples that are not part of the pour.
 *
 * The scale reports a knock, a lean or a lift as a reading like any other, and the
 * detector filters those with a Hampel window it does not expose.  A chart cannot
 * borrow that, but it does not need to: it knows what the pour weighed.  Anything far
 * outside that is not a data point about this pour, and keeping it costs the whole
 * y-axis — one -200 g sample flattens a 37 g curve into a line along the bottom.
 *
 * The band is generous on purpose.  This throws away what cannot belong, not what
 * looks unusual: a pour that overshoots, stalls or gets topped up stays on the chart,
 * because that is exactly what somebody opens this to see.
 */
const cleanSamples = (samples, expected) => {
  const ceiling = Number.isFinite(expected) && expected > 0 ? expected * 2 + 20 : Infinity;
  return samples.filter(
    (s) => Number.isFinite(s.v) && s.v >= -2 && s.v <= ceiling
  );
};

//: Horizontal gridlines, and therefore how many steps each vertical axis is cut into.
//: Both axes share the lines, so both have to share the count.
const AXIS_STEPS = 4;

/**
 * A round top for an axis, and the step to label it with.
 *
 * The axes used to be `peak * 1.1` cut into quarters, which put the numbers wherever
 * the coffee happened to land: a 37.2 g shot was labelled 0, 10, 20, 31, 41 and its
 * flow 0.0, 1.0, 2.0, 2.9, 3.9.  Every one of those is arithmetically correct and none
 * of them is a number anybody reads an axis in.
 *
 * The step is rounded up to 1, 2, 2.5 or 5 times a power of ten — the ladder every
 * chart axis has used since long before this one — and the top is that step times the
 * fixed number of lines, so `max >= peak` always holds and nothing is ever clipped.
 *
 * `minStep` is how flow gets whole numbers: below about 4 g/s the ladder would pick
 * tenths, and a tenth of a gram per second is not a quantity anybody is deciding
 * anything on.
 */
const axisTo = (peak, steps = AXIS_STEPS, minStep = 0) => {
  const raw = Number.isFinite(peak) && peak > 0 ? peak / steps : 0;
  const mag = raw > 0 ? Math.pow(10, Math.floor(Math.log10(raw))) : 1;
  const nice = [1, 2, 2.5, 5, 10].find((m) => m * mag >= raw - 1e-9) * mag;
  const step = Math.max(minStep, nice);
  return { step, max: step * steps };
};

//: Seconds between the vertical gridlines.  Ten, until a pour is long enough that ten
//: would draw a fence — a filter brew runs for minutes.
const timeStep = (xMax) => (xMax > 90 ? 30 : 10);

/** An SVG path through points already mapped to chart coordinates. */
const linePath = (pts) =>
  pts.length ? pts.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join("") : "";

// ── brewing control chart ───────────────────────────────────────────────────
// TDS against extraction, with a diagonal per brew ratio.  The relationship the whole
// chart rests on is EXT = TDS x yield / dose, so on these axes a ratio r is the line
// TDS = EXT / r — which is why a brew always sits on its own ratio diagonal and TDS
// alone decides where along it.
//
// Two frames, because espresso and filter do not share an axis: at 10% TDS a filter
// frame has nothing on it, and at 1.3% an espresso frame has everything in the bottom
// pixel.  Which one applies is decided by the coffee, not configured.
const CONTROL_FRAMES = {
  espresso: { x0: 14, x1: 26, y0: 4, y1: 16, box: [18, 22, 8, 12], ratios: [1, 2, 3, 4, 5, 6] },
  filter:   { x0: 14, x1: 26, y0: 0.8, y1: 1.8, box: [18, 22, 1.15, 1.45], ratios: [12, 14, 16, 18, 20] },
};

//: Above this TDS the cup is an espresso.  Nothing sane lands between 1.8 and 4.
const ESPRESSO_TDS = 3;

/**
 * The frame to draw, widened if need be so that no measured brew falls off it.
 *
 * A point outside the axes is the one worth seeing — it is the shot that went wrong —
 * so the frame gives way rather than the data.
 */
const controlFrame = (points, override) => {
  const last = points.length ? points[points.length - 1] : null;
  const base = { ...(last && last.tds < ESPRESSO_TDS
    ? CONTROL_FRAMES.filter
    : CONTROL_FRAMES.espresso) };
  if (override && override.length === 4) base.box = override.slice();
  for (const p of points) {
    if (!Number.isFinite(p.ext) || !Number.isFinite(p.tds)) continue;
    base.x0 = Math.min(base.x0, Math.floor(p.ext - 1));
    base.x1 = Math.max(base.x1, Math.ceil(p.ext + 1));
    base.y0 = Math.min(base.y0, p.tds - (base.y1 - base.y0) * 0.08);
    base.y1 = Math.max(base.y1, p.tds + (base.y1 - base.y0) * 0.08);
  }
  return base;
};

/**
 * Where a ratio diagonal enters and leaves the frame, or null if it misses entirely.
 *
 * Clipped rather than drawn and hidden, so a label can sit at the end of a line that
 * is actually on the chart — the app puts 1:2 and 1:3 on the right edge and 1:1 at the
 * top, because that is where each of those lines happens to leave.
 */
const ratioSegment = (ratio, frame) => {
  const tdsAt = (ext) => ext / ratio;
  const extAt = (tds) => tds * ratio;
  const pts = [];
  const push = (ext, tds) => {
    if (ext >= frame.x0 - 1e-9 && ext <= frame.x1 + 1e-9 &&
        tds >= frame.y0 - 1e-9 && tds <= frame.y1 + 1e-9) pts.push({ ext, tds });
  };
  push(frame.x0, tdsAt(frame.x0));
  push(frame.x1, tdsAt(frame.x1));
  push(extAt(frame.y0), frame.y0);
  push(extAt(frame.y1), frame.y1);
  if (pts.length < 2) return null;
  pts.sort((a, b) => a.ext - b.ext);
  const [from, to] = [pts[0], pts[pts.length - 1]];
  if (Math.abs(to.ext - from.ext) < 1e-6) return null;
  return { from, to };
};

// 18 rather than 18.0, and 37.4 rather than 37.40 — the numbers as a person writing a
// brew down would write them.
const trim1 = (v) => String(Math.round(v * 10) / 10);

/**
 * What the scale saw, as one line: `18 → 37.4 g · 1:2.1 · 20 s`.
 *
 * Every part is dropped when its number is missing rather than shown as zero.  The
 * pour time is the part that is really optional: a pour whose start was never
 * observed — an HA restart mid-shot, a BLE gap — has no duration at all, and "0 s"
 * would be a claim about the shot rather than an admission about the recording.  See
 * BrewPair.pour_seconds.
 */
const brewLabel = (p) => [
  Number.isFinite(p.dose) && Number.isFinite(p.yieldG)
    ? `${trim1(p.dose)} → ${trim1(p.yieldG)} g`
    // The dose alone, when the scale saw the grind but not the pour.  Not "18 → 0 g".
    : Number.isFinite(p.dose) ? `${trim1(p.dose)} g` : null,
  Number.isFinite(p.ratio) ? `1:${p.ratio.toFixed(1)}` : null,
  Number.isFinite(p.seconds) ? `${Math.round(p.seconds)} s` : null,
].filter(Boolean).join(" · ");

//: How far apart the pour card's two sources can be and still be the same brew.  They
//: are two publications of one plateau — Last Yield's detected_at and Brew Ratio's
//: paired_at — so they match exactly; the slack costs nothing and a mismatch here is
//: read as "different brews", which is the safe direction.
const SAME_POUR_SECONDS = 1;

/**
 * The brew the pour card should caption its curve with, in brewLabel's shape.
 *
 * The dose, the yield and the ratio come off Brew Ratio, which is the one place they
 * are guaranteed to describe the same cup — its own attributes, written in one go when
 * the pair formed.
 *
 * Not from the drawn curve, which is where the yield used to come from and why the two
 * cards disagreed: the pour card said 37.0 g and the extraction card 37.2 g for one
 * shot.  Both were honest.  The curve's last sample is whatever the scale read a few
 * seconds after the flow stopped, and the detector's figure is the plateau it settled
 * on — the crema was still going down.  The number a person compares between two cards
 * has to be the brew's, so only one of them can be right to show.
 *
 * Returns just the seconds when the last pour did not pair, which happens when a pour
 * is detected with no dose before it: Brew Ratio then describes an older cup than the
 * curve, and captioning one with the other is the mismatch this exists to prevent.
 */
const pourBrew = (ratioState, detectedAt, riseSeconds) => {
  const attrs = (ratioState || {}).attributes || {};
  const ratio = Number((ratioState || {}).state);
  const pairedAt = Date.parse(attrs.paired_at) / 1000;
  const drawnAt = Date.parse(detectedAt) / 1000;
  const samePour =
    Number.isFinite(pairedAt) && Number.isFinite(drawnAt) &&
    Math.abs(pairedAt - drawnAt) < SAME_POUR_SECONDS;
  return samePour
    ? { dose: attrs.dose, yieldG: attrs.yield, ratio, seconds: riseSeconds }
    : { seconds: riseSeconds };
};

//: How far apart two timestamps can be and still name the same brew.  They come from
//: the same stored pair by two routes — the point copied it when it was written, the
//: attribute reads it now — so they agree exactly today; the slack is here so that a
//: rounding change on the Python side cannot silently turn every point stale.
const SAME_BREW_SECONDS = 0.5;

/**
 * Is the newest measured brew still the newest brew?
 *
 * The red dot means "this is the cup you are drinking".  It stops being true the
 * moment another shot is pulled: the chart then highlights the cup *before* the one in
 * your hand, and nothing on it says so.  So the highlight goes out when the detector
 * completes a brew later than the newest measured one, and comes back when that brew
 * is measured.
 *
 * True when nothing is known about the last brew, rather than false: a detector that
 * has never paired should not put out a light it never lit.
 */
const isCurrent = (point, lastBrewAt) =>
  !point || !Number.isFinite(lastBrewAt) || point.at >= lastBrewAt - SAME_BREW_SECONDS;

/**
 * How this Home Assistant formats a time.
 *
 * Not the browser's defaults, which is what `toLocaleTimeString([], …)` gets you and
 * which rendered 19:32 as "07:32 PM" on an install whose every other clock reads 24
 * hours.  Home Assistant keeps the answer in hass.locale, and it is a real setting a
 * user has chosen — so it is the one to ask.
 *
 *   time_format  "12" / "24" force it; "language" follows the HA language's own
 *                convention; "system" defers to the browser, which is the only case
 *                where passing no language is right.
 *   time_zone    "local" or "server"; on "server" the instant must be rendered in
 *                hass.config.time_zone rather than wherever the browser is sitting.
 */
const timeFormat = (hass) => {
  const locale = (hass && hass.locale) || {};
  const opts = {};
  if (locale.time_format === "24") opts.hour12 = false;
  else if (locale.time_format === "12") opts.hour12 = true;
  if (locale.time_zone === "server" && hass && hass.config && hass.config.time_zone) {
    opts.timeZone = hass.config.time_zone;
  }
  return {
    lang: locale.time_format === "system" ? undefined : locale.language || undefined,
    opts,
  };
};

//: Which calendar day an instant falls on, in the same zone the time is rendered in —
//: otherwise a "server" setting could print yesterday's time without its date.
const dayKey = (d, lang, opts) =>
  d.toLocaleDateString(lang, {
    ...(opts.timeZone ? { timeZone: opts.timeZone } : {}),
    year: "numeric", month: "2-digit", day: "2-digit",
  });

/**
 * A POSIX timestamp as a clock time, with the date added once it is not today.
 *
 * `now` is a parameter rather than a call to Date.now() inside so that the "is it
 * today" branch can be tested at all.
 */
const whenLabel = (ts, hass, now = Date.now()) => {
  if (!Number.isFinite(ts)) return "";
  const { lang, opts } = timeFormat(hass);
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", ...opts });
  return dayKey(d, lang, opts) === dayKey(new Date(now), lang, opts)
    ? time
    : `${d.toLocaleDateString(lang, {
        ...(opts.timeZone ? { timeZone: opts.timeZone } : {}),
        day: "numeric", month: "short",
      })} ${time}`;
};

/**
 * A card header with the time of the brew it is about: `Extraction · 19:32`.
 *
 * Both charts describe one cup, so both name it the same way and in the same place.
 * The time was in a line of its own under the Extraction chart until 1.9.0, next to a
 * second time saying when the reading was taken — which answered a question nobody was
 * asking twice.  The brew's own time is the one that identifies it; when the reading
 * happened is in the Last TDS sensor for anyone who wants it.
 */
const cardTitle = (title, at, hass, now = Date.now()) => {
  const when = whenLabel(at, hass, now);
  return when ? `${title} · ${when}` : title;
};

/**
 * What to say when the highlight has gone out.
 *
 * A dot that quietly stops being red says something happened without saying what, so
 * the newer brew names itself.
 */
const staleNote = (lastBrewAt, hass, now = Date.now()) => {
  const when = whenLabel(lastBrewAt, hass, now);
  return when ? `newer brew ${when}, not measured` : "";
};

/**
 * The line under the chart: what the refractometer read on the left, what the scale
 * saw on the right.
 *
 * A function rather than a template inside _render so that it can be tested at all —
 * everything from `class DifluidCard` down needs a DOM and never runs in tools/
 * test_card.js.  Before 1.7.0 the right-hand side said "in the box" or "outside",
 * which only repeated where the dot already was.
 */
const legendRow = (p) => `
            <span class="cap">EXT ${p.ext.toFixed(2)}% · TDS ${p.tds.toFixed(2)}%</span>
            <span class="brew">${brewLabel(p)}</span>`;

const idPart = (entityId) => entityId.split(".")[1] || entityId;

const rank = (entityId, order) => {
  const id = idPart(entityId);
  for (let i = 0; i < order.length; i++) if (id.includes(order[i])) return i;
  return order.length + 1;
};

const inList = (entityId, list) => {
  const id = idPart(entityId);
  return list.some((key) => id.includes(key));
};

// Statistics match on the end of the entity_id rather than anywhere inside it, which
// the other lists here can afford to do because none of their keys is a prefix of
// another.  "brew_count" is a prefix of "brew_count_period" and "coffee_ground" of
// "coffee_ground_period": under a substring match each pair collapses to a single
// rank, the sort has nothing left to decide, and the row order falls back to
// hass.entities iteration order — the order the entities were registered in.  That
// happens to read correctly today, which is worse than reading wrongly: it would go
// wrong the first time a fresh install registered them in another order.
const statRank = (entityId) => {
  const id = idPart(entityId);
  for (let i = 0; i < STATS_ORDER.length; i++)
    if (id.endsWith(STATS_ORDER[i])) return i;
  return -1;
};

const isStat = (entityId) => statRank(entityId) >= 0;

class DifluidCard extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._built = false;
    this.innerHTML = "";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    this._update();
  }

  getCardSize() {
    return 6;
  }

  static getConfigElement() {
    return document.createElement("difluid-card-editor");
  }

  static getStubConfig(hass) {
    const ours = Object.values(hass.devices || {}).filter((d) =>
      (d.identifiers || []).some((ident) => ident[0] === DOMAIN)
    );
    // Prefer a device nothing else hangs off: the scale, not the detector service
    // device that hangs off it and not the R2, which is linked to neither.  Any of
    // them resolves to the same cluster, but the scale is the one whose name reads
    // like a heading.
    const parents = new Set(ours.map((d) => d.via_device_id).filter(Boolean));
    const dev = ours.find((d) => parents.has(d.id)) || ours[0];
    return { type: "custom:difluid-card", device: dev ? dev.id : "" };
  }

  // ── entity resolution ─────────────────────────────────────────────────────

  // The devices this card draws from: the configured one plus anything linked to it
  // by via_device, followed in both directions.
  //
  // A card used to be one device's worth of rows, which stopped being true when the
  // brew detector moved onto a service device of its own: the scale holds the weight
  // and the flow rate, the detector holds every statistic, and a card showing one
  // without the other is half a card.  Following the link in both directions means a
  // config pointing at either device produces the same rows, so the configs people
  // already have keep working — they point at the scale, which is where all of this
  // used to live.
  _clusterIds() {
    const devices = this._hass.devices || {};
    const root = this._config.device;
    const isOurs = (d) => (d.identifiers || []).some((ident) => ident[0] === DOMAIN);
    const cluster = new Set([root]);
    for (const [id, dev] of Object.entries(devices)) {
      if (!isOurs(dev)) continue;
      if (dev.via_device_id === root) cluster.add(id);          // children of root
      const rootDev = devices[root];
      if (rootDev && rootDev.via_device_id === id) cluster.add(id);  // root's parent
    }
    return cluster;
  }

  _deviceEntities() {
    const hass = this._hass;
    const cluster = this._clusterIds();
    const ids = [];
    for (const [entityId, ent] of Object.entries(hass.entities || {})) {
      if (!cluster.has(ent.device_id)) continue;
      if (ent.disabled_by) continue;
      if (!(entityId in hass.states)) continue;
      ids.push(entityId);
    }
    return ids;
  }

  _deviceName() {
    const devices = this._hass.devices || {};
    // Name the card after the physical device even when it is configured on the
    // detector: "Brew Detector" is a poor heading for a card whose first row is the
    // live weight.
    const own = devices[this._config.device];
    const dev = (own && own.via_device_id && devices[own.via_device_id]) || own;
    return (dev && (dev.name_by_user || dev.name)) || "DiFluid";
  }

  // ── build (once) ──────────────────────────────────────────────────────────
  _build() {
    if (!this._hass || !this._config) return;

    const card = document.createElement("ha-card");
    card.header = this._config.title || this._deviceName();

    const body = document.createElement("div");
    body.className = "difluid-body";
    card.appendChild(body);

    const style = document.createElement("style");
    style.textContent = `
      .difluid-body { padding: 4px 16px 16px; }
      .row { display:flex; align-items:center; min-height:40px; gap:12px; }
      .row .icon { color: var(--state-icon-color,#44739e); width:24px; text-align:center; }
      .row .label { flex:1; color: var(--primary-text-color); }
      .row .value { color: var(--primary-text-color); font-weight:500; text-align:right; }
      .divider { height:1px; background:var(--divider-color); margin:8px 0; }
      .section {
        display:flex; align-items:center; gap:8px;
        margin:12px 0 2px; color: var(--secondary-text-color);
        font-size:12px; font-weight:500; text-transform:uppercase; letter-spacing:.06em;
      }
      .section::after {
        content:""; flex:1; height:1px; background:var(--divider-color);
      }
      details.diag > summary {
        list-style:none; cursor:pointer; outline:none;
        display:flex; align-items:center; gap:8px;
        margin:12px 0 2px; color: var(--secondary-text-color);
        font-size:12px; font-weight:500; text-transform:uppercase; letter-spacing:.06em;
      }
      details.diag > summary::-webkit-details-marker { display:none; }
      details.diag > summary::before { content:"\\25B8"; font-size:10px; }
      details.diag[open] > summary::before { content:"\\25BE"; }
      details.diag > summary::after {
        content:""; flex:1; height:1px; background:var(--divider-color);
      }
      button.df-btn {
        background: var(--primary-color); color: var(--text-primary-color,#fff);
        border:none; border-radius:16px; padding:6px 16px; cursor:pointer; font-size:14px;
      }
      button.df-btn:active { opacity:.8; }
      select.df-select, input.df-number {
        background: var(--card-background-color); color: var(--primary-text-color);
        border:1px solid var(--divider-color); border-radius:6px; padding:6px 8px; font-size:14px;
      }
      input.df-number { width:80px; }
      .df-btn:disabled, .df-select:disabled, .df-number:disabled {
        cursor:not-allowed; filter:grayscale(1);
      }
    `;
    card.appendChild(style);

    // Three groups, partitioned before anything is ranked: statistics and diagnostics
    // are claimed first, and whatever is left is a live reading or a control.  Doing
    // it in that order is what keeps a new entity from silently landing in the plain
    // sensor list — the card enumerates every entity the device has, so anything not
    // deliberately placed ends up in the middle of the weight and the flow rate.
    const ids = this._deviceEntities();
    const stats = ids
      .filter(isStat)
      .sort((a, b) => statRank(a) - statRank(b));
    const diagnostics = ids
      .filter((id) => !isStat(id) && inList(id, DIAG_ORDER))
      .sort((a, b) => rank(a, DIAG_ORDER) - rank(b, DIAG_ORDER));
    const claimed = new Set([...stats, ...diagnostics]);

    const sensors = ids
      .filter((id) => id.startsWith("sensor.") && !claimed.has(id))
      .sort((a, b) => rank(a, SENSOR_ORDER) - rank(b, SENSOR_ORDER));
    const controls = ids
      .filter((id) => /^(button|select|number|switch)\./.test(id))
      .filter((id) => !claimed.has(id))
      .filter((id) => !EXCLUDE_CONTROLS.some((x) => id.includes(x)))
      .sort((a, b) => rank(a, CONTROL_ORDER) - rank(b, CONTROL_ORDER));

    this._rows = [];

    for (const id of sensors) body.appendChild(this._sensorRow(id));
    if (sensors.length && controls.length) {
      const div = document.createElement("div");
      div.className = "divider";
      body.appendChild(div);
    }
    for (const id of controls) body.appendChild(this._controlRow(id));

    if (stats.length) {
      body.appendChild(this._sectionHeader("Statistics"));
      for (const id of stats) {
        body.appendChild(
          id.startsWith("sensor.") ? this._sensorRow(id) : this._controlRow(id)
        );
      }
    }

    if (diagnostics.length) {
      const details = document.createElement("details");
      details.className = "diag";
      const summary = document.createElement("summary");
      summary.textContent = "Diagnostic";
      details.appendChild(summary);
      for (const id of diagnostics) details.appendChild(this._sensorRow(id));
      body.appendChild(details);
    }

    if (!sensors.length && !controls.length && !stats.length && !diagnostics.length) {
      const empty = document.createElement("div");
      empty.className = "row label";
      empty.textContent = this._config.device
        ? "No entities found for this device."
        : "Select a DiFluid device in the card settings.";
      body.appendChild(empty);
    }

    this.innerHTML = "";
    this.appendChild(card);
    this._card = card;
    this._built = true;
  }

  _stateName(id) {
    const st = this._hass.states[id];
    const ent = (this._hass.entities || {})[id];
    return (
      (ent && ent.name) ||
      (st && st.attributes && st.attributes.friendly_name
        ? st.attributes.friendly_name.replace(`${this._deviceName()} `, "")
        : id)
    );
  }

  _sectionHeader(text) {
    const el = document.createElement("div");
    el.className = "section";
    el.textContent = text;
    return el;
  }

  _sensorRow(id) {
    const row = document.createElement("div");
    row.className = "row";
    const icon = document.createElement("ha-icon");
    icon.className = "icon";
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = this._stateName(id);
    const value = document.createElement("div");
    value.className = "value";
    row.append(icon, label, value);
    this._rows.push({ id, kind: "sensor", icon, value });
    return row;
  }

  _controlRow(id) {
    const domain = id.split(".")[0];
    const st = this._hass.states[id];
    const row = document.createElement("div");
    row.className = "row";
    const icon = document.createElement("ha-icon");
    icon.className = "icon";
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = this._stateName(id);
    row.append(icon, label);

    let control;
    if (domain === "button") {
      control = document.createElement("button");
      control.className = "df-btn value";
      // Reset is the one button here that destroys something: Tare and Start/Stop are
      // undone by pressing them again, while a period, once ended, cannot be restored
      // — the odometers survive but the trip figures do not. It also sits in the same
      // column as five sensor rows, so a mis-tap is a realistic way to lose a month.
      const isReset = idPart(id).includes("reset_period");
      control.textContent = isReset ? "Reset" : "Press";
      control.addEventListener("click", () => {
        if (isReset && !window.confirm(
          "Start a new statistics period? The all-time totals are kept, " +
          "but the current period cannot be restored."
        )) return;
        this._hass.callService("button", "press", { entity_id: id });
      });
    } else if (domain === "switch") {
      control = document.createElement("button");
      control.className = "df-btn value";
      control.addEventListener("click", () =>
        this._hass.callService("switch", "toggle", { entity_id: id })
      );
    } else if (domain === "select") {
      control = document.createElement("select");
      control.className = "df-select value";
      for (const opt of (st.attributes.options || [])) {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        control.appendChild(o);
      }
      control.addEventListener("change", () =>
        this._hass.callService("select", "select_option", {
          entity_id: id,
          option: control.value,
        })
      );
    } else if (domain === "number") {
      control = document.createElement("input");
      control.type = "number";
      control.className = "df-number value";
      control.min = st.attributes.min;
      control.max = st.attributes.max;
      control.step = st.attributes.step || 1;
      control.addEventListener("change", () =>
        this._hass.callService("number", "set_value", {
          entity_id: id,
          value: Number(control.value),
        })
      );
    }
    if (control) row.appendChild(control);
    this._rows.push({ id, kind: domain, icon, control, row });
    return row;
  }

  // ── update (each hass change) ─────────────────────────────────────────────
  _update() {
    if (!this._built || !this._rows) return;
    const hass = this._hass;
    for (const r of this._rows) {
      const st = hass.states[r.id];
      if (!st) continue;
      const ent = (hass.entities || {})[r.id];
      const iconName =
        st.attributes.icon || (ent && ent.icon) || this._domainIcon(r.id);
      if (iconName && r.icon.getAttribute("icon") !== iconName)
        r.icon.setAttribute("icon", iconName);

      // Disable controls (and dim the row) when the entity is unavailable —
      // e.g. the device is powered off / disconnected — mirroring the device page.
      // Note: a button's normal "available" state is "unknown" (never pressed),
      // so only "unavailable" means offline.
      const available = st.state !== "unavailable";
      if (r.kind !== "sensor" && r.control) {
        r.control.disabled = !available;
        if (r.row) r.row.style.opacity = available ? "" : "0.5";
      }

      if (r.kind === "sensor") {
        r.value.textContent = this._formatState(st);
      } else if (r.kind === "select" && r.control) {
        if (available && document.activeElement !== r.control && r.control.value !== st.state)
          r.control.value = st.state;
      } else if (r.kind === "number" && r.control) {
        if (available && document.activeElement !== r.control)
          r.control.value = st.state;
      }
    }
  }

  _formatState(st) {
    try {
      if (this._hass.formatEntityState) return this._hass.formatEntityState(st);
    } catch (e) { /* fall through */ }
    const unit = st.attributes.unit_of_measurement;
    return unit ? `${st.state} ${unit}` : st.state;
  }

  _domainIcon(id) {
    const d = id.split(".")[0];
    return d === "button" ? "mdi:gesture-tap-button"
      : d === "select" ? "mdi:format-list-bulleted"
      : d === "number" ? "mdi:ray-vertex"
      : "mdi:information-outline";
  }
}

// ── visual editor ────────────────────────────────────────────────────────────
class DifluidCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: ev.detail.value },
          })
        );
      });
      this._form.computeLabel = (s) =>
        s.name === "device" ? "DiFluid device" : "Title (optional)";
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = [
      { name: "device", selector: { device: { integration: DOMAIN } } },
      { name: "title", selector: { text: {} } },
    ];
    this._form.data = this._config;
  }
}

/**
 * The last pour, as weight and flow against seconds since it started.
 *
 * A separate card rather than a section of DifluidCard: a chart wants its own height
 * and its own place on the dashboard, and somebody who only wants the numbers should
 * not have to carry it.
 *
 * While the scale is connected it draws what is happening now, straight from the state
 * machine — the weight sensor updates on every BLE packet, so the card's own hass
 * setter is already being called five times a second and there is nothing to poll.
 * When the scale goes away it falls back to the recorder, which by then holds the brew
 * that just finished.
 */
class DifluidPourCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    //: Live samples, newest last.  Cleared whenever the scale disconnects, so a
    //: reconnect never draws a line across the gap between two different brews.
    this._live = [];
    this._history = null;
    this._fetchedFor = null;
    this._fetching = false;
  }

  setConfig(config) {
    if (!config || !config.device) throw new Error("Choose a DiFluid device");
    this._config = config;
  }

  getCardSize() {
    return 6;
  }

  static getConfigElement() {
    return document.createElement("difluid-card-editor");
  }

  static getStubConfig(hass) {
    return DifluidCard.getStubConfig(hass);
  }

  set hass(hass) {
    this._hass = hass;
    const ids = this._entityIds();
    if (!ids) return;

    const weight = hass.states[ids.weight];
    const connected = weight && weight.state !== "unavailable";

    if (connected) {
      const t = Date.parse(weight.last_updated);
      const v = Number(weight.state);
      const last = this._live[this._live.length - 1];
      if (Number.isFinite(t) && Number.isFinite(v) && (!last || last.t !== t)) {
        const flow = Number((hass.states[ids.flow] || {}).state);
        this._live.push({ t, v, f: Number.isFinite(flow) ? flow : 0 });
        // A pour is under a minute; anything older is a previous trip to the scale.
        const cutoff = t - 120000;
        while (this._live.length && this._live[0].t < cutoff) this._live.shift();
      }
    } else if (this._live.length) {
      this._live = [];
      this._fetchedFor = null; // re-read the recorder: it now has the finished brew
    }

    this._maybeFetch(ids);
    this._render(ids);
  }

  _cluster() {
    return DifluidCard.prototype._clusterIds.call(this);
  }

  _entityIds() {
    if (!this._hass || !this._config) return null;
    const cluster = this._cluster();
    const found = {};
    for (const [entityId, ent] of Object.entries(this._hass.entities || {})) {
      if (!cluster.has(ent.device_id)) continue;
      const id = idPart(entityId);
      if (id.endsWith("_weight")) found.weight = entityId;
      else if (id.endsWith("_flow_rate")) found.flow = entityId;
      else if (id.endsWith("_last_yield")) found.lastYield = entityId;
      else if (id.endsWith("_brew_ratio")) found.ratio = entityId;
    }
    return found.weight ? found : null;
  }

  async _maybeFetch(ids) {
    if (this._live.length || !ids.lastYield || this._fetching) return;
    const attrs = (this._hass.states[ids.lastYield] || {}).attributes;
    const win = pourWindow(attrs);
    if (!win) {
      this._history = null;
      return;
    }
    if (this._fetchedFor === attrs.detected_at) return;

    this._fetching = true;
    this._fetchedFor = attrs.detected_at;
    try {
      const entity_ids = [ids.weight];
      if (ids.flow) entity_ids.push(ids.flow);
      const res = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: new Date(win.start).toISOString(),
        end_time: new Date(win.end).toISOString(),
        entity_ids,
        minimal_response: true,
        no_attributes: true,
        // The detector runs on every sample and so does this chart; the recorder's
        // idea of a significant change would drop most of the rise.
        significant_changes_only: false,
      });
      this._history = { win, rows: res || {} };
    } catch (err) {
      // Most likely the window has aged out of the recorder's retention.
      this._history = null;
      console.warn("difluid-pour-card: history fetch failed", err);
    } finally {
      this._fetching = false;
      this._render(ids);
    }
  }

  // The compressed history format uses `lu` (epoch seconds) and `s`; be tolerant of
  // the uncompressed one too, since which arrives depends on the request flags.
  _rowsToSamples(rows) {
    return (rows || []).map((r) => ({
      t: Number.isFinite(r.lu)
        ? r.lu * 1000
        : Date.parse(r.last_updated || r.last_changed),
      v: Number(r.s !== undefined ? r.s : r.state),
    }));
  }

  _series(ids) {
    if (this._live.length >= 2) {
      const t0 = this._live[0].t;
      const peak = Math.max(...this._live.map((s) => s.v));
      return {
        live: true,
        t0,
        weight: cleanSamples(this._live, peak),
        flow: this._live.map((s) => ({ t: s.t, v: s.f })),
      };
    }
    if (!this._history) return null;
    const { win, rows } = this._history;
    const expected = Number((this._hass.states[ids.lastYield] || {}).state);
    const weight = cleanSamples(this._rowsToSamples(rows[ids.weight]), expected).filter(
      (s) => s.t >= win.start && s.t <= win.end
    );
    if (weight.length < 2) return null;
    const flow = ids.flow
      ? this._rowsToSamples(rows[ids.flow]).filter(
          (s) => Number.isFinite(s.v) && s.t >= win.start && s.t <= win.end
        )
      : [];
    return { live: false, t0: win.riseStart, weight, flow, riseSeconds: win.riseSeconds };
  }

  _render(ids) {
    const series = this._series(ids);
    const root = this.shadowRoot;
    if (!series) {
      root.innerHTML = `
        <ha-card header="Pour">
          <div class="empty">No pour recorded yet.</div>
          ${DifluidPourCard.STYLE}
        </ha-card>`;
      return;
    }

    const W = 480, H = 210;
    const padL = 34, padR = 34, padT = 12, padB = 24;
    const innerW = W - padL - padR, innerH = H - padT - padB;

    const secs = (s) => (s.t - series.t0) / 1000;
    const xMax = Math.max(1, ...series.weight.map(secs));
    // Whole grams on the left of the ladder, whole grams per second on the right.
    const wAxis = axisTo(Math.max(1, ...series.weight.map((s) => s.v)));
    const fAxis = axisTo(Math.max(0.5, ...series.flow.map((s) => s.v)), AXIS_STEPS, 1);

    const X = (t) => padL + (Math.min(Math.max(secs({ t }), 0), xMax) / xMax) * innerW;
    const Yw = (v) => padT + innerH - (Math.min(v, wAxis.max) / wAxis.max) * innerH;
    const Yf = (v) => padT + innerH - (Math.min(v, fAxis.max) / fAxis.max) * innerH;

    const wPts = series.weight.map((s) => ({ x: X(s.t), y: Yw(s.v) }));
    const fPts = series.flow
      .filter((s) => s.t >= series.t0)
      .map((s) => ({ x: X(s.t), y: Yf(s.v) }));

    const area = wPts.length
      ? `${linePath(wPts)}L${wPts[wPts.length - 1].x.toFixed(1)} ${(padT + innerH).toFixed(1)}L${wPts[0].x.toFixed(1)} ${(padT + innerH).toFixed(1)}Z`
      : "";

    // Flow on the left, weight on the right.  One set of lines serves both, which is
    // why the two axes are cut into the same number of steps.
    const gridY = [];
    for (let i = 0; i <= AXIS_STEPS; i++) {
      const y = padT + innerH - (i / AXIS_STEPS) * innerH;
      gridY.push(
        `<line class="grid" x1="${padL}" y1="${y}" x2="${padL + innerW}" y2="${y}"/>
         <text class="tick f" x="${padL - 5}" y="${y + 3}">${trim1(i * fAxis.step)}</text>
         <text class="tick w" x="${padL + innerW + 5}" y="${y + 3}">${trim1(i * wAxis.step)}</text>`
      );
    }

    // Vertical lines to match, so a point on the curve can be read off in both
    // directions instead of only downwards.
    const secTicks = [];
    const step = timeStep(xMax);
    for (let s = 0; s <= xMax; s += step) {
      const x = X(series.t0 + s * 1000);
      secTicks.push(
        `<line class="grid" x1="${x}" y1="${padT}" x2="${x}" y2="${padT + innerH}"/>
         <text class="tick x" x="${x}" y="${H - 6}">${s}s</text>`
      );
    }

    // When the drawn pour happened.  Only for the recorded one — "Pouring" is now by
    // definition, and the curve is moving while you read it.  Taken from the same
    // detected_at the history window was fetched for, so the header cannot name one
    // brew while the chart draws another.
    const detectedAt = ((this._hass.states[ids.lastYield] || {}).attributes || {})
      .detected_at;

    const last = series.weight[series.weight.length - 1];
    const brew = series.live
      ? null
      : pourBrew(this._hass.states[ids.ratio], detectedAt, series.riseSeconds);
    const caption = series.live
      ? `${last.v.toFixed(1)} g · ${secs(last).toFixed(0)} s`
      : brewLabel(brew) || `${last.v.toFixed(1)} g`;

    // Where the pour actually ended.
    //
    // The curve runs on past it — deliberately, so the reading can be seen settling —
    // and that is what makes the chart read wrong at a glance: the axis goes to 20 s
    // while the brew took 17, and nothing says which of the two the caption means.
    // So the end names itself, on both axes at once.
    const endAt = series.live ? null : series.riseSeconds;
    const endWeight = brew && Number.isFinite(brew.yieldG) ? brew.yieldG : last.v;
    const endMark = Number.isFinite(endAt) && endAt > 0 && endAt <= xMax
      ? (() => {
          const x = X(series.t0 + endAt * 1000), y = Yw(endWeight);
          return `<line class="mark" x1="${x}" y1="${padT}" x2="${x}" y2="${padT + innerH}"/>
                  <line class="mark" x1="${padL}" y1="${y}" x2="${x}" y2="${y}"/>
                  <circle class="markdot" cx="${x}" cy="${y}" r="3.5"/>
                  <text class="tick end" x="${x - 4}" y="${y - 6}">${trim1(endWeight)} g · ${Math.round(endAt)} s</text>`;
        })()
      : "";
    const header = series.live
      ? "Pouring"
      : cardTitle("Last pour", Date.parse(detectedAt) / 1000, this._hass);

    root.innerHTML = `
      <ha-card header="${header}">
        <div class="body">
          <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
               aria-label="Pour curve: weight and flow over time">
            ${gridY}
            <path class="area" d="${area}"/>
            <path class="weight" d="${linePath(wPts)}"/>
            <path class="flow" d="${linePath(fPts)}"/>
            ${secTicks.join("")}
            ${endMark}
          </svg>
          <div class="legend">
            <span class="k flow"></span>Flow
            <span class="k weight"></span>Weight
            <span class="cap">${caption}</span>
          </div>
        </div>
        ${DifluidPourCard.STYLE}
      </ha-card>`;
  }
}

DifluidPourCard.STYLE = `
  <style>
    .body { padding: 4px 12px 12px; }
    .empty { padding: 24px 16px; color: var(--secondary-text-color); }
    svg { width: 100%; height: 210px; display: block; }
    .grid { stroke: var(--divider-color); stroke-width: 1; }
    .area { fill: var(--state-icon-color, #44739e); opacity: .12; stroke: none; }
    .weight { fill: none; stroke: var(--state-icon-color, #44739e); stroke-width: 2;
              stroke-linejoin: round; stroke-linecap: round; }
    .flow { fill: none; stroke: var(--warning-color, #ffa726); stroke-width: 1.5;
            stroke-dasharray: 3 3; stroke-linejoin: round; }
    .tick { font-size: 9px; fill: var(--secondary-text-color); }
    .tick.f { text-anchor: end; }
    .tick.w { text-anchor: start; }
    .tick.end { text-anchor: end; fill: var(--primary-text-color); font-weight: 500; }
    .mark { stroke: var(--state-icon-color, #44739e); stroke-width: 1;
            stroke-dasharray: 2 3; opacity: .8; }
    .markdot { fill: var(--state-icon-color, #44739e); }
    .tick.x { text-anchor: middle; }
    .legend { display: flex; align-items: center; gap: 6px; font-size: 12px;
              color: var(--secondary-text-color); padding-top: 2px; }
    .legend .k { width: 10px; height: 2px; display: inline-block; }
    .legend .k.weight { background: var(--state-icon-color, #44739e); }
    .legend .k.flow { background: var(--warning-color, #ffa726); }
    .legend .cap { margin-left: auto; color: var(--primary-text-color); font-weight: 500; }
  </style>`;

/**
 * The brewing control chart: every measured brew as a dot on TDS against extraction.
 *
 * Reads the series off the Extraction sensor's `points` attribute rather than the
 * refractometer's own entities.  The R2 is a handheld and is switched off almost all
 * of the time, so its sensors are `unavailable` whenever anybody is actually looking
 * at a dashboard; the detector stores each reading against the brew it belonged to,
 * and that store is what this draws.
 */
class DifluidControlCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    if (!config || !config.device) throw new Error("Choose a DiFluid device");
    if (config.box && (!Array.isArray(config.box) || config.box.length !== 4)) {
      throw new Error("box must be [ext_low, ext_high, tds_low, tds_high]");
    }
    this._config = config;
  }

  getCardSize() {
    return 8;
  }

  static getConfigElement() {
    return document.createElement("difluid-card-editor");
  }

  static getStubConfig(hass) {
    return DifluidCard.getStubConfig(hass);
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _extractionEntity() {
    const cluster = DifluidCard.prototype._clusterIds.call(this);
    for (const [entityId, ent] of Object.entries(this._hass.entities || {})) {
      if (cluster.has(ent.device_id) && idPart(entityId).endsWith("_extraction")) {
        return entityId;
      }
    }
    return null;
  }

  //: When the detector last completed a brew, measured or not — see isCurrent.
  _lastBrewAt() {
    const id = this._extractionEntity();
    if (!id) return null;
    const v = ((this._hass.states[id] || {}).attributes || {}).last_brew_at;
    return Number.isFinite(v) ? v : null;
  }

  _points() {
    const id = this._extractionEntity();
    if (!id) return [];
    const attrs = (this._hass.states[id] || {}).attributes || {};
    // Positional, and the tail is optional: dose, yield and pour time were added in
    // 1.7.0, and a point stored before that — or a pour whose start was never seen —
    // simply has none.  Destructuring past the end gives undefined, which every
    // consumer below already treats as "do not show it".
    return (attrs.points || [])
      .map(([at, ext, tds, ratio, dose, yieldG, seconds, measuredAt]) =>
        ({ at, ext, tds, ratio, dose, yieldG, seconds, measuredAt }))
      // A reading with no TDS is not a reading.  A reading with no EXT is one whose
      // pour the scale never saw: it is a real measurement and it is kept, but it
      // cannot be placed on an extraction axis until somebody supplies the yield.
      .filter((p) => Number.isFinite(p.tds));
  }

  _render() {
    if (!this._hass || !this._config) return;
    const points = this._points();
    const root = this.shadowRoot;

    if (!points.length) {
      root.innerHTML = `
        <ha-card header="Extraction">
          <div class="empty">
            No brew measured yet. Pull a shot, then measure it on the refractometer —
            the reading attaches to the last brew.
          </div>
          ${DifluidControlCard.STYLE}
        </ha-card>`;
      return;
    }

    // Measured, but with no yield there is no extraction and therefore no x for the
    // dot.  Saying "nothing measured yet" here would be a lie about the one number
    // the refractometer cannot produce again, so the card says what is missing and
    // where to put it.
    const plottable = points.filter((p) => Number.isFinite(p.ext));
    if (!plottable.length) {
      const p = points[points.length - 1];
      root.innerHTML = `
        <ha-card header="${cardTitle("Extraction", p.at, this._hass)}">
          <div class="empty">
            TDS ${p.tds.toFixed(2)}% on ${trim1(p.dose)} g, but the scale never saw the
            pour, so there is no extraction to plot. Enter what came out in
            <b>Measured Yield</b> and this brew joins the chart.
          </div>
          ${DifluidControlCard.STYLE}
        </ha-card>`;
      return;
    }

    const f = controlFrame(plottable, this._config.box);
    const W = 520, H = 380;
    const padL = 38, padR = 40, padT = 16, padB = 30;
    const iW = W - padL - padR, iH = H - padT - padB;

    const X = (ext) => padL + ((ext - f.x0) / (f.x1 - f.x0)) * iW;
    const Y = (tds) => padT + iH - ((tds - f.y0) / (f.y1 - f.y0)) * iH;

    // Grid + axis labels.  Whole numbers on EXT; TDS steps depend on the frame, since
    // an espresso axis counts in percent and a filter one in tenths.
    const xStep = (f.x1 - f.x0) > 16 ? 2 : 1;
    const yStep = (f.y1 - f.y0) > 4 ? 1 : 0.1;
    const grid = [];
    for (let e = Math.ceil(f.x0); e <= f.x1; e += xStep) {
      grid.push(`<line class="grid" x1="${X(e)}" y1="${padT}" x2="${X(e)}" y2="${padT + iH}"/>
                 <text class="tick x" x="${X(e)}" y="${H - 10}">${e}</text>`);
    }
    for (let t = Math.ceil(f.y0 / yStep) * yStep; t <= f.y1 + 1e-9; t += yStep) {
      const label = yStep < 1 ? t.toFixed(2) : t.toFixed(0);
      grid.push(`<line class="grid" x1="${padL}" y1="${Y(t)}" x2="${padL + iW}" y2="${Y(t)}"/>
                 <text class="tick y" x="${padL - 6}" y="${Y(t) + 3}">${label}</text>`);
    }

    const diagonals = f.ratios.map((r) => {
      const seg = ratioSegment(r, f);
      if (!seg) return "";
      const atRightEdge = seg.to.ext >= f.x1 - 1e-6;
      return `<line class="ratio" x1="${X(seg.from.ext)}" y1="${Y(seg.from.tds)}"
                    x2="${X(seg.to.ext)}" y2="${Y(seg.to.tds)}"/>
              <text class="rlabel ${atRightEdge ? "r" : "t"}"
                    x="${X(seg.to.ext) + (atRightEdge ? 4 : 0)}"
                    y="${Y(seg.to.tds) + (atRightEdge ? 3 : -4)}">1:${r}</text>`;
    }).join("");

    const [bx0, bx1, by0, by1] = f.box;
    const box = `<rect class="ideal" x="${X(bx0)}" y="${Y(by1)}"
                       width="${X(bx1) - X(bx0)}" height="${Y(by0) - Y(by1)}"/>`;

    // The newest brew that can be drawn, which is not always the newest measured —
    // one still waiting for its yield stays off the chart until it has one.
    const p = plottable[plottable.length - 1];
    const current = isCurrent(p, this._lastBrewAt());

    // Oldest faintest, so the drift is legible as a direction and not just a cloud.
    // The newest keeps full opacity either way — it is still the last thing measured
    // — but only wears the highlight while it is also the last thing brewed.
    const dots = plottable.map((q, i) => {
      const newest = i === plottable.length - 1;
      const lit = newest && current;
      const age = plottable.length > 1 ? i / (plottable.length - 1) : 1;
      return `<circle class="${lit ? "dot last" : "dot"}"
                      cx="${X(q.ext)}" cy="${Y(q.tds)}" r="${lit ? 6 : 4}"
                      opacity="${newest ? 1 : (0.25 + age * 0.45).toFixed(2)}"/>`;
    }).join("");

    root.innerHTML = `
      <ha-card header="${cardTitle("Extraction", p.at, this._hass)}">
        <div class="body">
          <div class="when">${current ? "" : staleNote(this._lastBrewAt(), this._hass)}</div>
          <svg viewBox="0 0 ${W} ${H}" role="img"
               aria-label="Brewing control chart: TDS against extraction">
            ${grid.join("")}
            ${box}
            ${diagonals}
            ${dots}
            <text class="axis" x="${padL - 6}" y="${padT - 4}">TDS</text>
            <text class="axis end" x="${padL + iW}" y="${H - 10}">EXT %</text>
          </svg>
          <div class="legend">${legendRow(p)}
          </div>
        </div>
        ${DifluidControlCard.STYLE}
      </ha-card>`;
  }
}

DifluidControlCard.STYLE = `
  <style>
    .body { padding: 4px 12px 12px; }
    .empty { padding: 24px 16px; color: var(--secondary-text-color); line-height: 1.5; }
    .when { text-align: right; font-size: 11px; color: var(--secondary-text-color);
            font-variant-numeric: tabular-nums; padding-bottom: 2px; }
    svg { width: 100%; height: auto; display: block; }
    .grid { stroke: var(--divider-color); stroke-width: .5; }
    .ratio { stroke: var(--secondary-text-color); stroke-width: 1; opacity: .55; }
    .ideal { fill: none; stroke: var(--primary-text-color); stroke-width: 1.5; opacity: .8; }
    .dot { fill: var(--state-icon-color, #44739e); }
    .dot.last { fill: var(--error-color, #db4437); }
    .tick, .rlabel, .axis { font-size: 10px; fill: var(--secondary-text-color); }
    .tick.x, .rlabel.t { text-anchor: middle; }
    .tick.y { text-anchor: end; }
    .rlabel.r { text-anchor: start; }
    .axis.end { text-anchor: end; }
    .legend { display: flex; align-items: center; gap: 8px; font-size: 12px;
              padding-top: 4px; color: var(--primary-text-color); }
    .legend .cap { font-weight: 500; }
    .legend .brew { margin-left: auto; font-variant-numeric: tabular-nums;
                    color: var(--secondary-text-color); }
  </style>`;

if (!customElements.get("difluid-card")) {
  customElements.define("difluid-card", DifluidCard);
}
if (!customElements.get("difluid-control-card")) {
  customElements.define("difluid-control-card", DifluidControlCard);
}
if (!customElements.get("difluid-pour-card")) {
  customElements.define("difluid-pour-card", DifluidPourCard);
}
if (!customElements.get("difluid-card-editor")) {
  customElements.define("difluid-card-editor", DifluidCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "difluid-card")) {
  window.customCards.push({
    type: "difluid-card",
    name: "DiFluid Microbalance / R2",
    description:
      "Ordered sensors + controls for a DiFluid scale or R2 refractometer.",
    preview: true,
    documentationURL:
      "https://github.com/eryepa/difluid_for_home_assistant",
  });
}
if (!window.customCards.some((c) => c.type === "difluid-control-card")) {
  window.customCards.push({
    type: "difluid-control-card",
    name: "DiFluid Extraction Chart",
    description:
      "Measured brews on the TDS/extraction control chart, with ratio diagonals.",
    preview: true,
    documentationURL:
      "https://github.com/eryepa/difluid_for_home_assistant",
  });
}
if (!window.customCards.some((c) => c.type === "difluid-pour-card")) {
  window.customCards.push({
    type: "difluid-pour-card",
    name: "DiFluid Pour Curve",
    description:
      "Weight and flow against seconds, for the pour happening now or the last one.",
    preview: true,
    documentationURL:
      "https://github.com/eryepa/difluid_for_home_assistant",
  });
}

console.info("%c DiFluid card loaded", "color:#5eead4;font-weight:bold;");
