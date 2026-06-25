# DiFluid for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/eryepa/difluid_for_home_assistant)](https://github.com/eryepa/difluid_for_home_assistant/releases)
[![License](https://img.shields.io/github/license/eryepa/difluid_for_home_assistant)](LICENSE)

Кастомная интеграция Home Assistant для устройств **[DiFluid](https://digitizefluid.com/)**, подключаемых по Bluetooth Low Energy:

- **DiFluid Microbalance** и **Microbalance Ti** — кофейные весы (вес, скорость потока, таймер, батарея)
- **DiFluid R2 Extract** — рефрактометр (TDS/концентрация, показатель преломления, температуры)

Связь полностью локальная, через BLE. Если у вас есть **ESPHome Bluetooth Proxy**, весы можно держать далеко от сервера HA.

> [!NOTE]
> Новые прошивки шифруют BLE-трафик. Интеграция автоматически выполняет облачный handshake DiFluid без ключа — это работает «из коробки». Поле **License Key** оставьте пустым.

---

## Возможности

- Автоматическое обнаружение по BLE + ручной ввод MAC-адреса (если устройство не рекламирует свои UUID)
- Push-обновления: весы сами шлют данные через BLE-уведомления, без постоянного опроса
- Автоматический реконнект при потере связи
- Поддержка зашифрованной прошивки через облачный handshake DiFluid
- Работа через ESPHome Bluetooth Proxy

## Поддерживаемые устройства

| Устройство | Service UUID | Зашифрованная прошивка |
|---|---|---|
| Microbalance | `000000EE` | Автоматический handshake, ключ не нужен |
| Microbalance Ti | `000000DD` | Автоматический handshake, ключ не нужен |
| R2 Extract | `000000FF` | Автоматический handshake, ключ не нужен |

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

### Добавление устройства

1. **Настройки → Устройства и службы → Добавить интеграцию**
2. Найдите **DiFluid Microbalance & R2**
3. Если устройство найдено автоматически — подтвердите добавление

### Если устройство не находится

Некоторые весы DiFluid не публикуют свои Service UUID в рекламных пакетах, поэтому автообнаружение их не видит. В этом случае:

1. Выберите ввод **MAC-адреса вручную** (формат `AA:BB:CC:DD:EE:FF`)
2. Укажите тип устройства (Microbalance / R2)

> MAC-адрес можно посмотреть в официальном приложении DiFluid или через **nRF Connect** на телефоне.

---

## Сенсоры

### Microbalance

| Сенсор | Описание |
|---|---|
| `weight` | Вес (граммы / oz / gr) |
| `flow_rate` | Скорость потока (г/с) |
| `timer` | Таймер (секунды) |
| `battery` | Заряд батареи (%) |
| `charging` | Статус зарядки (Charging / Idle) |
| `device_status` | Статус устройства (Idle, Timing in Progress и т.д.) |

### R2 Extract

| Сенсор | Описание |
|---|---|
| `concentration` | TDS / концентрация (%) |
| `refractive_index` | Показатель преломления |
| `prism_temperature` | Температура призмы |
| `sample_temperature` | Температура образца |
| `test_status` | Статус теста |

> R2 не шлёт данные непрерывно — результат приходит только после завершения теста (кнопка **TEST** на устройстве).

---

## Шифрованная прошивка

Новые прошивки Microbalance (и все R2) **шифруют** BLE-трафик (`DA DA …` заголовок вместо `DF DF`). Интеграция обнаруживает это автоматически по наличию зашифрованного канала (`ff01`) и выполняет трёхшаговый облачный handshake DiFluid (`cmd1 → cmd2 → cmd3 → enableCleartext`).

**Ключ не нужен**: DiFluid убрал ограничение на лицензионный ключ — сервер принимает запросы без него. Поле **License Key** при добавлении устройства оставьте пустым.

Если handshake не удаётся (ошибка сети или проблема сервера), интеграция автоматически переключается в прямой cleartext-режим.

> Для **старых** (нешифрованных) весов handshake не запускается — всё работает напрямую без обращений к серверу.

---

## Как это работает

1. Интеграция регистрирует BLE Service UUID в `manifest.json` — HA пытается обнаружить устройство автоматически
2. При подключении подписывается на BLE-уведомления и включает **авто-отправку данных** (`Func=1, Cmd=0, Data=01`)
3. Данные веса/потока приходят push-уведомлениями; статус (батарея, зарядка) опрашивается одним запросом раз в 30 секунд
4. Для зашифрованной прошивки сначала выполняется облачный handshake, затем данные читаются из cleartext-канала
5. При потере связи — автоматический реконнект с паузами 5 → 15 → 30 → 60 → 120 сек

---

## Устранение неполадок

| Проблема | Решение |
|---|---|
| Устройство не находится при добавлении | Введите MAC-адрес вручную (см. [выше](#если-устройство-не-находится)) |
| Сенсоры показывают `0`, в логах пакеты `DA DA` | Прошивка зашифрована — HA выполняет handshake автоматически; проверьте доступ в интернет |
| `BLE device … not found` | Устройство вне зоны действия BLE / выключено, либо нет Bluetooth-адаптера или ESPHome-прокси |
| Частые переподключения через ESPHome-прокси | Не держите уровень логов прокси на `VERY_VERBOSE` — это вызывает нестабильность; верните `INFO` |
| Handshake падает с ошибкой сервера | Неверный лицензионный ключ или модель — проверьте ключ и укажите правильную **Model** |

### Включение отладочных логов

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.difluid_microbalance: debug
```

---

## Благодарности и ссылки

- Протоколы и SDK-демо: [DiFluid/difluid-sdk-demo](https://github.com/DiFluid/difluid-sdk-demo)
- BLE-подключение через [`bleak`](https://github.com/hbldh/bleak) и [`bleak-retry-connector`](https://github.com/Bluetooth-Devices/bleak-retry-connector)
