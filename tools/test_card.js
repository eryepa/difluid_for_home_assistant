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
      " STATS_ORDER, DIAG_ORDER, rank, inList, isStat, statRank };",
    sandbox,
    { filename: CARD }
  );
  return sandbox.__exported;
}

// The scale's 22 entities, in registry order, as ha_get_device reports them.  Registry
// order matters: it is the tie-breaker the sort falls back on, so a fixture that
// silently sorted them would hide the very ambiguity worth catching.
const IDS = [
  "sensor.microbalance_304268_weight",
  "sensor.microbalance_304268_flow_rate",
  "sensor.microbalance_304268_timer",
  "sensor.microbalance_304268_battery",
  "sensor.microbalance_304268_device_status",
  "button.microbalance_304268_tare",
  "number.microbalance_304268_auto_shutdown",
  "select.microbalance_304268_mode",
  "button.microbalance_304268_timer_start",
  "sensor.kitchen_microbalance_304268_last_dose",
  "sensor.kitchen_microbalance_304268_last_yield",
  "sensor.kitchen_microbalance_304268_brew_ratio",
  "sensor.kitchen_microbalance_304268_integration_version",
  "sensor.kitchen_microbalance_304268_brew_count",
  "sensor.kitchen_microbalance_304268_brew_dose",
  "sensor.kitchen_microbalance_304268_brew_yield",
  "sensor.kitchen_microbalance_304268_brew_count_period",
  "sensor.kitchen_microbalance_304268_brews_per_day",
  "sensor.kitchen_microbalance_304268_coffee_ground",
  "sensor.kitchen_microbalance_304268_coffee_ground_period",
  "sensor.kitchen_microbalance_304268_coffee_per_day",
  "button.kitchen_microbalance_304268_reset_period",
];

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

console.log(ok ? "\nOK" : "\nFAILED");
process.exit(ok ? 0 : 1);
