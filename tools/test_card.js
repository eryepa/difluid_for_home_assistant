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
      " CONTROL_FRAMES, ESPRESSO_TDS, brewLabel, legendRow };",
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
// is the only part of the chain a harness with no DOM cannot execute.
check(
  "the chart's legend is the one tested above",
  (() => {
    const src = fs.readFileSync(CARD, "utf8");
    // From the control card, not the pour card — both have a legend.
    const legend = src.slice(
      src.indexOf('<div class="legend">', src.indexOf("class DifluidControlCard"))
    );
    return legend.slice(0, legend.indexOf("</div>")).replace(/\s+/g, " ").trim();
  })(),
  '<div class="legend">${legendRow(p)}'
);

check(
  "the chart reads all seven fields of a stored point",
  (() => {
    const points = loadPointsFn().call({
      _extractionEntity: () => "sensor.brew_detector_extraction",
      _hass: {
        states: {
          "sensor.brew_detector_extraction": {
            attributes: { points: [[1787723068.9, 22.92, 11.03, 2.08, 18, 37.4, 20.4]] },
          },
        },
      },
    });
    return points;
  })(),
  [{ at: 1787723068.9, ext: 22.92, tds: 11.03, ratio: 2.08,
     dose: 18, yieldG: 37.4, seconds: 20.4 }]
);

console.log(ok ? "\nOK" : "\nFAILED");
process.exit(ok ? 0 : 1);
