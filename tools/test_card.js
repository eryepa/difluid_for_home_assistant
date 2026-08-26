/**
 * Where difluid-card puts each entity.
 *
 *     node tools/test_card.js
 *
 * The card enumerates every entity its device has and sorts them into four groups by
 * matching entity_id against four ordered lists.  Nothing checks those lists against
 * reality, and a key that matches nothing does not raise — it falls through into the
 * plain sensor list, in among the weight and the flow rate.  That is exactly how
 * Coffee Ground and Coffee Ground (Period) ended up there in 1.4.0-beta.12: the lists
 * were written from the `key` of each sensor description, but entity_ids are built
 * from the display *name*, so key="coffee_total" name="Coffee Ground" registers as
 * `..._coffee_ground` and matched nothing.
 *
 * So this fixture is a list of real entity_ids, copied from what Home Assistant
 * reports for the kitchen scale, run through the card's own partition.  It cannot
 * confirm that the card renders — that needs a browser — only that every entity lands
 * in the group it was meant for.
 *
 * The constants and helpers are read out of difluid-card.js rather than repeated here.
 * A copy would pass forever after someone edited the card and forgot this file.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CARD = path.join(
  __dirname, "..", "custom_components", "difluid_microbalance", "www", "difluid-card.js"
);

// Everything above `class DifluidCard` is constants and pure helpers; below it is DOM.
function loadCardHelpers() {
  const src = fs.readFileSync(CARD, "utf8");
  const cut = src.indexOf("\nclass DifluidCard");
  if (cut < 0) throw new Error(`no 'class DifluidCard' in ${CARD} — did the file move?`);
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(
    src.slice(0, cut) +
      "\n;globalThis.__exported = { SENSOR_ORDER, CONTROL_ORDER, EXCLUDE_CONTROLS," +
      " STATS_ORDER, DIAG_ORDER, rank, inList, isStat, statRank, DOMAIN," +
      " pourWindow, cleanSamples, linePath, controlFrame, ratioSegment," +
      " CONTROL_FRAMES, ESPRESSO_TDS, brewLabel, legendRow, whenLabel, whenRow," +
      " isCurrent, staleNote };",
    sandbox,
    { filename: CARD }
  );
  return sandbox.__exported;
}

// _clusterIds is a method on the card class, so it cannot be lifted out of the file
// the way the pure helpers above are.  It is read out of the source and called with a
// hand-made `this` instead — which still fails if someone edits it, and that is the
// point: a reimplementation here would pass forever.
function loadClusterFn() {
  const src = fs.readFileSync(CARD, "utf8");
  const start = src.indexOf("  _clusterIds() {");
  if (start < 0) throw new Error("no _clusterIds() in the card — did it get renamed?");
  const end = src.indexOf("\n  }", start);
  const body = src.slice(src.indexOf("{", start) + 1, end);
  return new Function("DOMAIN", `return function () {${body}\n};`)(DOMAIN_NAME);
}

// Lifted out of the class the same way _clusterIds is, and for the same reason: the
// mapping from the sensor's positional `points` attribute to what the chart draws is
// exactly where a field added on the Python side goes missing on the JavaScript one.
function loadPointsFn() {
  const src = fs.readFileSync(CARD, "utf8");
  const start = src.indexOf("  _points() {");
  if (start < 0) throw new Error("no _points() in the card — did it get renamed?");
  const end = src.indexOf("\n  }", start);
  const body = src.slice(src.indexOf("{", start) + 1, end);
  return new Function(`return function () {${body}\n};`)();
}

const DOMAIN_NAME = "difluid_microbalance";

// Device ids.  SCALE is the real one this install has; DETECTOR is the service device
// the brew statistics moved onto in 1.5.0, hung off the scale with via_device; R2 is
// the refractometer, which is linked to neither and must therefore stay out.
const SCALE = "4a94644ad88667b1c30c138cdfd3164f";
const DETECTOR = "d3tec70000000000000000000000000f";
const R2 = "12f1u1d12f1u1d12f1u1d12f1u1d1200";

const DEVICES = {
  [SCALE]: { id: SCALE, name: "Kitchen Microbalance 304268",
             identifiers: [[DOMAIN_NAME, "scale-entry"]] },
  [DETECTOR]: { id: DETECTOR, name: "Brew Detector", via_device_id: SCALE,
                identifiers: [[DOMAIN_NAME, "detector-entry"]] },
  [R2]: { id: R2, name: "DiFluid R2 301055",
          identifiers: [[DOMAIN_NAME, "r2-entry"]] },
  // Hung off the scale by a different integration — a Bluetooth proxy or a power
  // monitor plugged into the same socket will do this.  via_device_id alone would
  // sweep it in, so the identifiers check in _clusterIds is what keeps it out.
  "ffff": {
    id: "ffff",
    name: "Some other integration's device",
    via_device_id: SCALE,
    identifiers: [["zha", "x"]],
  },
};

// The 22 entities, in registry order, as ha_get_device reports them, each with the
// device that owns it after the split.  Registry order matters: it is the tie-breaker
// the sort falls back on, so a fixture that silently sorted them would hide the very
// ambiguity worth catching.
const ENTITIES = [
  ["sensor.microbalance_304268_weight", SCALE],
  ["sensor.microbalance_304268_flow_rate", SCALE],
  ["sensor.microbalance_304268_timer", SCALE],
  ["sensor.microbalance_304268_battery", SCALE],
  ["sensor.microbalance_304268_device_status", SCALE],
  ["button.microbalance_304268_tare", SCALE],
  ["number.microbalance_304268_auto_shutdown", SCALE],
  ["select.microbalance_304268_mode", SCALE],
  ["button.microbalance_304268_timer_start", SCALE],
  ["sensor.kitchen_microbalance_304268_last_dose", DETECTOR],
  ["sensor.kitchen_microbalance_304268_last_yield", DETECTOR],
  ["sensor.kitchen_microbalance_304268_brew_ratio", DETECTOR],
  ["sensor.kitchen_microbalance_304268_integration_version", SCALE],
  ["sensor.kitchen_microbalance_304268_brew_count", DETECTOR],
  ["sensor.kitchen_microbalance_304268_brew_dose", DETECTOR],
  ["sensor.kitchen_microbalance_304268_brew_yield", DETECTOR],
  ["sensor.kitchen_microbalance_304268_brew_count_period", DETECTOR],
  ["sensor.kitchen_microbalance_304268_brews_per_day", DETECTOR],
  ["sensor.kitchen_microbalance_304268_coffee_ground", DETECTOR],
  ["sensor.kitchen_microbalance_304268_coffee_ground_period", DETECTOR],
  ["sensor.kitchen_microbalance_304268_coffee_per_day", DETECTOR],
  ["button.kitchen_microbalance_304268_reset_period", DETECTOR],
];

const IDS = ENTITIES.map(([id]) => id);

// The partition from _build(), kept in the same order — statistics and diagnostics
// claim their rows first, and what is left is a live reading or a control.
function partition(h, ids) {
  const stats = ids.filter(h.isStat).sort((a, b) => h.statRank(a) - h.statRank(b));
  const diagnostics = ids
    .filter((id) => !h.isStat(id) && h.inList(id, h.DIAG_ORDER))
    .sort((a, b) => h.rank(a, h.DIAG_ORDER) - h.rank(b, h.DIAG_ORDER));
  const claimed = new Set([...stats, ...diagnostics]);
  const sensors = ids
    .filter((id) => id.startsWith("sensor.") && !claimed.has(id))
    .sort((a, b) => h.rank(a, h.SENSOR_ORDER) - h.rank(b, h.SENSOR_ORDER));
  const controls = ids
    .filter((id) => /^(button|select|number|switch)\./.test(id))
    .filter((id) => !claimed.has(id))
    .filter((id) => !h.EXCLUDE_CONTROLS.some((x) => id.includes(x)))
    .sort((a, b) => h.rank(a, h.CONTROL_ORDER) - h.rank(b, h.CONTROL_ORDER));
  return { stats, diagnostics, sensors, controls };
}

const short = (ids) =>
  ids.map((x) => x.split(".")[1].replace(/^(kitchen_)?microbalance_304268_/, ""));

let ok = true;
function check(label, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  const pass = g === w;
  if (!pass) ok = false;
  console.log(`  ${pass ? "PASS" : "FAIL"}  ${label}\n        ${g}` +
              (pass ? "" : `\n   want ${w}`));
}

const h = loadCardHelpers();
const { stats, diagnostics, sensors, controls } = partition(h, IDS);

console.log("difluid-card — where each entity lands\n");

check(
  "Statistics reads total -> period -> per day for each quantity, then the reset",
  short(stats),
  ["brew_count", "brew_count_period", "brews_per_day",
   "coffee_ground", "coffee_ground_period", "coffee_per_day", "reset_period"]
);

// The point of the whole layout: the top of the card is what the scale reads now.
check(
  "Sensors holds only instantaneous readings — no statistic leaked in",
  short(sensors),
  ["weight", "flow_rate", "timer", "device_status", "battery"]
);

check(
  "Diagnostic holds the last shot's working parts",
  short(diagnostics),
  ["brew_dose", "brew_yield", "brew_ratio", "last_dose", "last_yield",
   "integration_version"]
);

check(
  "the scale's own buttons stay reachable from the dashboard",
  short(controls),
  ["tare", "timer_start", "mode"]
);

// Equal ranks leave the sort with nothing to decide, and the row order falls back to
// the order Home Assistant registered the entities in.  A fresh install can register
// them in another order, so "it looks right here" is not the same as "it is ordered".
check(
  "each statistic ranks distinctly, so the order is the card's and not the registry's",
  new Set(stats.map(h.statRank)).size,
  stats.length
);

// Auto-disconnect Bluetooth is deliberately excluded, so 21 of 22 are placed.  Any
// other entity going missing means it matched nothing and nothing noticed.
check(
  "every entity is placed exactly once, bar the excluded auto-disconnect",
  stats.length + diagnostics.length + sensors.length + controls.length,
  IDS.length - 1
);

// ── the device cluster ──────────────────────────────────────────────────────
// The rows above only exist if the card looks at both devices.  Before 1.5.0 it
// filtered on a single device_id, which after the split would have shown a card of
// weight and flow with no statistics — or, configured on the detector, statistics
// with no weight.
const clusterFn = loadClusterFn();
const clusterFrom = (device) =>
  [...clusterFn.call({ _hass: { devices: DEVICES }, _config: { device } })].sort();

const both = [SCALE, DETECTOR].sort();

check(
  "a card configured on the scale reaches the detector hanging off it",
  clusterFrom(SCALE),
  both
);

// The direction that keeps existing dashboards working: every card config written
// before the split names the scale, but a card added after it may well name the
// detector, and the two must produce the same rows.
check(
  "a card configured on the detector reaches back up to the scale",
  clusterFrom(DETECTOR),
  both
);

// The R2 has no via_device link to either, so it must not be swept in — its TDS rows
// are iteration 2, and until then a card silently growing three temperature readings
// would be a surprise, not a feature.
check(
  "the R2 and other integrations' devices stay out of the cluster",
  clusterFrom(R2),
  [R2]
);

// What the picker offers on a fresh card.  Reached through the same source, so a
// rename of via_device handling breaks this too.
const entitiesFor = (device) => {
  const cluster = clusterFn.call({ _hass: { devices: DEVICES }, _config: { device } });
  return ENTITIES.filter(([, dev]) => cluster.has(dev)).map(([id]) => id);
};
check(
  "configured either way, the card draws the same 22 entities",
  entitiesFor(DETECTOR).length,
  entitiesFor(SCALE).length
);

// ── the pour curve ──────────────────────────────────────────────────────────
// The 2026-08-25 19:26 brew, as Home Assistant recorded it: 17.8 g in, 37.4 g out
// over 16.3 s. Trimmed to the samples that decide something — the start of the rise,
// a few points along it, the top, and every reading that is not part of the pour.
console.log("\ndifluid-pour-card — the last pour\n");

const YIELD_ATTRS = {
  detected_at: "2026-08-25T16:27:05.061624+00:00",
  plateau_seconds: 9.8,
  rise_seconds: 16.3,
  steps: [37.4],
};

const t = (iso) => Date.parse(iso);
const REAL_WEIGHT = [
  // Recorded at 16:26:35 and then nothing until the rise: a state is written when it
  // changes, so an idle scale produces no samples at all.  The baseline the chart
  // draws from does not come from here — see the carried-forward row below.
  { t: t("2026-08-25T16:26:35.000Z"), v: 0.0 },
  { t: t("2026-08-25T16:26:38.543Z"), v: 0.5 },   // rise starts
  { t: t("2026-08-25T16:26:46.425Z"), v: 12.2 },
  { t: t("2026-08-25T16:26:55.034Z"), v: 36.8 },
  { t: t("2026-08-25T16:26:55.231Z"), v: 37.0 },
  { t: t("2026-08-25T16:26:55.584Z"), v: -102.2 },  // knock, mid-pour
  { t: t("2026-08-25T16:26:55.784Z"), v: 37.3 },
  { t: t("2026-08-25T16:26:56.505Z"), v: 37.4 },   // settled
  { t: t("2026-08-25T16:27:04.652Z"), v: -200.4 }, // cup coming off
  { t: t("2026-08-25T16:27:04.861Z"), v: -142.7 },
  { t: t("2026-08-25T16:27:05.504Z"), v: 160.0 },
  { t: t("2026-08-25T16:27:05.706Z"), v: 685.4 },
  { t: t("2026-08-25T16:27:05.905Z"), v: 831.6 },
];

const win = h.pourWindow(YIELD_ATTRS);
const POUR_LEAD = 2;  // seconds of lead-in the card asks for; see POUR_LEAD_SECONDS

// detected_at is when the detector *decided*, and what makes it decide is the cup
// being lifted. Ending the chart there means drawing the lift.
check(
  "the window ends when the pour stops, not when the detector says so",
  [
    new Date(win.pourEnd).toISOString(),
    new Date(win.end).toISOString() < YIELD_ATTRS.detected_at,
  ],
  ["2026-08-25T16:26:55.261Z", true]
);

check(
  "the rise is the 16.3 s the detector measured, with a lead-in before it",
  [(win.pourEnd - win.riseStart) / 1000, (win.riseStart - win.start) / 1000],
  [16.3, 2]
);

// What history_during_period actually returns for this window: the state carried
// forward to the window's start, then every change inside it.  The 16:26:35 reading
// is nearly four seconds before the window and is not one of them — it arrives as
// that carried-forward row instead, which is where the flat baseline comes from.
const carried = [
  { t: win.start, v: 0.0 },
  ...REAL_WEIGHT.filter((s) => s.t >= win.start && s.t <= win.end),
];

// Everything after the pour ends is the cup being taken away.
check(
  "the lift never enters the window",
  carried.map((s) => s.v),
  [0, 0.5, 12.2, 36.8, 37, -102.2, 37.3, 37.4]
);

check(
  "the curve opens on a flat baseline rather than mid-rise",
  [(carried[1].t - carried[0].t) / 1000 <= POUR_LEAD, carried[0].v],
  [true, 0]
);

// The knock at -102.2 g survives the window, and it is the one that matters: a single
// negative sample sets the y-axis floor and flattens the curve against it.
const cleaned = h.cleanSamples(carried, 37.4);
check(
  "the mid-pour knock is dropped, and nothing else is",
  cleaned.map((s) => s.v),
  [0, 0.5, 12.2, 36.8, 37, 37.3, 37.4]
);

check(
  "the y-axis is set by the pour, not by the knock",
  [Math.min(...cleaned.map((s) => s.v)), Math.max(...cleaned.map((s) => s.v))],
  [0, 37.4]
);

// A pour that overshoots or gets topped up is the reason somebody opens this card;
// the filter must not tidy those away along with the knocks.
check(
  "an overshoot well past the final yield is kept",
  h.cleanSamples([{ t: 0, v: 52 }, { t: 1, v: 37.4 }], 37.4).map((s) => s.v),
  [52, 37.4]
);

// rise_seconds is null whenever the load was already on the scale — after a restart
// or a BLE gap. There is no rise to draw, and a default would draw a fiction.
check(
  "no window is offered when the rise was never seen",
  h.pourWindow({ ...YIELD_ATTRS, rise_seconds: null }),
  null
);

check(
  "nor from attributes that are missing altogether",
  [h.pourWindow(null), h.pourWindow({})],
  [null, null]
);

check(
  "an empty series produces no path rather than a broken one",
  h.linePath([]),
  ""
);

// ── the control chart ───────────────────────────────────────────────────────
// Checked against the DiFluid app's own Pro chart, read off a screenshot of it: the
// 1:2 diagonal runs from (14, 7) to (26, 13) and 1:1 from (14, 14) to (16, 16).  Both
// follow from TDS = EXT / ratio, and reproducing them is what says our axes mean the
// same thing the app's do — a chart that disagreed would still look plausible.
console.log("\ndifluid-control-card — TDS against extraction\n");

const esp = h.CONTROL_FRAMES.espresso;

// Guarded before being read.  Get the relation backwards — TDS = EXT * r rather than
// EXT / r — and every diagonal leaves the frame entirely, so these come back null and
// the checks below would die on a TypeError.  That still fails the build, but it
// reports "cannot read properties of null" instead of naming the formula.
const seg12 = h.ratioSegment(2, esp);
const seg11 = h.ratioSegment(1, esp);
check(
  "the ratio diagonals cross the espresso frame at all",
  [seg12 !== null, seg11 !== null],
  [true, true]
);

if (seg12 && seg11) {
  check(
    "the 1:2 diagonal crosses the espresso frame where the app draws it",
    [seg12.from.ext, +seg12.from.tds.toFixed(2), seg12.to.ext, +seg12.to.tds.toFixed(2)],
    [14, 7, 26, 13]
  );

  // 1:1 leaves through the top of the frame, not the right edge — which is why the app
  // labels it up there and 1:2 on the side.
  check(
    "the 1:1 diagonal leaves through the top, at TDS 16",
    [seg11.from.ext, seg11.to.ext, +seg11.to.tds.toFixed(2)],
    [14, 16, 16]
  );
}

// A ratio whose line never enters the frame must report that rather than produce a
// degenerate segment for the renderer to draw as a dot in the corner.
check(
  "a diagonal that misses the frame is refused",
  h.ratioSegment(40, esp),
  null
);

// The brew this all started from: 17.8 g in, 37.4 g out, TDS 10.67.
const RATIO = 37.4 / 17.8;
const ESPRESSO_POINT = { at: 1, ext: +(10.67 * RATIO).toFixed(2), tds: 10.67, ratio: RATIO };

// 22.42 is past 22, so this shot sits just outside the right edge of the box — a
// touch over-extracted. Worth pinning as a number rather than a feeling: it is the
// difference between the chart being decorative and it telling you something.
check(
  "the measured shot lands at 22.42% EXT, just past the box",
  [
    ESPRESSO_POINT.ext,
    ESPRESSO_POINT.ext > esp.box[1],
    ESPRESSO_POINT.ext - esp.box[1] < 0.5,
    ESPRESSO_POINT.tds >= esp.box[2] && ESPRESSO_POINT.tds <= esp.box[3],
  ],
  [22.42, true, true, true]
);

// A point sits on its own ratio diagonal by construction — EXT = TDS x ratio is the
// same relation the line is drawn from.  If these ever disagree, one of the two is
// using a different formula.  Compared with a tolerance because `ext` is stored
// rounded for display; the identity is exact on the values it was computed from.
check(
  "a brew sits on the diagonal of its own ratio",
  Math.abs(ESPRESSO_POINT.ext / ESPRESSO_POINT.ratio - ESPRESSO_POINT.tds) < 0.01,
  true
);

// Filter coffee is 1.2-1.5% TDS. On an espresso frame it would sit in the bottom pixel
// row, so the frame is chosen by the coffee rather than configured.
check(
  "a filter brew switches the frame instead of vanishing off the bottom",
  [
    h.controlFrame([{ ext: 20, tds: 1.35, ratio: 15 }]).y1,
    h.controlFrame([ESPRESSO_POINT]).y1,
  ],
  [h.CONTROL_FRAMES.filter.y1, esp.y1]
);

// The shot that went wrong is the one worth looking at, so the axes give way.
const wild = h.controlFrame([{ ext: 31, tds: 14, ratio: 2.2 }]);
check(
  "a brew off the right of the frame widens it rather than falling off",
  [wild.x1 >= 31, esp.x1],
  [true, 26]
);

check(
  "an explicit box overrides the default without touching the axes",
  (() => {
    const f = h.controlFrame([ESPRESSO_POINT], [19, 21, 9, 11]);
    return [f.box, f.x0, f.x1];
  })(),
  [[19, 21, 9, 11], 14, 26]
);

// ── the legend under the chart ───────────────────────────────────────────────────
// It used to say "in the box" or "outside", which repeats what the dot's position
// already shows.  What it could not tell you was the brew: the chart plots TDS against
// extraction, and neither axis is the dose, the yield or how long the pour took.

check(
  "the legend reads the brew off the scale",
  h.brewLabel({ dose: 18, yieldG: 37.4, ratio: 2.08, seconds: 20.4 }),
  "18 → 37.4 g · 1:2.1 · 20 s"
);

// A pour whose start was never observed — a restart mid-shot, a BLE gap — stores null,
// and the line has to drop the part rather than print "0 s", which would be a claim
// about the shot instead of an admission about the recording.
check(
  "an unobserved pour loses its duration and keeps the rest",
  h.brewLabel({ dose: 18, yieldG: 37.4, ratio: 2.08, seconds: null }),
  "18 → 37.4 g · 1:2.1"
);

// What a browser holding a cached card sees against a sensor that has not been
// upgraded yet, and what every point measured before 1.7.0 looks like forever.
check(
  "a point stored before the scale figures existed still renders",
  h.brewLabel({ ratio: 2.08 }),
  "1:2.1"
);

// The rendered row, not just the formatter.  The first attempt at these tests checked
// brewLabel alone and passed with the legend hard-coded back to "outside" — a helper
// nothing calls is a helper that works perfectly.
check(
  "the rendered legend carries the reading and the brew, and no verdict",
  (() => {
    const html = h.legendRow({
      ext: 22.92, tds: 11.03, ratio: 2.08, dose: 18, yieldG: 37.4, seconds: 20.4,
    }).replace(/\s+/g, " ").trim();
    return [html, /in the box|outside/.test(html)];
  })(),
  ['<span class="cap">EXT 22.92% · TDS 11.03%</span> ' +
   '<span class="brew">18 → 37.4 g · 1:2.1 · 20 s</span>', false]
);

// …and that _render actually delegates to it rather than keeping its own copy, which
// is the only part of the chain a harness with no DOM cannot execute.  Both rows are
// checked the same way: emptying either one in the template left every assertion above
// passing, because a helper nobody calls still works perfectly.
// `after` anchors where the search starts.  The pour card has a legend of its own, and
// the control card prints a `.when` row twice — once above the chart, and once in the
// message shown when a brew was measured but has no yield to place it with.
const controlMarkup = (tag, after = "class DifluidControlCard") => {
  const src = fs.readFileSync(CARD, "utf8");
  const from = src.slice(
    src.indexOf(`<div class="${tag}">`, src.indexOf(after))
  );
  return from.slice(0, from.indexOf("</div>")).replace(/\s+/g, " ").trim();
};

check(
  "the chart's legend is the one tested above",
  controlMarkup("legend"),
  '<div class="legend">${legendRow(p)}'
);

check(
  "the chart prints the times above it, and says when the light is out",
  controlMarkup("when", "const current = isCurrent("),
  '<div class="when">${whenRow(p)}${current ? "" : staleNote(this._lastBrewAt())}'
);

// The dot itself: only the newest point may wear the highlight, and only while the
// brew it describes is still the newest one.  Checked against the template because the
// harness cannot render — isCurrent passing on its own proves nothing about the SVG.
check(
  "the highlight is tied to isCurrent, not just to being last",
  (() => {
    const src = fs.readFileSync(CARD, "utf8");
    const from = src.indexOf("const dots = plottable.map", src.indexOf("class DifluidControlCard"));
    const body = src.slice(from, src.indexOf('.join("")', from));
    return [
      /const lit = newest && current;/.test(body),
      /class="\$\{lit \? "dot last" : "dot"\}"/.test(body),
      /r="\$\{lit \? 6 : 4\}"/.test(body),
      // Full opacity for the newest either way: it is still the last thing measured.
      /opacity="\$\{newest \? 1 :/.test(body),
    ];
  })(),
  [true, true, true, true]
);

// The pour card names its brew in the header instead, and has the same blind spot.
check(
  "the pour card's header carries the time of the pour it drew",
  (() => {
    const src = fs.readFileSync(CARD, "utf8");
    const from = src.indexOf("class DifluidPourCard");
    // At the start of a line: `${DifluidPourCard.STYLE}` appears inside the class
    // first, and slicing there cuts the body off above the part being checked.
    const to = src.indexOf("\nDifluidPourCard.STYLE =");
    const body = src.slice(from, to);
    return [
      /const when = series\.live \? "" : whenLabel\(/.test(body),
      /`Last pour · \$\{when\}`/.test(body),
      body.includes('<ha-card header="${header}">'),
    ];
  })(),
  [true, true, true]
);

// ── when it was made, and when it was read ───────────────────────────────────────
// The 2026-08-26 case, at its real timestamps: the scale was off the air all morning,
// so a shot read at 10:12 was divided by the dose and yield of an 08:44 brew.  Every
// number on the card was correct and every one of them was about the wrong coffee.
// Two clock times are the only thing that shows it.
const BREW_0844 = 1787723068.9;                 // 2026-08-26 08:44:28 +03
const READ_1012 = 1787728377.1;                 // 2026-08-26 10:12:57 +03
const SAME_DAY = READ_1012 * 1000;              // "now", for the is-it-today branch

// Asserted on structure, not on the rendered string.  The first attempt pinned
// "brewed 08:44 · measured 10:12" and failed under Node, which formats 08:44 AM — and
// that is not a bug to fix but the feature working: the time follows the reader's
// locale, so a hard-coded expectation would be testing Intl's defaults on whichever
// machine happened to run it.
const clocks = (s) => s.match(/\d{1,2}:\d{2}/g) || [];

check(
  "a reading taken well after the brew shows both times",
  (() => {
    const row = h.whenRow({ at: BREW_0844, measuredAt: READ_1012 }, SAME_DAY);
    return [clocks(row).length, row.startsWith("brewed "), row.includes("measured ")];
  })(),
  [2, true, true]
);

// Measuring the cup you just pulled shows two times a minute apart, which is the
// honest rendering of what happened.  Pinned because the tempting alternative — hide
// the reading time when it is "close enough" — puts a threshold on the clock, and
// this is the case that sits right on it.
check(
  "a shot measured a minute later still shows both times",
  (() => {
    const row = h.whenRow({ at: READ_1012, measuredAt: READ_1012 + 60 }, SAME_DAY);
    return [clocks(row).length, row.includes("measured ")];
  })(),
  [2, true]
);

// A point from before 1.7.0 has no measured time at all; the brew time still shows.
check(
  "a point with no reading time still says when it was brewed",
  (() => {
    const row = h.whenRow({ at: BREW_0844 }, SAME_DAY);
    return [clocks(row).length, row.includes("measured ")];
  })(),
  [1, false]
);

// Yesterday's shot must not read as a bare "08:44" next to today's, or the chart's
// history becomes a column of times with no days attached.  Which side the date falls
// on is the locale's business; that there is one is not.
check(
  "a brew from another day carries a date and today's does not",
  (() => {
    const today = h.whenLabel(BREW_0844, SAME_DAY);
    const older = h.whenLabel(BREW_0844, SAME_DAY + 86400000);
    return [clocks(today).length, older.length > today.length, older.includes(today)];
  })(),
  [1, true, true]
);

// ── the highlight goes out when the cup is no longer yours ───────────────────────
// The red dot means "this is the cup you are drinking".  Pull another shot without
// measuring it and that stops being true, silently: the chart goes on highlighting the
// cup before the one in your hand.

check(
  "the newest measured brew stays lit while it is also the newest brewed",
  h.isCurrent({ at: BREW_0844 }, BREW_0844),
  true
);

check(
  "a shot pulled after the last measurement puts the highlight out",
  h.isCurrent({ at: BREW_0844 }, BREW_0844 + 600),
  false
);

// Measuring that newer shot lights it again — the same call, with the point that the
// new measurement produced.
check(
  "measuring the newer shot lights it again",
  h.isCurrent({ at: BREW_0844 + 600 }, BREW_0844 + 600),
  true
);

// A detector that has never completed a pair publishes null, and a card must not read
// that as "everything is stale" and put out a light it never lit.
check(
  "nothing known about the last brew leaves the highlight alone",
  [h.isCurrent({ at: BREW_0844 }, null), h.isCurrent(null, BREW_0844 + 600)],
  [true, true]
);

// A measurement anchored on a dose, with the pour never seen, is *newer* than the last
// completed pair.  It must not be called stale by the pair it postdates.
check(
  "a dose-anchored measurement is not stale against an older pair",
  h.isCurrent({ at: READ_1012 }, BREW_0844),
  true
);

check(
  "the line says which brew put the light out",
  (() => {
    const note = h.staleNote(READ_1012, SAME_DAY);
    return [note.includes("not measured"), (note.match(/\d{1,2}:\d{2}/g) || []).length];
  })(),
  [true, 1]
);

// ── a cup whose pour the scale never saw ─────────────────────────────────────────
// It is a real measurement with a real TDS and no place on an extraction axis.  The
// card has to keep it, say so, and not call it "nothing measured yet".
check(
  "a brew with no yield keeps its dose and loses the rest of the line",
  h.brewLabel({ dose: 18.1, yieldG: null, ratio: null, seconds: null }),
  "18.1 g"
);

check(
  "a yieldless point survives parsing but cannot be plotted",
  (() => {
    const points = loadPointsFn().call({
      _extractionEntity: () => "sensor.brew_detector_extraction",
      _hass: {
        states: {
          "sensor.brew_detector_extraction": {
            attributes: {
              points: [[BREW_0844, null, 10.13, null, 18.1, null, null, READ_1012]],
            },
          },
        },
      },
    });
    // Guarded rather than indexed straight: the failure being checked for is the
    // point being dropped, and reading .tds off nothing throws a TypeError that kills
    // the whole run instead of reporting one failed assertion.
    const p = points[0];
    return [points.length, p ? p.tds : null, p ? Number.isFinite(p.ext) : null];
  })(),
  [1, 10.13, false]
);

// The frame is built from the plottable points, and a null ext must not drag an axis
// to NaN — one such point would blank the whole chart.
check(
  "a yieldless point cannot poison the axes",
  (() => {
    const f = h.controlFrame([
      { ext: null, tds: 10.13, ratio: null },
      ESPRESSO_POINT,
    ]);
    return [Number.isFinite(f.x0), Number.isFinite(f.x1), Number.isFinite(f.y1)];
  })(),
  [true, true, true]
);

check(
  "the chart reads all eight fields of a stored point",
  (() => {
    const points = loadPointsFn().call({
      _extractionEntity: () => "sensor.brew_detector_extraction",
      _hass: {
        states: {
          "sensor.brew_detector_extraction": {
            attributes: {
              points: [[BREW_0844, 22.92, 11.03, 2.08, 18, 37.4, 20.4, READ_1012]],
            },
          },
        },
      },
    });
    return points;
  })(),
  [{ at: BREW_0844, ext: 22.92, tds: 11.03, ratio: 2.08,
     dose: 18, yieldG: 37.4, seconds: 20.4, measuredAt: READ_1012 }]
);

console.log(ok ? "\nOK" : "\nFAILED");
process.exit(ok ? 0 : 1);
