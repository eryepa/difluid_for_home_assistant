# DiFluid для Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/eryepa/difluid_for_home_assistant)](https://github.com/eryepa/difluid_for_home_assistant/releases)
[![License](https://img.shields.io/github/license/eryepa/difluid_for_home_assistant)](LICENSE)

🇷🇺 Русский · 🇬🇧 [English version below](#difluid-for-home-assistant-english)

Кастомная интеграция Home Assistant для устройств **[DiFluid](https://digitizefluid.com/)**, подключаемых по Bluetooth Low Energy:

- **DiFluid Microbalance** и **Microbalance Ti** — кофейные весы (вес, скорость потока, таймер, батарея)
- **DiFluid R2 Extract** — рефрактометр (TDS/концентрация, показатель преломления, температуры)

Связь полностью **локальная**, через BLE. Работает через **ESPHome Bluetooth Proxy** — весы можно держать далеко от сервера HA.

> **Примечание**: новые прошивки шифруют BLE-трафик. Интеграция выполняет облачный handshake DiFluid автоматически — лицензионный ключ не нужен.

---

## Возможности

- Автообнаружение по имени BLE-рекламы (`Microbalance…`, `DiFluid R2…`) и по Service UUID
- Ручной ввод MAC-адреса, если автообнаружение не сработало
- Вес и поток — push-уведомления с максимальным приоритетом; батарея / статус опрашиваются раз в секунду
- Мгновенный реконнект при включении устройства (BT advertisement callback) + бесконечный цикл переподключения
- Кнопки управления: Tare, Start/Stop таймера (весы), Start Test (R2)
- Поддержка зашифрованной прошивки через автоматический handshake
- Работа через ESPHome Bluetooth Proxy
- Собственная иконка встроена в интеграцию (папка `brand/`, отображается в HA 2026.3+)

---

## Поддерживаемые устройства

| Устройство | Service UUID | Зашифрованная прошивка |
|---|---|---|
| Microbalance | `000000EE` | Автоматически |
| Microbalance Ti | `000000DD` | Автоматически |
| R2 Extract | `000000FF` | Автоматически |

---

## Установка

### Через HACS (рекомендуется)

1. HACS → ⋮ (меню вверху справа) → **Custom repositories**
2. Добавьте репозиторий `https://github.com/eryepa/difluid_for_home_assistant`, категория **Integration**
3. Найдите **DiFluid Microbalance & R2** в списке и нажмите **Download**
4. Перезапустите Home Assistant

### Вручную

1. Скопируйте папку `custom_components/difluid_microbalance` в каталог `config/custom_components/` вашего Home Assistant
2. Перезапустите Home Assistant

---

## Настройка

1. **Настройки → Устройства и службы → Добавить интеграцию**
2. Найдите **DiFluid Microbalance & R2**
3. Если устройство найдено в списке — выберите его и подтвердите
4. Если ничего не найдено — выберите **Enter MAC address manually**, введите MAC (`AA:BB:CC:DD:EE:FF`) и тип устройства

> MAC-адрес можно посмотреть в официальном приложении DiFluid или через **nRF Connect** на телефоне.

---

## Объекты (entities)

### Microbalance — сенсоры

Порядок отображения: вес → поток → таймер → статус → батарея.

| Объект | Описание |
|---|---|
| **Weight** | Вес (г / oz / gr) — приходит push-уведомлением с максимальным приоритетом |
| **Flow Rate** | Скорость потока (г/с) — push-уведомление |
| **Timer** | Таймер (секунды) — push-уведомление |
| **Device Status** | Статус устройства (Idle, Timing in Progress, Timer Pause, Tare in Progress, …) |
| **Battery** | Заряд батареи (%). Во время зарядки иконка меняется на батарею с молнией — отдельного сенсора «Charging» нет |

> Второстепенные параметры (статус, батарея, зарядка) опрашиваются **раз в 1 секунду**. Вес и поток от этого не зависят — они приходят отдельными push-уведомлениями.

### Microbalance — управление

| Объект | Описание |
|---|---|
| Кнопка **Tare** | Обнуление веса (одиночное нажатие кнопки питания) |
| Кнопка **Start/Stop** | Запуск / возобновление таймера (одиночное нажатие DLink) |
| Селектор **Mode** | `Manual` / `Espresso` / `Pour Over` — управляет Auto Detect Timing и Auto Stop Timing |
| Число **Auto-disconnect Bluetooth** | Разорвать BLE через N минут без изменения веса (0 = отключено). После разрыва весы выключаются по собственному аппаратному таймеру |

### R2 Extract — сенсоры

| Объект | Описание |
|---|---|
| `concentration` | TDS / концентрация (%) |
| `refractive_index` | Показатель преломления |
| `prism_temperature` | Температура призмы |
| `sample_temperature` | Температура образца |
| `test_status` | Статус теста |

### R2 Extract — управление

| Объект | Описание |
|---|---|
| Кнопка **Start Test** | Запустить одиночное измерение |

> R2 не шлёт данные непрерывно — результат приходит только после завершения измерения. Авто-отключения по BLE у R2 нет: он выключается по собственному таймеру независимо от соединения.

---

## Карточка для дашборда

В интеграцию встроена **собственная карточка** — её не нужно настраивать вручную:

1. Обновите интеграцию и перезапустите Home Assistant
2. На дашборде: **Изменить → Добавить карточку**
3. В поиске введите **DiFluid** → выберите **DiFluid Microbalance / R2**
4. В редакторе карточки выберите ваше устройство — всё остальное соберётся автоматически (сенсоры в нужном порядке + интерактивные кнопки, режим, авто-отключение)

Карточка работает и для весов, и для R2. То же самое в YAML:

```yaml
type: custom:difluid-card
device: <device_id>   # выбирается в редакторе карточки
title: Мои весы       # необязательно
```

> Карточка грузится автоматически (интеграция регистрирует её как ресурс) — добавлять её в **Настройки → Панели** вручную не нужно. Если сразу не появилась в списке — обновите страницу с очисткой кэша (Ctrl+F5).

---

## Режимы (Microbalance)

Селектор **Mode** управляет двумя настройками устройства:

| Режим | Auto Detect Timing | Auto Stop Timing |
|---|---|---|
| Manual | Выкл | Выкл |
| Espresso | Вкл | Выкл |
| Pour Over | Вкл | Вкл |

---

## Auto-disconnect Bluetooth (Microbalance)

Установите **Auto-disconnect Bluetooth** в количество минут (1–60). Когда вес не меняется это время:

1. BLE-соединение разрывается
2. Переподключение подавляется на 60 секунд
3. Весы выключаются по собственному аппаратному таймеру
4. При повторном включении весов интеграция подключается автоматически

Значение **0** отключает функцию. Значение сохраняется между перезапусками HA.

---

## Зашифрованная прошивка

Новые Microbalance (и все R2) **шифруют** BLE-трафик (заголовок `DA DA …` вместо `DF DF`). Интеграция обнаруживает это автоматически и выполняет трёхшаговый облачный handshake DiFluid.

**Ключ не нужен**: DiFluid убрал ограничение на лицензионный ключ.

Для **старых** (нешифрованных) весов handshake не выполняется — всё работает напрямую без обращений к серверу.

---

## Устранение неполадок

| Проблема | Решение |
|---|---|
| Устройство не найдено при добавлении | Введите MAC вручную (см. [Настройка](#настройка)) |
| Сенсоры показывают `0`, в логах пакеты `DA DA` | Зашифрованная прошивка — handshake выполняется автоматически; проверьте доступ в интернет |
| `BLE device … not found` | Устройство вне зоны BLE или выключено; нет Bluetooth-адаптера или ESPHome-прокси |
| Частые переподключения через ESPHome-прокси | Оставьте уровень логов прокси на `INFO`, не `VERY_VERBOSE` |

### Включение отладочных логов

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.difluid_microbalance: debug
```

---

## Ссылки

- Протоколы и SDK-демо: [DiFluid/difluid-sdk-demo](https://github.com/DiFluid/difluid-sdk-demo)
- BLE: [`bleak`](https://github.com/hbldh/bleak) и [`bleak-retry-connector`](https://github.com/Bluetooth-Devices/bleak-retry-connector)

<br>

---
---

# DiFluid for Home Assistant (English)

🇬🇧 English · 🇷🇺 [Русская версия выше](#difluid-для-home-assistant)

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
- Brand icon bundled with the integration (`brand/` folder, shown in HA 2026.3+)

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

## Dashboard card

The integration ships its **own Lovelace card** — no manual setup required:

1. Update the integration and restart Home Assistant
2. On a dashboard: **Edit → Add card**
3. Search for **DiFluid** → pick **DiFluid Microbalance / R2**
4. Choose your device in the card editor — everything else is built automatically (sensors in the right order + interactive buttons, mode, auto-disconnect)

The card works for both the scale and the R2. The YAML equivalent:

```yaml
type: custom:difluid-card
device: <device_id>   # chosen in the card editor
title: My Scale       # optional
```

> The card is loaded automatically (the integration registers it as a resource) — you do **not** need to add it under **Settings → Dashboards → Resources**. If it doesn't appear right away, hard-refresh the page (Ctrl+F5).

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
