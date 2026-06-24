DOMAIN = "difluid_microbalance"

# Microbalance
SERVICE_UUID_MICROBALANCE = "000000ee-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID_MICROBALANCE = "0000ff01-0000-1000-8000-00805f9b34fb"

# Microbalance Ti
SERVICE_UUID_MICROBALANCE_TI = "000000dd-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID_MICROBALANCE_TI = "0000aa01-0000-1000-8000-00805f9b34fb"

# R2 Extract
SERVICE_UUID_R2 = "000000ff-0000-1000-8000-00805f9b34fb"
R2_API_URL = "https://cloud-gateway-os.digitizefluid.com/system"

CONF_IS_TI = "is_ti"
CONF_DEVICE_TYPE = "device_type"
CONF_LICENSE_KEY = "license_key"
CONF_MODEL = "model"

DEVICE_TYPE_MICROBALANCE = "microbalance"
DEVICE_TYPE_R2 = "r2"

# Device model identifiers sent to the DiFluid cloud during the encrypted
# handshake. Newer firmware encrypts its BLE traffic (frames start with 0xDADA)
# and only streams cleartext sensor data after a license-authenticated handshake.
DEFAULT_MODEL_MICROBALANCE = "DFT-S101"
DEFAULT_MODEL_MICROBALANCE_TI = "DFT-S102"
DEFAULT_MODEL_R2 = "DFT-R102"

R2_STATUS_MAP = {
    0: "Test Finished",
    1: "Calibration Finished",
    4: "Average Test Start",
    5: "Average Test Ongoing",
    6: "Average Test Finished",
    7: "Loop Test Start",
    8: "Loop Test Ongoing",
    9: "Loop Test Finished",
    10: "Average Test Ongoing",
    11: "Test Start",
    12: "Calibration Start",
}

DEVICE_STATUS_MAP = {
    0: "Power Down",
    1: "Charging",
    2: "Low Power Mode 1",
    3: "Low-Battery Shutdown",
    4: "Startup",
    5: "Idle",
    6: "Show Device Information",
    7: "Tare in Progress",
    8: "OTA in Progress",
    9: "OTA Failed",
    10: "Timing in Progress",
    11: "Timer Pause",
    12: "Reserved",
    13: "Low Power Mode 2",
    14: "Auto Stop Timing Trigger",
}

WEIGHT_UNITS = {0: "g", 1: "oz", 2: "gr"}
