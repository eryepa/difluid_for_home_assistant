# DiFluid для Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/eryepa/difluid_for_home_assistant)](https://github.com/eryepa/difluid_for_home_assistant/releases)
[![License](https://img.shields.io/github/license/eryepa/difluid_for_home_assistant)](LICENSE)

[🇬🇧 English version](README_EN.md)

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
