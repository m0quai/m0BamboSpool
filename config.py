import builtins
from functools import partial
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from config.env when present so live runs have access
# to printer and Spoolman credentials without manual exports.
load_dotenv(Path(__file__).resolve().parent / "config.env")

# Accept existing installations while using the new project prefix internally.
for _legacy_name, _legacy_value in tuple(os.environ.items()):
    if _legacy_name.startswith("OPENSPOOLMAN_"):
        os.environ.setdefault(
            "M0BAMBOSPOOL_" + _legacy_name[len("OPENSPOOLMAN_"):], _legacy_value
        )

# Central m0BamboSpool database configuration.
DATABASE_TYPE = (os.getenv("M0BAMBOSPOOL_DATABASE_TYPE") or "sqlite").strip().lower()
DATABASE_NAME = (os.getenv("M0BAMBOSPOOL_DATABASE_NAME") or "osm.db").strip()
DATABASE_PATH_OVERRIDE = (
    os.getenv("M0BAMBOSPOOL_DATABASE_PATH")
    or os.getenv("M0BAMBOSPOOL_PRINT_HISTORY_DB")
    or ""
).strip()
if not DATABASE_NAME or Path(DATABASE_NAME).name != DATABASE_NAME:
    raise ValueError("M0BAMBOSPOOL_DATABASE_NAME must be a filename without a directory")
DATABASE_PATH = (
    Path(DATABASE_PATH_OVERRIDE).expanduser().resolve()
    if DATABASE_PATH_OVERRIDE
    else Path(__file__).resolve().parent / "data" / DATABASE_NAME
)

EXTERNAL_SPOOL_AMS_ID = 255 # don't change
EXTERNAL_SPOOL_ID = 254 #  don't change

builtins.print = partial(builtins.print, flush=True)


def _env_to_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_to_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


BASE_URL = os.getenv("M0BAMBOSPOOL_BASE_URL")  # Where will this app be accessible
PRINTER_ID = (os.getenv("PRINTER_ID") or "").upper()  # Printer serial number - Run init_bambulab.py
PRINTER_ACCESS_ONLINE = os.getenv("PRINTER_ACCESS_ONLINE") or ""  # Online/cloud printer access code
PRINTER_ACCESS_LAN = os.getenv("PRINTER_ACCESS_LAN") or ""  # Local LAN/Developer Mode access code
PRINTER_CONNECTION_MODE = (os.getenv("PRINTER_CONNECTION_MODE") or "LAN").strip().lower()
if PRINTER_CONNECTION_MODE not in ("lan", "online"):
    PRINTER_CONNECTION_MODE = "lan"

# Backwards-compatible active access code used by existing printer/FTP code.
# The selected mode decides which credential is active.
PRINTER_CODE = PRINTER_ACCESS_LAN if PRINTER_CONNECTION_MODE == "lan" else PRINTER_ACCESS_ONLINE
PRINTER_IP = os.getenv("PRINTER_IP")  # Required printer IP address for the connection
PRINTER_NAME = os.getenv("PRINTER_NAME")  # Printer name - Check wireless on printer
SPOOLMAN_BASE_URL = os.getenv("SPOOLMAN_BASE_URL")
SPOOLMAN_API_URL = f"{SPOOLMAN_BASE_URL}/api/v1"
USE_SPOOLMAN = (os.getenv("USE_SPOOLMAN", "yes") or "yes").strip().lower() in {"yes", "true", "1", "on"}
AUTO_SPEND = True
TRACK_LAYER_USAGE = True
LOG_AMS_MODE = (os.getenv("LOG_AMS_MODE") or "changes").strip().lower()
if LOG_AMS_MODE not in ("none", "changes", "everything"):
    LOG_AMS_MODE = "changes"
SPOOL_SORTING = os.getenv(
    "SPOOL_SORTING", "filament.material:asc,filament.vendor.name:asc,filament.name:asc"
)
DISABLE_MISMATCH_WARNING = _env_to_bool("DISABLE_MISMATCH_WARNING", False)
CLEAR_ASSIGNMENT_WHEN_EMPTY = False
COLOR_DISTANCE_TOLERANCE = _env_to_int("COLOR_DISTANCE_TOLERANCE", 40)

# Bambu MQTT command signing (optional)
BAMBU_LAB_USER_ID = os.getenv("BAMBU_LAB_USER_ID")
BAMBU_LAB_APP_CERT_ID = os.getenv("BAMBU_LAB_APP_CERT_ID")
BAMBU_APP_PRIVATE_KEY_PATH = os.getenv("BAMBU_APP_PRIVATE_KEY_PATH")
BAMBU_APP_CERTIFICATE_PATH = os.getenv("BAMBU_APP_CERTIFICATE_PATH")
