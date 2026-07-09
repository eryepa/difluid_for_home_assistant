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
  "auto_disconnect", "disconnect", "shutdown", "auto",
];

const rank = (entityId, order) => {
  const id = entityId.split(".")[1] || entityId;
  for (let i = 0; i < order.length; i++) if (id.includes(order[i])) return i;
  return order.length + 1;
};

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
    const dev = Object.values(hass.devices || {}).find((d) =>
      (d.identifiers || []).some((ident) => ident[0] === DOMAIN)
    );
    return { type: "custom:difluid-card", device: dev ? dev.id : "" };
  }

  // ── entity resolution ─────────────────────────────────────────────────────
  _deviceEntities() {
    const hass = this._hass;
    const deviceId = this._config.device;
    const ids = [];
    for (const [entityId, ent] of Object.entries(hass.entities || {})) {
      if (ent.device_id !== deviceId) continue;
      if (ent.disabled_by) continue;
      if (!(entityId in hass.states)) continue;
      ids.push(entityId);
    }
    return ids;
  }

  _deviceName() {
    const dev = (this._hass.devices || {})[this._config.device];
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
    `;
    card.appendChild(style);

    const ids = this._deviceEntities();
    const sensors = ids
      .filter((id) => id.startsWith("sensor."))
      .sort((a, b) => rank(a, SENSOR_ORDER) - rank(b, SENSOR_ORDER));
    const controls = ids
      .filter((id) => /^(button|select|number|switch)\./.test(id))
      .sort((a, b) => rank(a, CONTROL_ORDER) - rank(b, CONTROL_ORDER));

    this._rows = [];

    for (const id of sensors) body.appendChild(this._sensorRow(id));
    if (sensors.length && controls.length) {
      const div = document.createElement("div");
      div.className = "divider";
      body.appendChild(div);
    }
    for (const id of controls) body.appendChild(this._controlRow(id));

    if (!sensors.length && !controls.length) {
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
      control.textContent = "Press";
      control.addEventListener("click", () =>
        this._hass.callService("button", "press", { entity_id: id })
      );
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
    this._rows.push({ id, kind: domain, icon, control });
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

      if (r.kind === "sensor") {
        r.value.textContent = this._formatState(st);
      } else if (r.kind === "select" && r.control) {
        if (document.activeElement !== r.control && r.control.value !== st.state)
          r.control.value = st.state;
      } else if (r.kind === "number" && r.control) {
        if (document.activeElement !== r.control)
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
