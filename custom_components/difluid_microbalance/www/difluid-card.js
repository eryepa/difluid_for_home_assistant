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

if (!customElements.get("difluid-card")) {
  customElements.define("difluid-card", DifluidCard);
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

console.info("%c DiFluid card loaded", "color:#5eead4;font-weight:bold;");
