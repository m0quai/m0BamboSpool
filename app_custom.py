# Custom OpenSpoolMan entry point; keeps local extensions outside upstream app.py.
import os
import logging
import builtins
import socket
import ssl
import struct
import threading
from pathlib import Path
from datetime import datetime

# Waitress' startup banner is informational and duplicates the application
# startup log; retain warnings/errors while keeping normal container logs tidy.
logging.getLogger("waitress").setLevel(logging.WARNING)
logging.getLogger("waitress.queue").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# OpenSpoolMan / Spoolman URL split
#
# Public URLs are for links opened by the browser/host.
# SPOOLMAN_INTERNAL_BASE_URL is only for OpenSpoolMan -> Spoolman API traffic.
#
# Missing values are written automatically to config.env. Existing values are
# never overwritten.
# ---------------------------------------------------------------------------
_CONFIG_ENV = Path(__file__).resolve().parent / "config.env"
_URL_DEFAULTS = {
    "HOST_IP": "localhost",
    "OPENSPOOLMAN_BASE_PORT": "8000",
    "SPOOLMAN_BASE_PORT": "7912",
    "SPOOLMAN_INTERNAL_BASE_URL": "http://spoolman:8000",
}

def _read_config_env(path):
    values = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return values

def _ensure_url_config():
    if not _CONFIG_ENV.exists():
        _CONFIG_ENV.write_text("", encoding="utf-8")

    values = _read_config_env(_CONFIG_ENV)
    missing = [(k, v) for k, v in _URL_DEFAULTS.items() if not values.get(k)]

    if missing:
        current = _CONFIG_ENV.read_text(encoding="utf-8")
        with _CONFIG_ENV.open("a", encoding="utf-8") as f:
            if current and not current.endswith("\r\n"):
                f.write("\r\n")
            f.write("\r\n# OpenSpoolMan public/internal URLs (automatically added)\r\n")
            for key, value in missing:
                f.write(f"{key}={value}\r\n")
                values[key] = value

    return values

_url_config = _ensure_url_config()

_host = _url_config.get("HOST_IP", _URL_DEFAULTS["HOST_IP"])
_openspoolman_port = _url_config.get("OPENSPOOLMAN_BASE_PORT", _URL_DEFAULTS["OPENSPOOLMAN_BASE_PORT"])
_spoolman_port = _url_config.get("SPOOLMAN_BASE_PORT", _URL_DEFAULTS["SPOOLMAN_BASE_PORT"])
_SPOOLMAN_PUBLIC_BASE_URL = f"http://{_host}:{_spoolman_port}".rstrip("/")
_SPOOLMAN_DOCKER_BASE_URL = _url_config.get(
    "SPOOLMAN_INTERNAL_BASE_URL", _SPOOLMAN_PUBLIC_BASE_URL
).rstrip("/")
_OPENSPOOLMAN_PUBLIC_BASE_URL = f"http://{_host}:{_openspoolman_port}".rstrip("/")

def _running_inside_docker():
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore").lower()
        return "docker" in cgroup or "containerd" in cgroup or "kubepods" in cgroup
    except Exception:
        return False

def _docker_spoolman_url():
    import socket
    from urllib.parse import urlparse

    configured = _SPOOLMAN_DOCKER_BASE_URL
    try:
        host = urlparse(configured).hostname
        if host:
            socket.getaddrinfo(host, None)
            return configured
    except OSError:
        pass

    host_fallback = "http://host.docker.internal:7912"
    try:
        socket.getaddrinfo("host.docker.internal", None)
        return host_fallback
    except OSError:
        pass

    return _SPOOLMAN_PUBLIC_BASE_URL

_SPOOLMAN_RUNTIME_BASE_URL = (
    _docker_spoolman_url() if _running_inside_docker()
    else _SPOOLMAN_PUBLIC_BASE_URL
)

os.environ["OPENSPOOLMAN_BASE_URL"] = _OPENSPOOLMAN_PUBLIC_BASE_URL
os.environ["SPOOLMAN_BASE_URL"] = _SPOOLMAN_RUNTIME_BASE_URL

from app import app

os.environ["SPOOLMAN_BASE_URL"] = _SPOOLMAN_PUBLIC_BASE_URL

import config as _openspoolman_config
import app as _openspoolman_app_module

_openspoolman_config.SPOOLMAN_BASE_URL = _SPOOLMAN_PUBLIC_BASE_URL
_openspoolman_config.SPOOLMAN_INTERNAL_BASE_URL = _SPOOLMAN_DOCKER_BASE_URL
_openspoolman_config.SPOOLMAN_RUNTIME_BASE_URL = _SPOOLMAN_RUNTIME_BASE_URL
_openspoolman_config.SPOOLMAN_API_URL = f"{_SPOOLMAN_RUNTIME_BASE_URL}/api/v1"
_openspoolman_app_module.SPOOLMAN_BASE_URL = _SPOOLMAN_PUBLIC_BASE_URL

# Upstream only recognizes the base material PLA.  Keep the concrete PLA+
# material in Spoolman while using a safe PLA range when no per-filament
# nozzle-temperature extra field has been maintained.
_original_generate_filament_temperatures = _openspoolman_app_module.generate_filament_temperatures

def _generate_filament_temperatures_with_pla_plus(filament_type, filament_brand):
    normalized = str(filament_type or "").strip().upper().replace(" ", "")
    if normalized.startswith("PLA+"):
        return {"filament_min_temp": 190, "filament_max_temp": 240}
    return _original_generate_filament_temperatures(filament_type, filament_brand)

_openspoolman_app_module.generate_filament_temperatures = _generate_filament_temperatures_with_pla_plus


def _readonly_augment_tray(spool_list, tray_data, ams_id, tray_id):
    _openspoolman_app_module.augmentTrayDataWithSpoolMan(
        spool_list, tray_data, ams_id, tray_id
    )
    # The upstream warning is based on transient/incomplete AMS material
    # packets and is not actionable in the tray card.  The stable tray state
    # and spool assignment are the source of truth shown below.
    tray_data["ams_material_missing"] = False
    tray_data["ams_material_missing_message"] = ""
    if int(ams_id) == EXTERNAL_SPOOL_AMS_ID:
        tray_data["ams_material_missing"] = False
        tray_data["ams_material_missing_message"] = ""
        tray_data["issue"] = False
        tray_data["mismatch"] = False
        tray_data["mismatch_detected"] = False
    # Keep External Spool actionable; only mark genuinely empty AMS trays.
    tray_data["ams_empty"] = False
    tray_data["ams_loading"] = mqtt_bambulab.is_ams_tray_operation_pending(ams_id, tray_id)
    if tray_data["ams_loading"]:
        tray_data["ams_material_missing"] = False
        tray_data["ams_material_missing_message"] = ""
    if ams_id is None:
        return
    for ams in (mqtt_bambulab.LAST_AMS_CONFIG.get("ams", []) or []):
        if str(ams.get("id")) != str(ams_id):
            continue
        for tray in ams.get("tray", []) or []:
            if str(tray.get("id")) != str(tray_id):
                continue
            tray_uuid = str(tray.get("tray_uuid") or "").strip()
            uuid_empty = not tray_uuid
            tray_color = str(tray.get("tray_color") or "").strip().upper()
            tray_data["ams_empty"] = (
                uuid_empty
                and not str(tray.get("tray_type") or "").strip()
                and not str(tray.get("tray_sub_brands") or "").strip()
                and not str(tray.get("tray_info_idx") or "").strip()
                and not str(tray.get("setting_id") or "").strip()
                and tray_color in {"", "FFFFFFFF"}
            )
            tray_status = str(tray.get("tray_status") or tray.get("status") or "").lower()
            tray_data["ams_loading"] = (
                mqtt_bambulab.is_ams_tray_operation_pending(ams_id, tray_id)
                or tray_status in {"loading", "unloading", "changing", "load", "unload"}
            )
            if tray_data["ams_loading"]:
                tray_data["ams_material_missing"] = False
                tray_data["ams_material_missing_message"] = ""
            return

_openspoolman_app_module._augment_tray = _readonly_augment_tray


if not app.secret_key:
    import os
    import secrets
    from pathlib import Path

    _secret_file = Path(__file__).resolve().parent / "data" / ".flask_secret_key"
    _secret_file.parent.mkdir(parents=True, exist_ok=True)

    _configured_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if _configured_secret:
        app.secret_key = _configured_secret
    else:
        try:
            app.secret_key = _secret_file.read_text(encoding="utf-8").strip()
        except Exception:
            app.secret_key = ""

        if not app.secret_key:
            app.secret_key = secrets.token_urlsafe(48)
            _secret_file.write_text(app.secret_key, encoding="utf-8")

from bambu_auth_routes import bp as bambu_cloud_bp
from nfc_routes import bp as ams_nfc_bp
import nfc_pending_repository
from flask import jsonify, redirect, request, url_for, render_template, send_from_directory, Response, stream_with_context, session
import mqtt_bambulab
import inventory_repository as spool_data
import inventory_database as inventory_db
from filament_color_catalog import SUNLU_COLOR_OPTIONS
from config import USE_SPOOLMAN
import print_history as print_history_service
from config import EXTERNAL_SPOOL_AMS_ID, PRINTER_ID, PRINTER_NAME
from __version__ import __build_number__, __version__
import tools_3mf as _tools_3mf
import filament_usage_tracker as _filament_usage_tracker
from jobs_3mf import JOBS_3MF
from ui_formatting import format_ui_datetime

from logger import log as _log

spool_data.install_spoolman_compatibility_adapter()
try:
    # Keep the local administration schema available even while Spoolman remains active.
    spool_data._spoolman_api.SPOOLMAN_API_URL = _openspoolman_config.SPOOLMAN_API_URL
    spool_data.ensure_local_catalog()
except Exception as exc:
    _log(f"Local inventory schema initialization failed: {exc!r}")
import inventory_service as _inventory_service

_get_inventory_settings = _inventory_service.getSettings

def _get_inventory_settings_for_legacy_template(cached=False):
    settings = _get_inventory_settings(cached)
    settings["currency_symbol"] = ""
    return settings

_inventory_service.getSettings = _get_inventory_settings_for_legacy_template

_AMS_REFRESH_LOCK = threading.Lock()
_AMS_REFRESH_LAST_REQUEST = 0.0
_AMS_REFRESH_MIN_INTERVAL_SECONDS = 5.0

# Bambu's mc_percent is the printer's authoritative completion value.  The
# upstream history view derives progress from layer counts, which can differ
# noticeably for jobs with variable layer durations.  Enrich the rendered
# history data in the custom entry point without modifying app.py.
_openspoolman_app_module.LAYER_TRACKING_STATUS_DISPLAY.update({
    "PREPARING": ("Preparing", "info"),
    "PAUSED": ("Paused", "secondary"),
})
_original_app_render_template = _openspoolman_app_module.render_template

def _render_template_with_printer_progress(template_name, *args, **kwargs):
    if template_name == "print_history.html":
        requested_print_id = request.args.get("print_id")
        try:
            requested_print_id = int(requested_print_id) if requested_print_id is not None else None
        except (TypeError, ValueError):
            requested_print_id = None
        if requested_print_id is not None:
            # A history deep link should open the selected print, not merely
            # scroll to its table row.  Keep this override in the custom entry
            # point so the upstream app.py remains untouched.
            kwargs["expanded_print_id"] = requested_print_id
        print_state = getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}
        raw_percent = print_state.get("mc_percent")
        active_print_id = print_history_service.get_latest_active_print_id()
        if active_print_id is not None:
            kwargs["active_print_id"] = active_print_id
        if requested_print_id is None and active_print_id is not None:
            kwargs["expanded_print_id"] = active_print_id
        try:
            percent = max(0, min(100, int(float(raw_percent)))) if raw_percent is not None else None
        except (TypeError, ValueError):
            percent = None
        if percent is not None and active_print_id is not None:
            for print_entry in kwargs.get("prints", []) or []:
                if print_entry.get("id") == active_print_id and print_entry.get("layer_tracking"):
                    print_entry["layer_tracking"]["progress_percent"] = percent
                    break
        print_ids = [entry.get("id") for entry in kwargs.get("prints", []) or [] if entry.get("id") is not None]
        tracking_rows = print_history_service.get_layer_tracking_for_prints(print_ids)
        for print_entry in kwargs.get("prints", []) or []:
            tracking = print_entry.get("layer_tracking")
            row = tracking_rows.get(print_entry.get("id"))
            if tracking is not None and row is not None:
                tracking["estimated_duration_minutes"] = row.get("estimated_duration_minutes")
                tracking["printer_percent"] = row.get("printer_percent")
                tracking["last_status_at"] = row.get("last_status_at")
                tracking["last_usage_event_at"] = row.get("last_usage_event_at")
    return _original_app_render_template(template_name, *args, **kwargs)

_openspoolman_app_module.render_template = _render_template_with_printer_progress

_UI_TRANSLATIONS = {
        "de": {"Home":"Home", "History":"History", "LiveCam":"LiveCam", "Inventory":"Inventar", "Settings":"Settings", "SpoolMan":"SpoolMan", "Spool":"Spule", "Hersteller":"Hersteller", "Material":"Material", "Filament":"Filament", "Farbe":"Farbe", "Restgewicht":"Restgewicht", "Drucke":"Drucke", "Alle":"Alle", "Status":"Status", "ausgewählt":"ausgewählt", "Datum":"Datum", "Auftrag":"Auftrag", "Verbrauch":"Verbrauch", "Ohne Namen":"Ohne Namen", "ungebraucht":"ungebraucht", "Keine Spulen gefunden":"Keine Spulen gefunden", "Sprache":"Sprache", "Deutsch":"Deutsch", "Englisch":"Englisch", "Speichern":"Speichern", "Print history":"Druckhistorie", "Jetzt aktualisieren":"Jetzt aktualisieren", "Druckname":"Druckname", "Aktive anzeigen":"Aktive anzeigen", "Gelöschte anzeigen":"Gelöschte anzeigen", "Previous":"Zurück", "Next":"Weiter", "Page":"Seite", "of":"von", "Abmelden":"Abmelden", "Drucker übernehmen":"Drucker übernehmen", "Code bestätigen":"Code bestätigen", "Bei Bambu anmelden":"Bei Bambu anmelden", "Drucker Verbindung":"Drucker Verbindung", "Verbindungsmodus":"Verbindungsmodus", "Lokaler LAN-Modus":"Lokaler LAN-Modus", "Online-Authentifizierung":"Online-Authentifizierung", "Direkte Verbindung zum Drucker im lokalen Netzwerk.":"Direkte Verbindung zum Drucker im lokalen Netzwerk.", "Drucker-IP":"Drucker-IP", "Seriennummer":"Seriennummer", "Druckername":"Druckername", "Printer Access LAN":"Printer Access LAN", "Drucker auswählen":"Drucker auswählen", "Keine gebundenen Drucker zurückgegeben.":"Keine gebundenen Drucker zurückgegeben."},
    "en": {"Home":"Home", "History":"History", "LiveCam":"LiveCam", "Inventory":"Inventory", "Settings":"Settings", "SpoolMan":"SpoolMan", "Spool":"Spool", "Hersteller":"Manufacturer", "Material":"Material", "Filament":"Filament", "Farbe":"Color", "Restgewicht":"Remaining weight", "Drucke":"Prints", "Alle":"All", "Status":"Status", "ausgewählt":"selected", "Datum":"Date", "Auftrag":"Job", "Verbrauch":"Usage", "Ohne Namen":"Unnamed", "ungebraucht":"Unused", "Keine Spulen gefunden":"No spools found", "Sprache":"Language", "Deutsch":"German", "Englisch":"English", "Speichern":"Save", "Print history":"Print history", "Jetzt aktualisieren":"Refresh now", "Druckname":"Print name", "Aktive anzeigen":"Show active", "Gelöschte anzeigen":"Show deleted", "Previous":"Previous", "Next":"Next", "Page":"Page", "of":"of", "Abmelden":"Sign out", "Drucker übernehmen":"Use printer", "Code bestätigen":"Confirm code", "Bei Bambu anmelden":"Sign in to Bambu", "Drucker Verbindung":"Printer connection", "Verbindungsmodus":"Connection mode", "Lokaler LAN-Modus":"Local LAN mode", "Online-Authentifizierung":"Online authentication", "Direkte Verbindung zum Drucker im lokalen Netzwerk.":"Direct connection to the printer on the local network.", "Drucker-IP":"Printer IP", "Seriennummer":"Serial number", "Druckername":"Printer name", "Printer Access LAN":"Printer LAN access", "Drucker auswählen":"Select printer", "Keine gebundenen Drucker zurückgegeben.":"No bound printers returned."},
}

# Additional labels used by the existing pages. Keeping these keys here lets
# legacy templates migrate incrementally while still using one translation
# source.
_UI_TRANSLATIONS["de"].update({
    "Deutsch oder Englisch für die Benutzeroberfläche": "Deutsch oder Englisch für die Benutzeroberfläche",
    "Ein LAN Access-Code ist gespeichert. Leer lassen, um ihn beizubehalten.": "Ein LAN Access-Code ist gespeichert. Leer lassen, um ihn beizubehalten.",
    "Bambu Cloud": "Bambu Cloud", "Angemeldet": "Angemeldet", "als": "als", "Name": "Name", "Modell": "Modell", "Online": "Online", "Lokale IP": "Lokale IP", "Bambu Cloud anmelden": "Bambu Cloud anmelden", "Bambu E-Mail": "Bambu E-Mail", "Passwort": "Passwort", "Verbunden": "Verbunden", "Nicht verbunden": "Nicht verbunden", "MQTT": "MQTT", "Status:": "Status:",
    "Link Bambu Spool to SpoolMan": "Bambu-Spule mit SpoolMan verknüpfen", "Bambu tag": "Bambu-Tag", "detected in this tray. Pick the matching SpoolMan spool to bind this tag and assign the tray.": "in diesem Fach erkannt. Wähle die passende SpoolMan-Spule zum Verknüpfen und Zuordnen.", "Error": "Fehler", "Back": "Zurück", "Fill Tray": "Fach füllen", "External Spool": "Externe Spule", "Solve issue": "Problem lösen", "Livebild": "Livebild", "NFC-Tags": "NFC-Tags", "Zurück": "Zurück", "Unbekannter NFC-Tag": "Unbekannter NFC-Tag", "Spule auswählen ...": "Spule auswählen ...", "Spule zuordnen": "Spule zuordnen", "Keine unbekannten NFC-Tags vorhanden.": "Keine unbekannten NFC-Tags vorhanden.", "Change spool": "Spule ändern", "Assign spool to print": "Spule dem Druck zuordnen", "Assign Tray": "Fach zuordnen", "NFC Write Process": "NFC-Schreibvorgang", "Follow the steps below to write data to your NFC tag.": "Folge den Schritten zum Schreiben deines NFC-Tags.", "Allow NFC usage if prompted.": "Erlaube die NFC-Nutzung, wenn du dazu aufgefordert wirst.", "Bring the tag close to your phone when prompted.": "Halte den Tag an dein Telefon, wenn du dazu aufgefordert wirst.", "Humidity": "Luftfeuchtigkeit", "Filament type mismatch": "Filamenttyp stimmt nicht überein", "No AMS material selected": "Kein AMS-Material ausgewählt", "Fach leer": "Fach leer", "Unknown spool detected": "Unbekannte Spule erkannt", "Last used:": "Zuletzt verwendet:", "Full": "Voll", "Remaining:": "Verbleibend:", "Link to Spoolman": "Mit SpoolMan verknüpfen", "Assign": "Zuordnen", "Clear": "Leeren", "Fill": "Füllen", "No Image": "Kein Bild", "Print ID": "Druck-ID", "Druck": "Druck", "Layers": "Schichten", "Billed": "Abgerechnet", "Progress": "Fortschritt", "No spool assigned": "Keine Spule zugeordnet", "printed": "gedruckt", "expected": "erwartet", "Spule ändern": "Spule ändern", "Eintrag aus der History ausblenden": "Eintrag aus der History ausblenden", "Eintrag löschen": "Eintrag löschen", "Remaining Weight:": "Restgewicht:", "Remaining Length:": "Restlänge:", "Nozzle Temp:": "Düsentemperatur:", "view in spoolman": "in SpoolMan anzeigen", "Registered:": "Registriert:", "Last Used:": "Zuletzt verwendet:", "Keine Historie vorhanden.": "Keine Historie vorhanden.", "Success!": "Erfolgreich!"
})
_UI_TRANSLATIONS["en"].update({
    "Deutsch oder Englisch für die Benutzeroberfläche": "German or English for the user interface", "Ein LAN Access-Code ist gespeichert. Leer lassen, um ihn beizubehalten.": "A LAN access code is saved. Leave empty to keep it.", "Angemeldet": "Signed in", "als": "as", "Lokale IP": "Local IP", "Verbunden": "Connected", "Nicht verbunden": "Not connected", "Status:": "Status:", "Zurück": "Back", "Spule zuordnen": "Assign spool", "Keine unbekannten NFC-Tags vorhanden.": "No unknown NFC tags found.", "No Image": "No image", "Success!": "Success!", "Keine Historie vorhanden.": "No history available.", "Clear": "Clear", "Fill": "Fill", "Assign": "Assign", "Back": "Back", "Error": "Error", "Livebild": "Live view", "Humidity": "Humidity", "Full": "Full", "Remaining:": "Remaining:", "Last used:": "Last used:", "Unbekannter NFC-Tag": "Unknown NFC tag", "Spule auswählen ...": "Select spool ...", "Spule ändern": "Change spool", "Eintrag löschen": "Delete entry", "Eintrag aus der History ausblenden": "Hide history entry", "Keine Spule zugeordnet": "No spool assigned", "gedruckt": "printed", "erwartet": "expected", "Druck-ID": "Print ID", "Druck": "Print", "Schichten": "Layers", "Abgerechnet": "Billed", "Fortschritt": "Progress", "Restgewicht:": "Remaining weight:", "Restlänge:": "Remaining length:", "Düsentemperatur:": "Nozzle temperature:", "in SpoolMan anzeigen": "view in SpoolMan", "Registriert:": "Registered:", "Zuletzt verwendet:": "Last used:"
})
_UI_TRANSLATIONS["de"]["Last print"] = "Letzter Druck"
_UI_TRANSLATIONS["en"]["Last print"] = "Last print"
_UI_TRANSLATIONS["de"]["(geschätzt)"] = "(geschätzt)"
_UI_TRANSLATIONS["en"]["(geschätzt)"] = "(estimated)"
_UI_TRANSLATIONS["de"].update({"Finished": "Abgeschlossen", "Completed": "Abgeschlossen", "Failed": "Fehlgeschlagen", "Cancelled": "Abgebrochen", "Aborted": "Abgebrochen", "Running": "Druck läuft"})
_UI_TRANSLATIONS["en"].update({"Finished": "Finished", "Completed": "Completed", "Failed": "Failed", "Cancelled": "Cancelled", "Aborted": "Aborted", "Running": "Printing"})
_UI_TRANSLATIONS["de"]["Ready"] = "Bereit"
_UI_TRANSLATIONS["en"]["Ready"] = "Ready"
_UI_TRANSLATIONS["de"].update({"Last status": "Letzter Status", "Last booking": "Letzte Buchung"})
_UI_TRANSLATIONS["en"].update({"Last status": "Last status", "Last booking": "Last booking"})
_UI_TRANSLATIONS["de"].update({"Letzter Druck": "Letzter Druck", "Letzter Status": "Letzter Status", "Letzte Buchung": "Letzte Buchung"})
_UI_TRANSLATIONS["en"].update({"Letzter Druck": "Last print", "Letzter Status": "Last status", "Letzte Buchung": "Last booking"})

# The external module is the authoritative source; the legacy inline map
# above is retained only for compatibility with older local checkouts.
from translations import UI_TRANSLATIONS as _UI_TRANSLATIONS

def _ui_language():
    language = session.get("ui_language", "de")
    return language if language in _ui_languages() else "de"

def _ui_languages():
    return tuple(
        language
        for language, values in _UI_TRANSLATIONS.items()
        if language != "_init" and isinstance(values, dict)
    )

def _ui_language_metadata():
    metadata = _UI_TRANSLATIONS.get("_init", {})
    return {
        language: metadata.get(language, {})
        for language in _ui_languages()
    }

def _ui_text(key):
    return _UI_TRANSLATIONS[_ui_language()].get(key, key)

@app.context_processor
def inject_ui_language():
    return {
        "_": _ui_text,
        "current_language": _ui_language(),
        "available_languages": _ui_languages(),
        "language_metadata": _ui_language_metadata(),
        "format_datetime": lambda value: format_ui_datetime(value, _ui_language()),
    }

@app.post("/language")
def set_language():
    language = request.form.get("language", "de")
    if language in _ui_languages():
        session["ui_language"] = language
    return redirect(request.form.get("next") or url_for("home"))

_build_number_file = Path(__file__).resolve().parent / "build_number"
_runtime_build_number = os.environ.get("BUILD_NUMBER", "").strip()
if not _runtime_build_number or _runtime_build_number == "dev":
    try:
        _runtime_build_number = _build_number_file.read_text(encoding="utf-8").strip()
    except OSError:
        _runtime_build_number = ""
if not _runtime_build_number:
    _runtime_build_number = __build_number__
builtins.print("\r\n" * 40, end="", flush=True)
_log("=" * 80)
_log(f"Version {__version__} (Build {_runtime_build_number}) starting")
_log("=" * 80)

app.register_blueprint(bambu_cloud_bp)
app.register_blueprint(ams_nfc_bp)


@app.get("/print-images/<path:filename>")
def print_image(filename):
    # Serve print thumbnails from the persistent runtime location.
    image_dir = os.path.join(app.root_path, "static", "prints")
    image_path = os.path.join(image_dir, filename)
    if not os.path.isfile(image_path):
        _log(f"[print-image] missing filename={filename!r} path={image_path!r}")
    return send_from_directory(image_dir, filename)


# Make the human-readable AMS console output match the values shown in the UI.
# Bambu's `humidity` field is a 1..5 level, not a percentage. If humidity_raw
# is available, show that as percent and retain the level as supplemental info.
# `remain == -1` means unknown, and an all-zero tray UUID is not a real UID.
_original_mqtt_log = mqtt_bambulab.log

def _ams_console_empty_status(ams_letter, tray_number):
    # Describe an unassigned tray without the ambiguous '---' marker.
    try:
        ams_id = ord(ams_letter) - ord("A")
        tray_id = int(tray_number) - 1
    except (TypeError, ValueError):
        return "Fach leer"

    for ams in (getattr(mqtt_bambulab, "LAST_AMS_CONFIG", {}) or {}).get("ams", []):
        if str(ams.get("id")) != str(ams_id):
            continue
        for tray in ams.get("tray", []) or []:
            if str(tray.get("id")) != str(tray_id):
                continue
            has_material = any(
                str(tray.get(field) or "").strip()
                for field in ("tray_type", "tray_sub_brands", "tray_info_idx", "setting_id")
            ) or str(tray.get("tray_color") or "").strip().upper() not in {"", "FFFFFFFF"}
            return "Keine Spule" if has_material else "Fach leer"
    return "Fach leer"


def _format_ams_console_log(*args, **kwargs):
    import re

    if not args:
        return _original_mqtt_log(*args, **kwargs)

    text = str(args[0])
    ams_match = re.match(r"^AMS \[([A-Z])\] \(hum: ([^,]+), temp: (.+)\)$", text)
    if ams_match:
        ams_letter = ams_match.group(1)
        ams_id = ord(ams_letter) - ord("A")
        for ams in (getattr(mqtt_bambulab, "LAST_AMS_CONFIG", {}) or {}).get("ams", []):
            try:
                same_ams = int(ams.get("id")) == ams_id
            except (TypeError, ValueError):
                same_ams = False
            if not same_ams:
                continue

            level = ams.get("humidity")
            raw = ams.get("humidity_raw")
            temp = ams.get("temp")
            if raw not in (None, "", 0, "0"):
                return _original_mqtt_log(
                    f"AMS [{ams_letter}] (hum: {raw}%, level: {level}, temp: {temp}ºC)",
                    *args[1:],
                    **kwargs,
                )
            return _original_mqtt_log(
                f"AMS [{ams_letter}] (humidity level: {level}, temp: {temp}ºC)",
                *args[1:],
                **kwargs,
            )

    if text.lstrip().startswith("- [A"):
        text = re.sub(r"\s+\(-0?1%\)", "", text)
        text = re.sub(r"\s+\[\[\s*0{32}\s*\]\]", "", text)
        empty_match = re.match(r"^(\s*- \[([A-Z])(\d+)\])\s+---\s*$", text)
        if empty_match:
            text = f"{empty_match.group(1)} {_ams_console_empty_status(empty_match.group(2), empty_match.group(3))}"
    elif re.match(r"^\s*- \[EX\]\s+---\s*$", text):
        text = "    - [EX] Keine Spule"

    return _original_mqtt_log(text, *args[1:], **kwargs)


mqtt_bambulab.log = _format_ams_console_log


# Bambu's FTPS server can be very slow while a print is being prepared. A hard
# Preserve the FTP transfer policy configured by tools_3mf: no total timeout,
# a low-speed stall guard, and a larger receive buffer.
_original_setup_pycurl_connection = _tools_3mf.setupPycurlConnection


def _setup_pycurl_connection_for_large_3mf(ftp_user, ftp_pass):
    connection = _original_setup_pycurl_connection(ftp_user, ftp_pass)
    try:
        connection.setopt(connection.CONNECTTIMEOUT, 5)
    except Exception:
        pass
    return connection


_tools_3mf.setupPycurlConnection = _setup_pycurl_connection_for_large_3mf

_original_download3mf_from_ftp = _tools_3mf.download3mfFromFTP


def _download3mf_with_unique_suffix_fallback(filename, dest_file, progress_callback=None, cancel_event=None):
    # Resolve printer-side filename prefixes without guessing between matches.
    #
    #     Bambu .bbl files may reference /sdcard/Kerstin.gcode.3mf while FTPS exposes
    #     the same job as /ItsLitho_Kerstin.gcode.3mf. Try the normal resolver first.
    #     If that fails, inspect the FTPS root. An exact filename wins over suffix
    #     variants; otherwise a suffix match is accepted only when it is unique.
    try:
        return _original_download3mf_from_ftp(filename, dest_file, progress_callback, cancel_event)
    except Exception as original_error:
        if cancel_event is not None and cancel_event.is_set():
            raise original_error
        expected_name = os.path.basename(str(filename or "").strip())
        if not expected_name.lower().endswith(".3mf"):
            raise

        try:
            root_names = _tools_3mf._ftp_read("/", directory=True).decode(
                "utf-8", errors="replace"
            ).splitlines()
        except Exception as list_error:
            _log(
                f"[3MF] FTPS-Root konnte fuer Suffix-Fallback nicht gelesen werden: {list_error}"
            )
            raise original_error

        root_names = [name.strip() for name in root_names if name.strip()]
        expected_lower = expected_name.lower()

        exact_matches = [
            name for name in root_names
            if name.lower() == expected_lower and name.lower().endswith(".3mf")
        ]
        if len(exact_matches) == 1:
            resolved_path = "/" + exact_matches[0]
            _log(
                f"[3MF] Exakter FTPS-Root-Treffer fuer {expected_name!r}: {resolved_path}; erneuter Download."
            )
            return _original_download3mf_from_ftp(resolved_path, dest_file, progress_callback, cancel_event)

        matches = [
            name
            for name in root_names
            if name.lower().endswith(expected_lower)
            and name.lower().endswith(".3mf")
        ]

        if len(matches) == 1:
            resolved_path = "/" + matches[0]
            _log(
                f"[3MF] Eindeutiger FTPS-Suffix-Treffer fuer {expected_name!r}: {resolved_path}"
            )
            return _original_download3mf_from_ftp(resolved_path, dest_file, progress_callback, cancel_event)

        if len(matches) > 1:
            _log(
                f"[3MF] Suffix-Fallback mehrdeutig fuer {expected_name!r}: {matches}. Keine Datei gewaehlt."
            )
        else:
            _log(
                f"[3MF] Kein FTPS-Suffix-Treffer fuer {expected_name!r} im Root-Verzeichnis."
            )
        raise original_error


_tools_3mf.download3mfFromFTP = _download3mf_with_unique_suffix_fallback
_filament_usage_tracker.download3mfFromFTP = _download3mf_with_unique_suffix_fallback

_original_download3mf_from_cloud = _tools_3mf.download3mfFromCloud


def _download3mf_from_cloud_with_timeout(url, dest_file, progress_callback=None, cancel_event=None):
    # Keep a connect timeout, but let an actively progressing download finish.
    _log("Downloading 3MF file from cloud...")
    response = _tools_3mf.requests.get(url, timeout=(5, None), stream=True)
    response.raise_for_status()
    total = int(response.headers.get("content-length") or 0)
    downloaded = 0
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        _tools_3mf._ensure_download_not_cancelled(cancel_event)
        if not chunk:
            continue
        dest_file.write(chunk)
        downloaded += len(chunk)
        if progress_callback:
            progress_callback(downloaded, total)


_tools_3mf.download3mfFromCloud = _download3mf_from_cloud_with_timeout
_filament_usage_tracker.download3mfFromCloud = _download3mf_from_cloud_with_timeout

_original_get_metadata_from_3mf = _tools_3mf.getMetaDataFrom3mf
_METADATA_RETRY_DELAYS = (2, 5)
_METADATA_RETRY_TIMEOUT_SECONDS = 240


def _metadata_source_with_filename(source):
    # Replace Bambu's pathless FTP URL with its accompanying 3MF filename.
    from urllib.parse import urlparse

    parsed = urlparse(str(source or ""))
    if parsed.scheme not in ("ftp", "ftps") or parsed.path.strip("/"):
        return source

    print_state = getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}
    for key in ("file", "gcode_file"):
        candidate = str(print_state.get(key) or "").strip()
        if candidate.lower().endswith(".3mf"):
            _log(f"[3MF] Pfadlose FTP-URL wird ueber {key}={candidate!r} aufgeloest.")
            return candidate
    return source


def _metadata_is_complete(metadata):
    if not isinstance(metadata, dict):
        return False
    required_values = (
        metadata.get("file"),
        metadata.get("model_path"),
        metadata.get("plateID"),
    )
    return bool(
        all(required_values)
        and metadata.get("image") is not None
        and isinstance(metadata.get("filaments"), dict)
        and metadata.get("filaments")
    )


def _get_metadata_from_3mf_with_retry(source):
    # A complete transfer must never be repeated merely because parsing was
    # incomplete.  The central worker downloads once and parses its local file.
    source = _metadata_source_with_filename(source)
    metadata = _original_get_metadata_from_3mf(source) or {}
    if not _metadata_is_complete(metadata):
        _log("[3MF] Metadaten unvollständig; kein automatischer Komplett-Download-Retry.")
    return metadata


_tools_3mf.getMetaDataFrom3mf = _get_metadata_from_3mf_with_retry
mqtt_bambulab.getMetaDataFrom3mf = _get_metadata_from_3mf_with_retry


@app.before_request
def open_bambu_setup_when_mqtt_is_offline():
    if request.endpoint == "home" and not mqtt_bambulab.isMqttClientConnected():
        return redirect(url_for("bambu_cloud.index"))


@app.route("/home")
def home_status():
    # Show the connected printer and its current connection/job status.
    return render_template(
        "home_status.html",
        last_print=print_history_service.get_latest_print_summary(),
    )


@app.route("/ams")
def ams():
    # Keep the AMS tray dashboard available under its explicit menu name.
    return _openspoolman_app_module.home()


def _current_printer_status_payload():
    connected = bool(mqtt_bambulab.isMqttClientConnected())
    print_state = getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}
    active_print_id = print_history_service.get_latest_active_print_id()
    if active_print_id is None:
        active_print_id = mqtt_bambulab.get_active_3mf_print_id()
    last_print = print_history_service.get_latest_print_summary()
    # Overlay live MQTT values so Home does not wait for the next persisted
    # layer checkpoint before showing the current printer progress.
    live_state = str(print_state.get("gcode_state") or "").upper()
    live_status = print_history_service.printer_state_to_history_status(live_state, print_state.get("print_error"))
    live_print_matches_latest = (
        last_print is not None
        and (
            active_print_id is not None
            and int(last_print["id"]) == int(active_print_id)
            or active_print_id is None
            and live_status in {"COMPLETED", "ABORTED", "FAILED"}
        )
    )
    if live_print_matches_latest:
        if live_status in {"PREPARING", "RUNNING", "PAUSED", "COMPLETED", "ABORTED", "FAILED"}:
            last_print = dict(last_print)
            last_print["status"] = live_status
        live_status_at = getattr(mqtt_bambulab, "PRINTER_STATE_LAST_UPDATE_AT", None)
        if live_status_at:
            last_print = dict(last_print)
            last_print["last_status_at"] = live_status_at
        try:
            if print_state.get("mc_percent") is not None:
                last_print = dict(last_print)
                last_print["printer_percent"] = float(print_state["mc_percent"])
        except (TypeError, ValueError):
            pass
        for source, target in (("layer_num", "layers_printed"), ("total_layer_num", "total_layers")):
            if print_state.get(source) is not None:
                try:
                    last_print = dict(last_print)
                    last_print[target] = int(print_state[source])
                except (TypeError, ValueError):
                    pass
    return {
        "printer_name": PRINTER_NAME or (getattr(mqtt_bambulab, "getPrinterModel", lambda: {})() or {}).get("devicename") or PRINTER_ID,
        "mqtt_connected": connected,
        "mqtt_status": _ui_text("Verbunden" if connected else "Nicht verbunden"),
        "printer_state": print_state.get("gcode_state") or "OFFLINE",
        "printer_status": _ui_text(_printer_status_code()),
        "print_id": active_print_id,
        "last_print": last_print,
        "temperatures": _printer_temperature_status(),
        "ams_environment": _ams_environment_status(),
        "download": {
            key: value for key, value in JOBS_3MF.get().items()
            if key in {"job_key", "state", "percent", "bytes_downloaded", "bytes_total", "speed_bytes_per_second", "elapsed_seconds", "error"}
        },
    }


@app.get("/home/state")
def home_state():
    return jsonify(_current_printer_status_payload())


def _load_openspoolman_version():
    from pathlib import Path
    import re
    version_file = Path(__file__).resolve().parent / "__version__.py"
    try:
        text = version_file.read_text(encoding="utf-8")
        match = re.search(r'^\s*__version__\s*=\s*["\x27]([^"\x27]+)["\x27]', text, re.MULTILINE)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


def _printer_temperature_status():
    print_state = getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}
    return {
        "hotend": print_state.get("nozzle_temper"),
        "hotend_target": print_state.get("nozzle_target_temper"),
        "bed": print_state.get("bed_temper"),
        "bed_target": print_state.get("bed_target_temper"),
    }


def _ams_environment_status():
    metrics = []
    for index, ams in enumerate((getattr(mqtt_bambulab, "LAST_AMS_CONFIG", {}) or {}).get("ams", []) or []):
        raw_humidity = ams.get("humidity_raw")
        humidity = f"{raw_humidity}%" if raw_humidity not in (None, "", 0, "0") else None
        temperature = ams.get("temp")
        if humidity is None and temperature in (None, "", "0", "0.0", 0, 0.0):
            continue
        metrics.append({
            "label": chr(ord("A") + index),
            "humidity": humidity,
            "temperature": temperature if temperature not in (None, "", "0", "0.0", 0, 0.0) else None,
        })
    return metrics


def _printer_is_busy():
    state = (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}).get("gcode_state")
    return bool(state and str(state).upper() not in {"IDLE", "FINISH", "FAILED", "STOP"})


def _printer_status_code():
    state = str((getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}).get("gcode_state") or "OFFLINE").upper()
    return {
        "IDLE": "Ready",
        "PREPARE": "Preparing",
        "RUNNING": "Printing",
        "PAUSE": "Paused",
        # FINISH describes the last print job, not a busy printer. The
        # printer itself is ready once Bambu reports FINISH.
        "FINISH": "Ready",
        "FAILED": "Failed",
        "STOP": "Cancelled",
    }.get(state, "Offline" if state == "OFFLINE" else state.title())


@app.context_processor
def inject_openspoolman_version():
    pending_nfc = nfc_pending_repository.has_pending_tags()
    return {
        "openspoolman_version": _load_openspoolman_version(),
        "openspoolman_build_number": _runtime_build_number,
        "printer_temperatures": _printer_temperature_status(),
        "printer_ams_environment": _ams_environment_status(),
        "printer_status": _ui_text(_printer_status_code()),
        "mqtt_connected": mqtt_bambulab.isMqttClientConnected(),
        "PRINTER_ID": PRINTER_ID,
        "printer_is_busy": _printer_is_busy(),
        "ams_operation_pending": mqtt_bambulab.is_any_ams_operation_pending(),
        "pending_nfc": pending_nfc,
        "active_print_id": print_history_service.get_latest_active_print_id(),
        "spoolman_public_url": _SPOOLMAN_PUBLIC_BASE_URL,
    }


@app.before_request
def reconcile_stale_print_history_statuses():
    print_state = (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {})
    state = str(print_state.get("gcode_state") or "").upper()
    terminal_status = print_history_service.printer_state_to_history_status(
        state, print_state.get("print_error")
    )
    if terminal_status in {"COMPLETED", "FAILED", "ABORTED"}:
        candidate = print_history_service.find_open_print_for_printer_job(
            print_state.get("subtask_name"),
            print_state.get("gcode_file"),
            print_state.get("url"),
        )
        if candidate and candidate.get("status") != terminal_status:
            status_at = candidate.get("predicted_end_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print_history_service.update_layer_tracking(
                candidate["id"], status=terminal_status, actual_end_time=status_at,
                last_status_at=status_at,
            )
            _log(f"[History] Offener Druck {candidate['id']} via Web-Reconcile auf {terminal_status} gesetzt ({state}).")
    if state not in {"IDLE", "FINISH", "FAILED", "STOP", "CANCEL", "CANCELED", "CANCELLED", "ABORT", "ABORTED"}:
        return None
    print_history_service.cancel_stale_running_prints(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route("/livecam")
def livecam():
    return render_template("livecam.html")


@app.route("/inventory")
def inventory():
    # Show the Spoolman inventory together with print consumption history.
    inventory_rows = []
    try:
        spools = spool_data.list_spools(include_archived=False)
    except Exception as exc:
        _log(f"Inventory could not load archived spools: {exc}")
        spools = [spool for spool in mqtt_bambulab.fetchSpools() if not spool.get("archived")]
    for spool in spools:
        spool_id = spool.get("id")
        if spool_id is None:
            continue
        prints = print_history_service.get_spool_print_usage(spool_id)
        # Spoolman owns the authoritative timestamp.  The local print history
        # is a fallback for older/local records that do not expose last_used.
        last_used = spool.get("last_used") or (prints[0].get("print_date") if prints else None)
        inventory_rows.append({
            "spool": spool,
            "prints": prints,
            "last_used": last_used,
        })
    show_status_column = any(bool(item["spool"].get("archived")) for item in inventory_rows)
    local_mode = not USE_SPOOLMAN
    try:
        vendors = inventory_db.list_entities("vendors") if local_mode else []
        materials = inventory_db.list_entities("materials") if local_mode else []
        filaments = inventory_db.list_filaments() if local_mode else []
    except Exception as exc:
        _log(f"Local inventory editor could not load: {exc}")
        vendors, materials, filaments = [], [], []
    return render_template(
        "inventory.html",
        inventory_rows=inventory_rows,
        show_status_column=show_status_column,
        inventory_backend=spool_data.get_backend(),
        local_inventory=local_mode,
        inventory_vendors=vendors,
        inventory_materials=materials,
        inventory_filaments=filaments,
    )


@app.get("/inventory/management/<entity>")
def inventory_management(entity):
    # Show one local inventory catalog in its own preview page.
    entity_config = {
        "vendors": ("Hersteller", "vendors"),
        "materials": ("Materialien", "materials"),
        "filaments": ("Filamente", "filaments"),
        "spools": ("Spulen", "spools"),
    }
    if entity not in entity_config:
        return render_template("error.html", error="Unbekannte Verwaltungsseite."), 404
    title, table_name = entity_config[entity]
    rows = []
    local_inventory = not USE_SPOOLMAN
    warning = None
    form_id = request.args.get("edit", type=int)
    copy_id = request.args.get("copy", type=int)
    form_mode = "edit" if form_id else ("copy" if copy_id else ("new" if request.args.get("new") else None))
    form_record = None
    management_vendors = []
    management_materials = []
    management_filaments = []
    management_spools = []
    try:
        spool_data.ensure_local_catalog()
        management_vendors = inventory_db.list_entities("vendors")
        management_materials = inventory_db.list_entities("materials")
        management_filaments = inventory_db.list_filaments()
        management_spools = inventory_db.list_spools(include_archived=True)
        if entity == "spools":
            rows = inventory_db.list_spools(include_archived=True)
        elif entity == "filaments":
            rows = inventory_db.list_filaments()
        else:
            rows = inventory_db.list_entities(table_name)
        if form_id:
            form_record = next((row for row in rows if int(row.get("id")) == form_id), None)
        elif copy_id and entity == "spools":
            source = next((row for row in rows if int(row.get("id")) == copy_id), None)
            if source:
                form_record = dict(source)
                form_record.pop("id", None)
                form_record["archived"] = False
    except Exception as exc:
        warning = "Die lokale Verwaltung ist derzeit nicht verfügbar."
        _log(f"Inventory management preview could not load {entity}: {exc!r}")
    return render_template(
        "inventory_management.html",
        management_entity=entity,
        management_title=title,
        management_rows=rows,
        local_inventory=local_inventory,
        management_warning=warning,
        management_form_mode=form_mode,
        management_form_record=form_record or {},
        management_vendors=management_vendors,
        management_materials=management_materials,
        management_filaments=management_filaments,
        management_spools=management_spools if entity == "filaments" else [],
        sunlu_color_options=SUNLU_COLOR_OPTIONS,
    )


@app.post("/inventory/management/<entity>")
def inventory_management_save(entity):
    # Save only to the local catalog; the active Spoolman backend remains unchanged.
    try:
        if entity == "spools":
            inventory_db.save_spool(_inventory_payload())
        else:
            inventory_db.save_entity(entity, _inventory_payload())
    except (ValueError, KeyError, TypeError) as exc:
        return redirect(url_for("inventory_management", entity=entity, new=1, error=str(exc)))
    except Exception as exc:
        _log(f"Local management create failed ({entity}): {exc!r}")
        return redirect(url_for("inventory_management", entity=entity, new=1, error="Speichern fehlgeschlagen."))
    return redirect(url_for("inventory_management", entity=entity, saved="created"))


@app.post("/inventory/management/<entity>/<int:entity_id>")
def inventory_management_update(entity, entity_id):
    # Update only the local catalog; the active Spoolman backend remains unchanged.
    try:
        if entity == "spools":
            inventory_db.save_spool(_inventory_payload(), entity_id)
        else:
            inventory_db.save_entity(entity, _inventory_payload(), entity_id)
    except (ValueError, KeyError, TypeError) as exc:
        return redirect(url_for("inventory_management", entity=entity, edit=entity_id, error=str(exc)))
    except Exception as exc:
        _log(f"Local management update failed ({entity} #{entity_id}): {exc!r}")
        return redirect(url_for("inventory_management", entity=entity, edit=entity_id, error="Speichern fehlgeschlagen."))
    return redirect(url_for("inventory_management", entity=entity, saved="updated"))


@app.post("/inventory/management/<entity>/<int:entity_id>/delete")
def inventory_management_delete(entity, entity_id):
    # Deletion is intentionally limited to the local preview catalog.
    if entity == "spools":
        inventory_db.delete_entity("spools", entity_id)
    else:
        inventory_db.delete_entity(entity, entity_id)
    return redirect(url_for("inventory_management", entity=entity, saved="deleted"))


def _inventory_payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def _require_local_inventory():
    if USE_SPOOLMAN:
        return jsonify({"error": "Lokale Verwaltung ist deaktiviert. Setze USE_SPOOLMAN=no in config.env und starte den Container neu."}), 409
    return None


@app.post("/inventory/manage/<entity>")
@app.put("/inventory/manage/<entity>/<int:entity_id>")
def inventory_save_entity(entity, entity_id=None):
    denied = _require_local_inventory()
    if denied:
        return denied
    try:
        saved_id = inventory_db.save_entity(entity, _inventory_payload(), entity_id)
        return jsonify({"ok": True, "id": saved_id})
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            return jsonify({"error": "Dieses Material ist bereits vorhanden."}), 409
        if "FOREIGN KEY constraint failed" in str(exc):
            return jsonify({"error": "Der Eintrag wird noch verwendet und kann nicht gelöscht werden."}), 409
        _log(f"Local inventory save failed ({entity}): {exc!r}")
        return jsonify({"error": "Speichern fehlgeschlagen."}), 500


@app.delete("/inventory/manage/<entity>/<int:entity_id>")
def inventory_delete_entity(entity, entity_id):
    denied = _require_local_inventory()
    if denied:
        return denied
    try:
        inventory_db.delete_entity(entity, entity_id)
        return jsonify({"ok": True})
    except Exception as exc:
        if "FOREIGN KEY constraint failed" in str(exc):
            return jsonify({"error": "Der Eintrag wird noch verwendet und kann nicht gelöscht werden."}), 409
        _log(f"Local inventory delete failed ({entity} #{entity_id}): {exc!r}")
        return jsonify({"error": "Löschen fehlgeschlagen."}), 500


@app.post("/inventory/manage/spools")
@app.put("/inventory/manage/spools/<int:spool_id>")
def inventory_save_spool(spool_id=None):
    denied = _require_local_inventory()
    if denied:
        return denied
    try:
        saved_id = inventory_db.save_spool(_inventory_payload(), spool_id)
        return jsonify({"ok": True, "id": saved_id})
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            return jsonify({"error": "Dieser NFC-Tag ist bereits einer anderen Spule zugeordnet."}), 409
        if "FOREIGN KEY constraint failed" in str(exc):
            return jsonify({"error": "Bitte ein gültiges Filament auswählen."}), 400
        _log(f"Local spool save failed: {exc!r}")
        return jsonify({"error": "Speichern fehlgeschlagen."}), 500

@app.route("/livecam/stream")
def livecam_stream():
    # Relay the P1S TLS/JPEG camera protocol as a browser-compatible MJPEG stream.
    from config import PRINTER_IP, PRINTER_CODE

    def frame_stream():
        if not PRINTER_IP or not PRINTER_CODE:
            return
        raw_socket = None
        camera_socket = None
        try:
            raw_socket = socket.create_connection((PRINTER_IP, 6000), timeout=10)
            tls_context = ssl._create_unverified_context()
            camera_socket = tls_context.wrap_socket(raw_socket, server_hostname=PRINTER_IP)
            auth_packet = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
            auth_packet += b"bblp".ljust(32, b"\0")
            auth_packet += PRINTER_CODE.encode("ascii", "ignore")[:32].ljust(32, b"\0")
            camera_socket.sendall(auth_packet)

            while True:
                header = _recv_exact(camera_socket, 16)
                if not header:
                    break
                payload_size, _, _, _ = struct.unpack("<IIII", header)
                if payload_size <= 0 or payload_size > 10 * 1024 * 1024:
                    break
                image = _recv_exact(camera_socket, payload_size)
                if not image or not image.startswith(b"\xff\xd8"):
                    continue
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(image)).encode() + b"\r\n\r\n" + image + b"\r\n"
        except (OSError, ssl.SSLError, ValueError, struct.error):
            return
        finally:
            if camera_socket is not None:
                camera_socket.close()
            elif raw_socket is not None:
                raw_socket.close()

    return Response(stream_with_context(frame_stream()), mimetype="multipart/x-mixed-replace; boundary=frame")

def _recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(min(4096, remaining))
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@app.get("/ams/state-generation")
def ams_state_generation():
    temperatures = _printer_temperature_status()
    print_state = getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}
    active_print_id = print_history_service.get_latest_active_print_id()
    if active_print_id is None:
        active_print_id = mqtt_bambulab.get_active_3mf_print_id()
    return jsonify({
        "generation": getattr(mqtt_bambulab, "LAST_AMS_CONFIG_GENERATION", 0),
        "hotend": temperatures.get("hotend"),
        "hotend_target": temperatures.get("hotend_target"),
        "bed": temperatures.get("bed"),
        "bed_target": temperatures.get("bed_target"),
        "printer_state": print_state.get("gcode_state") or "OFFLINE",
        "printer_status": _ui_text(_printer_status_code()),
        "print_id": active_print_id,
        "download": {
            key: value for key, value in JOBS_3MF.get().items()
            if key in {"job_key", "state", "percent", "bytes_downloaded", "bytes_total", "speed_bytes_per_second", "elapsed_seconds", "error"}
        },
    })


@app.post("/refresh-ams")
def refresh_ams():
    # Read a fresh AMS state without writing filament settings to the printer.
    import time
    global _AMS_REFRESH_LAST_REQUEST
    import mqtt_bambulab
    from messages import PUSH_ALL
    target_endpoint = request.form.get("next_endpoint")
    if target_endpoint not in {"home", "home_status"}:
        target_endpoint = "home"

    if not mqtt_bambulab.isMqttClientConnected():
        return redirect(url_for(
            target_endpoint,
            success_message="AMS konnte nicht aktualisiert werden: MQTT ist nicht verbunden."
        ))

    now = time.monotonic()
    if now - _AMS_REFRESH_LAST_REQUEST < _AMS_REFRESH_MIN_INTERVAL_SECONDS:
        _log("AMS-AKTUALISIERUNG: doppelte Statusabfrage verworfen (Cooldown)")
        return redirect(url_for(target_endpoint))
    if not _AMS_REFRESH_LOCK.acquire(blocking=False):
        _log("AMS-AKTUALISIERUNG: doppelte Statusabfrage verworfen (laufende Abfrage)")
        return redirect(url_for(target_endpoint))

    _AMS_REFRESH_LAST_REQUEST = now
    try:
        before_generation = getattr(mqtt_bambulab, "LAST_AMS_CONFIG_GENERATION", 0)

        if not mqtt_bambulab.publish(mqtt_bambulab.getMqttClient(), PUSH_ALL):
            return redirect(url_for(
                target_endpoint,
                success_message="AMS-Abfrage konnte nicht gesendet werden."
            ))
        mqtt_bambulab.FORCE_AMS_STATUS_LOG = True
        # PUSH_ALL is deliberately read-only.  Do not call setActiveSpool here:
        # that sends ams_filament_setting and changes the printer state.
        _log("AMS-AKTUALISIERUNG: Statusabfrage gesendet")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            time.sleep(0.10)
            if getattr(mqtt_bambulab, "LAST_AMS_CONFIG_GENERATION", 0) > before_generation:
                return redirect(url_for(target_endpoint))

        return redirect(url_for(target_endpoint))
    finally:
        _AMS_REFRESH_LOCK.release()


def _custom_tray_clear():
    import traceback
    import inventory_service

    ams_id = request.form.get("ams")
    tray_id = request.form.get("tray")

    if ams_id is None or tray_id is None:
        return render_template("error.html", exception="Missing AMS ID or Tray ID.")

    if getattr(_openspoolman_app_module, "READ_ONLY_MODE", False):
        return render_template(
            "error.html",
            exception="Live read-only mode: clearing tray assignments is disabled.",
        )

    if mqtt_bambulab.is_any_ams_operation_pending():
        return render_template(
            "error.html",
            exception="Das Fach wird noch aktualisiert. Clear ist erst nach stabilem Fachstatus möglich.",
        )

    try:
        if not mqtt_bambulab.isMqttClientConnected():
            return render_template(
                "error.html",
                exception="MQTT is disconnected. The tray was not cleared on the printer.",
            )

        if not mqtt_bambulab.clear_ams_tray_assignment(ams_id, tray_id):
            return render_template(
                "error.html",
                exception="Could not send the AMS clear command to the printer.",
            )

        inventory_service.clear_active_spool_for_tray(ams_id, tray_id)
        inventory_service.SPOOLS = []
        return redirect(
            url_for(
                "home",
                success_message=(
                    f"Tray cleared in OpenSpoolMan and on printer for AMS {ams_id}, "
                    f"Tray {int(tray_id) + 1}."
                ),
            )
        )
    except Exception as exc:
        traceback.print_exc()
        return render_template("error.html", exception=str(exc))

app.view_functions["tray_clear"] = _custom_tray_clear


@app.before_request
def _block_mutating_tray_actions_while_pending():
    # Reject direct assignment URLs until the preceding tray operation is stable.
    # refresh_ams only sends the read-only PUSH_ALL request.  It must remain
    # available while printing so Home can refresh the printer/job status;
    # mutating tray actions below remain blocked while the printer is busy.
    if request.endpoint not in {"fill", "tray_load", "assign_bambu_spool"}:
        return None

    ams_id = request.args.get("ams")
    tray_id = request.args.get("tray")
    spool_id = request.args.get("spool_id")
    if not ams_id or tray_id is None or not spool_id:
        return None
    if _printer_is_busy():
        return render_template("error.html", exception="AMS-Aktionen sind während eines Drucks deaktiviert.")
    if request.endpoint == "fill":
        try:
            selected_spool = spool_data.get_spool(spool_id)
            active_tray = (selected_spool.get("extra") or {}).get("active_tray")
            if active_tray and str(active_tray) not in {"", '""'}:
                return render_template(
                    "error.html",
                    exception="Diese Spule ist bereits einem Fach zugeordnet und kann nicht doppelt verwendet werden.",
                )
        except Exception:
            pass
    if mqtt_bambulab.is_any_ams_operation_pending():
        return render_template(
            "error.html",
            exception="Das Fach wird noch aktualisiert. Eine neue Zuordnung ist erst nach stabilem Fachstatus möglich.",
        )
    return None


_original_set_active_spool = _openspoolman_app_module.setActiveSpool
_original_builtin_print = builtins.print
_original_upstream_log = getattr(_openspoolman_app_module, "log", None)

def _quiet_upstream_log(*args, **kwargs):
    # Suppress the verbose raw AMS command dump from upstream app.py.
    if args and isinstance(args[0], dict):
        print_data = args[0].get("print")
        if isinstance(print_data, dict) and print_data.get("command") == "ams_filament_setting":
            return None
    if _original_upstream_log is not None:
        return _original_upstream_log(*args, **kwargs)
    return None

if _original_upstream_log is not None:
    _openspoolman_app_module.log = _quiet_upstream_log

def _quiet_upstream_ams_print(*args, **kwargs):
    # Suppress legacy AMS detail dumps emitted by upstream app.py.
    text = " ".join(str(arg) for arg in args).lstrip()
    if text.startswith("[OpenSpoolMan] AMS Fill v2:") or text.startswith("{'print':"):
        return None
    return _original_builtin_print(*args, **kwargs)

def _set_active_spool_bambu_compatible(ams_id, tray_id, spool_data):
    import copy

    normalized = copy.deepcopy(spool_data)
    filament = normalized.get("filament", {}) or {}
    extra = filament.setdefault("extra", {})
    vendor = ((filament.get("vendor") or {}).get("name") or "").strip().upper()
    raw_filament_id = str(extra.get("filament_id", "") or "").strip().strip('"')
    if raw_filament_id.isdigit():
        extra["filament_id"] = ""

    builtins.print = _quiet_upstream_ams_print
    try:
        result = _original_set_active_spool(ams_id, tray_id, normalized)
    finally:
        builtins.print = _original_builtin_print
    # The assignment endpoint updates Spoolman, but the service-level spool
    # cache may still contain the previous active_tray value.  Invalidate it
    # so the next home render immediately reflects the confirmed assignment.
    import inventory_service
    inventory_service.SPOOLS = []
    return result

_openspoolman_app_module.setActiveSpool = _set_active_spool_bambu_compatible


_original_mqtt_publish = mqtt_bambulab.publish

def _publish_without_empty_setting_id(client, message):
    if isinstance(message, dict):
        print_data = message.get("print")
        if (
            isinstance(print_data, dict)
            and print_data.get("command") == "ams_filament_setting"
        ):
            import copy
            message = copy.deepcopy(message)
            ams_id = message["print"].get("ams_id")
            tray_id = message["print"].get("tray_id")
            # Only enrich actual fill/profile commands. Clear commands also use
            # this MQTT command but must remain empty.
            is_fill = bool(
                str(message["print"].get("tray_type") or "").strip()
                or str(message["print"].get("tray_info_idx") or "").strip()
            )
            if ams_id is not None and tray_id is not None and is_fill:
                if not message["print"].get("setting_id"):
                    message["print"].pop("setting_id", None)
                try:
                    import json
                    from inventory_service import fetchSpools
                    active_tray = json.dumps(f"{PRINTER_ID}_{ams_id}_{tray_id}")
                    spool = next(
                        (item for item in fetchSpools(True)
                         if item.get("extra", {}).get("active_tray") == active_tray),
                        None,
                    )
                    vendor = ((spool or {}).get("filament") or {}).get("vendor") or {}
                    vendor_name = str(vendor.get("name") or "").strip()
                    if vendor_name:
                        message["print"]["tray_sub_brands"] = vendor_name
                    filament = ((spool or {}).get("filament") or {})
                    if not str(message["print"].get("tray_color") or "").strip():
                        color_hex = str(filament.get("color_hex") or "").strip().lstrip("#")
                        if len(color_hex) == 6:
                            message["print"]["tray_color"] = color_hex.upper() + "FF"
                    extra = ((spool or {}).get("filament") or {}).get("extra") or {}
                    setting_id = str(extra.get("setting_id") or "").strip().strip('"')
                    filament_id = str(extra.get("filament_id") or "").strip().strip('"')
                    if setting_id:
                        message["print"]["setting_id"] = setting_id
                    if filament_id:
                        message["print"]["tray_info_idx"] = filament_id
                except Exception:
                    pass
    return _original_mqtt_publish(client, message)

mqtt_bambulab.publish = _publish_without_empty_setting_id


if __name__ == "__main__":
    app.run(debug=True)
