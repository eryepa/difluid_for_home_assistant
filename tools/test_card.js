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
      " STATS_ORDER, DIAG_ORDER, rank, inList, isStat, statRank, DOMAIN };",
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

console.log(ok ? "\nOK" : "\nFAILED");
process.exit(ok ? 0 : 1);
