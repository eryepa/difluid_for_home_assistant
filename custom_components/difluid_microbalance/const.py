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
# Public demo key from the DiFluid SDK demo repository (r2Detail.vue).
# Used as the default when no key is provided.
R2_DEFAULT_LICENSE_KEY = "b0b978c8d7fe4ac782767996f34a6ce1"

CONF_IS_TI = "is_ti"
CONF_DEVICE_TYPE = "device_type"
CONF_LICENSE_KEY = "license_key"
CONF_MODEL = "model"

# ── brew detector options ─────────────────────────────────────────────────────
# The tunable threshold names live in brew_detect.TUNABLE_FIELDS, next to the
# dataclass they map onto, so the two cannot drift apart.
CONF_RECORD_DATASET = "record_dataset"

DEVICE_TYPE_MICROBALANCE = "microbalance"
DEVICE_TYPE_R2 = "r2"
#: The detector: not a device at all, but an entry that reads one scale and,
#: optionally, one refractometer.  It owns the BrewSession and every statistic
#: derived from it.
DEVICE_TYPE_DETECTOR = "detector"

# ── detector entry data ───────────────────────────────────────────────────────
#: entry_id of the scale whose weight stream this detector reads.  Required.
CONF_SCALE_ENTRY = "scale_entry"
#: entry_id of the R2 whose TDS this detector reads.  Optional — the pairing of a
#: shot with a measurement is iteration 2; this is the wire it will travel on.
CONF_R2_ENTRY = "r2_entry"

#: What every entity of this detector prefixes its unique_id with, stored rather
#: than derived.
#:
#: The entity registry is keyed by (domain, platform, unique_id), and `platform`
#: is the integration domain for every entry we create.  So a detector that
#: registers `f"{prefix}_{key}"` with the prefix the *scale* entry used claims the
#: existing registry rows and HA simply rewrites their config_entry_id and
#: device_id in place — entity_id, recorder history, long-term statistics and the
#: Prometheus series all survive the move untouched.  That is the whole migration.
#:
#: It has to be stored, not computed, for the case that gives the trick away: point
#: an existing detector at a different scale and a derived prefix would change,
#: orphaning seven entities and starting the odometer again at zero.  Written once
#: when the entry is created, read forever after.
CONF_UID_PREFIX = "uid_prefix"

#: Set on a *scale* entry once its detector has been split out into an entry of its
#: own, so the migration runs exactly once per scale.
#:
#: 1.5.0-beta.1 tried to recognise a not-yet-migrated install by the detector
#: thresholds still sitting in the scale's options, which was a proxy and not the
#: fact: options are only written when somebody opens the options form and changes a
#: threshold, so an install running entirely on defaults had none, the migration never
#: fired, and — because that version had already stopped the scale from creating the
#: statistics entities — all seven went unavailable with no detector to claim them.
#:
#: The real condition is "a scale with no detector pointing at it".  This flag is what
#: keeps that from meaning "recreate the detector every restart" after somebody
#: deliberately deletes one.
CONF_DETECTOR_IMPORTED = "detector_imported"

#: Storage key for this detector's BrewSession, stored for the same reason and
#: seeded by the same rule: an imported detector keeps `difluid_microbalance.brew`,
#: which is where brew_count and the last pair already live, and a detector created
#: from scratch gets its own key.  See brew_session._DEFAULT_STORE_KEY.
CONF_STORE_KEY = "store_key"

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
