# DiFluid for Home Assistant

[🇷🇺 Русская версия](README.md)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/eryepa/difluid_for_home_assistant)](https://github.com/eryepa/difluid_for_home_assistant/releases)
[![License](https://img.shields.io/github/license/eryepa/difluid_for_home_assistant)](LICENSE)

A custom Home Assistant integration for **[DiFluid](https://digitizefluid.com/)** devices over Bluetooth Low Energy (BLE):

- **DiFluid Microbalance** and **Microbalance Ti** — espresso scales (weight, flow rate, timer, battery)
- **DiFluid R2 Extract** — refractometer (TDS / concentration, refractive index, temperatures)

All communication is **fully local** via BLE. Works with an **ESPHome Bluetooth Proxy** to reach devices far from the HA server.

> **Note**: newer firmware encrypts BLE traffic. The integration handles the DiFluid cloud handshake automatically — no license key required.

---

## Features

- Auto-discovery by BLE advertisement name (`Microbalance…`, `DiFluid R2…`) and by service UUID
- Manual MAC address entry when auto-discovery doesn't find the device
- Weight & flow are high-priority push notifications; battery / status are polled once per second
- Instant reconnect when the device powers on (BT advertisement callback) + endless reconnect loop
- Control buttons: Tare, Timer Start/Stop (scale), Start Test (R2)
- Encrypted firmware support via automatic DiFluid cloud handshake
- ESPHome Bluetooth Proxy support

---

## Supported Devices

| Device | Service UUID | Encrypted firmware |
|---|---|---|
| Microbalance | `000000EE` | Handled automatically |
| Microbalance Ti | `000000DD` | Handled automatically |
| R2 Extract | `000000FF` | Handled automatically |

---

## Installation

### Via HACS (recommended)

1. HACS → ⋮ (top-right menu) → **Custom repositories**
2. Add `https://github.com/eryepa/difluid_for_home_assistant`, category **Integration**
3. Find **DiFluid Microbalance & R2** in the list and click **Download**
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/difluid_microbalance` folder to `config/custom_components/` on your Home Assistant instance
2. Restart Home Assistant

---

## Setup

1. **Settings → Devices & Services → Add Integration**
2. Search for **DiFluid Microbalance & R2**
3. If the device appears in the scan list — select it and confirm
4. If nothing appears — choose **Enter MAC address manually**, enter the MAC (`AA:BB:CC:DD:EE:FF`) and select the device type

> The MAC address can be found in the official DiFluid app or via **nRF Connect** on your phone.

---

## Entities

### Microbalance — sensors

Display order: Weight → Flow Rate → Timer → Device Status → Battery.

| Entity | Description |
|---|---|
| **Weight** | Weight (g / oz / gr) — high-priority push notification |
| **Flow Rate** | Flow rate (g/s) — push notification |
| **Timer** | Timer (seconds) — push notification |
| **Device Status** | Device status (Idle, Timing in Progress, Timer Pause, Tare in Progress, …) |
| **Battery** | Battery level (%). While charging the icon shows a lightning bolt — there is no separate "Charging" sensor. |

> Secondary values (status, battery, charging) are polled **once per second**. Weight and flow are independent — they arrive as separate push notifications.

### Microbalance — controls

| Entity | Description |
|---|---|
| **Tare** button | Zero the scale (Power Button single click) |
| **Start/Stop** button | Start or resume the timer (DLink Button single click) |
| **Mode** selector | `Manual` / `Espresso` / `Pour Over` — controls Auto Detect Timing and Auto Stop Timing |
| **Auto-disconnect Bluetooth** number | Drop BLE after N minutes of no weight change (0 = disabled). The scale then powers off via its own hardware timer. |

### R2 Extract — sensors

| Entity | Description |
|---|---|
| `concentration` | TDS / concentration (%) |
| `refractive_index` | Refractive index |
| `prism_temperature` | Prism temperature |
| `sample_temperature` | Sample temperature |
| `test_status` | Test status (Test Finished, Average Test Ongoing, …) |

### R2 Extract — controls

| Entity | Description |
|---|---|
| **Start Test** button | Trigger a single measurement |

> R2 does not stream data continuously — results arrive only after a measurement completes (either from the **Start Test** button or the physical TEST button on the device). R2 has no BLE auto-disconnect entity: it powers off on its own hardware timer regardless of the connection.

---

## Modes (Microbalance)

The **Mode** selector maps to two device settings:

| Mode | Auto Detect Timing | Auto Stop Timing |
|---|---|---|
| Manual | Off | Off |
| Espresso | On | Off |
| Pour Over | On | On |

---

## Auto-disconnect Bluetooth (Microbalance)

Set **Auto-disconnect Bluetooth** to a number of minutes (1–60). When the weight hasn't changed for that long:

1. The BLE connection is dropped
2. Reconnection is suppressed for 60 seconds
3. The scale powers off via its own hardware auto-off timer
4. When the scale is turned on again, the integration reconnects automatically

Set to **0** to disable. The value is restored across HA restarts.

---

## Encrypted Firmware

Newer Microbalance and all R2 devices **encrypt** BLE traffic (`DA DA …` header instead of `DF DF`). The integration detects this automatically and performs a three-step cloud handshake with the DiFluid server (`cmd1 → cmd2 → cmd3 → enableCleartext`).

**No license key needed** — DiFluid removed the key restriction. Leave **License Key** blank if prompted (earlier versions of this integration showed that field).

For **older, unencrypted** scales the handshake is skipped entirely — no internet access required.

---

## How It Works

1. HA registers the BLE service UUIDs in `manifest.json` for automatic discovery; devices are also matched by advertisement name prefix
2. On connect, the integration subscribes to BLE notifications and sends **AUTO_SEND_ON** (`Func=1, Cmd=0, Data=01`) to start the data stream
3. Weight / flow rate arrive as push notifications; battery and status are fetched at connect time and then polled once per second
4. For encrypted firmware, the cloud handshake runs first; data is then read from the cleartext channel
5. On disconnect, auto-reconnect uses the BT advertisement callback for instant reconnect when the device is powered on, backed by an endless retry loop (5 → 10 → 20 → 40 → 80 → 120 s, capped at 2 min) that never gives up

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Device not found during setup | Enter the MAC address manually (Settings → Devices → Add Integration → DiFluid → Enter MAC manually) |
| Sensors show `0`, logs contain `DA DA` packets | Encrypted firmware — the integration runs the handshake automatically. Check that HA has internet access. |
| `BLE device … not found` | Device is out of BLE range or off; or no Bluetooth adapter / ESPHome proxy configured |
| Frequent disconnects via ESPHome proxy | Keep the proxy log level at `INFO`, not `VERY_VERBOSE` — verbose logging causes BLE instability |

### Enable debug logging

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.difluid_microbalance: debug
```

---

## Credits

- BLE protocol documentation and SDK demo: [DiFluid/difluid-sdk-demo](https://github.com/DiFluid/difluid-sdk-demo)
- BLE stack: [`bleak`](https://github.com/hbldh/bleak) and [`bleak-retry-connector`](https://github.com/Bluetooth-Devices/bleak-retry-connector)
