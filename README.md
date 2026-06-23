# difluid for home assistant

# Microbalance
Сенсоры которые появятся в HA 

| Сенсор | Описание |
|--------|----------|
| `sensor.weight` | Вес в граммах (или oz/gr) |
| `sensor.flow_rate` | Скорость потока (г/с) |
| `sensor.timer` | Таймер (секунды) |
| `sensor.battery` | Заряд батареи (%) |
| `sensor.charging` | Статус зарядки (Charging/Idle) |
| `sensor.device_status` | Статус устройства (Idle, Timing in Progress и т.д.) |

## Как это работает

1. Компонент регистрирует свои BLE UUID в `manifest.json` — HA автоматически обнаружит весы при сканировании
2. При подключении включается **авто-отправка данных** (`Func=1, Cmd=0, Data=01`) — весы сами шлют уведомления
3. Статус устройства (батарея, зарядка) запрашивается отдельно каждые 60 секунд
4. При потере соединения — автоматический реконнект с паузами 5→15→30→60→120 сек

# R2 Extract  
Cенсоры

| Сенсор | Описание |
|--------|----------|
| `sensor.concentration_tds` | TDS / концентрация (%) |
| `sensor.refractive_index` | Показатель преломления |
| `sensor.prism_temperature` | Температура призмы |
| `sensor.sample_temperature` | Температура образца |
| `sensor.test_status` | Статус теста |

### Важное про R2

R2 требует **license key** от Difluid — это особенность их SDK (серверная аутентификация через `cloud-gateway-os.digitizefluid.com` https://github.com/DiFluid/difluid-sdk-demo ). При добавлении R2 через HA будет форма с полем "License Key". Без ключа устройство работать не будет.


R2 не отправляет данные непрерывно — он отправляет результат только после завершения теста (кнопка TEST на устройстве).
