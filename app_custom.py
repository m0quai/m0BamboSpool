# Custom m0BamboSpool entry point; keeps local extensions outside upstream app.py.
import os
import math
import json
import hashlib
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

from app import app

import app as _m0bambospool_app_module

# Upstream only recognizes the base material PLA.  Keep the concrete PLA+
# material in the local catalog while using a safe PLA range when no per-filament
# nozzle-temperature extra field has been maintained.
_original_generate_filament_temperatures = _m0bambospool_app_module.generate_filament_temperatures

def _generate_filament_temperatures_with_pla_plus(filament_type, filament_brand):
    normalized = str(filament_type or "").strip().upper().replace(" ", "")
    if normalized.startswith("PLA+"):
        return {"filament_min_temp": 190, "filament_max_temp": 240}
    return _original_generate_filament_temperatures(filament_type, filament_brand)

_m0bambospool_app_module.generate_filament_temperatures = _generate_filament_temperatures_with_pla_plus


def _readonly_augment_tray(spool_list, tray_data, ams_id, tray_id):
    _m0bambospool_app_module.augment_tray_data(
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
            uuid_empty = (
                not tray_uuid
                or tray_uuid == "00000000000000000000000000000000"
            )
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

_m0bambospool_app_module._augment_tray = _readonly_augment_tray


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
from flask import jsonify, redirect, request, url_for, render_template, send_from_directory, Response, stream_with_context, session, g, has_request_context
import mqtt_bambulab
import inventory_repository as spool_data
import inventory_database as inventory_db
import print_history as print_history_service
from config import EXTERNAL_SPOOL_AMS_ID, PRINTER_ID, PRINTER_NAME
from __version__ import __build_number__, __version__
import tools_3mf as _tools_3mf
import filament_usage_tracker as _filament_usage_tracker
from jobs_3mf import JOBS_3MF
from ui_formatting import format_ui_datetime

from logger import log as _log

try:
    # Create the local administration schema before serving requests.
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
_m0bambospool_app_module.LAYER_TRACKING_STATUS_DISPLAY.update({
    "PREPARING": ("Preparing", "info"),
    "PAUSED": ("Paused", "secondary"),
})
_MOS_HISTORY_PAGE_SIZE = 20
_original_get_prints_with_filament = print_history_service.get_prints_with_filament

def _get_prints_with_mos_pagination(limit=None, offset=None, include_deleted=False):
    # Apply MOS pagination without changing the upstream history route.
    if not has_request_context() or request.endpoint != "print_history":
        return _original_get_prints_with_filament(limit, offset, include_deleted)

    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    requested_print_id = request.args.get("print_id")
    if requested_print_id:
        target_page = print_history_service.get_print_page(
            requested_print_id,
            _MOS_HISTORY_PAGE_SIZE,
            include_deleted=include_deleted,
        )
        if target_page is not None:
            page = target_page
    g.mos_history_page = page
    prints, total_prints = _original_get_prints_with_filament(
        limit=_MOS_HISTORY_PAGE_SIZE,
        offset=(page - 1) * _MOS_HISTORY_PAGE_SIZE,
        include_deleted=include_deleted,
    )
    max_page = max(1, math.ceil(total_prints / _MOS_HISTORY_PAGE_SIZE))
    if page > max_page:
        page = max_page
        g.mos_history_page = page
        prints, total_prints = _original_get_prints_with_filament(
            limit=_MOS_HISTORY_PAGE_SIZE,
            offset=(page - 1) * _MOS_HISTORY_PAGE_SIZE,
            include_deleted=include_deleted,
        )
    g.mos_history_total_prints = total_prints
    return prints, total_prints

print_history_service.get_prints_with_filament = _get_prints_with_mos_pagination
_original_app_render_template = _m0bambospool_app_module.render_template

def _history_page_controls_script(page, total_pages):
    # Add numbered history pages after the untouched upstream template renders.
    return f'''<script>
window.m0InitHistoryPagination = window.m0InitHistoryPagination || (() => {{
  const currentPage = {page};
  const totalPages = {total_pages};
  const pageUrl = (targetPage) => {{
    const params = new URLSearchParams(window.location.search);
    ["ams_slot", "print_id", "spool_id", "old_spool_id"].forEach((key) => params.delete(key));
    params.set("page", targetPage);
    return `${{window.location.pathname}}?${{params.toString()}}`;
  }};
  const addPage = (list, targetPage, label, active = false, disabled = false) => {{
    const item = document.createElement("li");
    item.className = `page-item${{active ? " active" : ""}}${{disabled ? " disabled" : ""}}`;
    const link = document.createElement(active || disabled ? "span" : "a");
    link.className = "page-link";
    link.textContent = label;
    if (!active && !disabled) link.href = pageUrl(targetPage);
    item.appendChild(link);
    list.appendChild(item);
  }};
  document.querySelectorAll('nav.print-history-pagination ul').forEach((list) => {{
    const items = Array.from(list.children);
    const previous = items[0];
    const next = items.at(-1);
    if (!previous || !next) return;
    previous.querySelector("a")?.setAttribute("href", pageUrl(Math.max(1, currentPage - 1)));
    next.querySelector("a")?.setAttribute("href", pageUrl(Math.min(totalPages, currentPage + 1)));
    items.slice(1, -1).forEach((item) => item.remove());
    const first = Math.max(1, currentPage - 2);
    const last = Math.min(totalPages, currentPage + 2);
    if (first > 1) {{
      addPage(list, 1, "1");
      if (first > 2) addPage(list, 0, "…", false, true);
    }}
    for (let number = first; number <= last; number += 1) {{
      addPage(list, number, String(number), number === currentPage);
    }}
    if (last < totalPages) {{
      if (last < totalPages - 1) addPage(list, 0, "…", false, true);
      addPage(list, totalPages, String(totalPages));
    }}
    list.insertBefore(previous, list.firstChild);
    list.appendChild(next);
  }});
}});
window.m0InitHistoryPagination();
</script>'''

def _render_template_with_printer_progress(template_name, *args, **kwargs):
    if template_name == "print_history.html":
        total_prints = getattr(g, "mos_history_total_prints", None)
        if total_prints is not None:
            try:
                requested_page = max(int(request.args.get("page", 1)), 1)
            except (TypeError, ValueError):
                requested_page = 1
            requested_page = getattr(g, "mos_history_page", requested_page)
            kwargs["page"] = requested_page
            kwargs["per_page"] = _MOS_HISTORY_PAGE_SIZE
            kwargs["total_pages"] = max(1, math.ceil(total_prints / _MOS_HISTORY_PAGE_SIZE))
        filter_options = print_history_service.get_history_filter_options(
            request.args.get("show_deleted") == "1"
        )
        status_options = []
        for status_key in filter_options["statuses"]:
            status_label = _m0bambospool_app_module.LAYER_TRACKING_STATUS_DISPLAY.get(
                status_key, (status_key.title(), "secondary")
            )[0]
            status_options.append({
                "value": status_label.lower(),
                "label": _ui_text(status_label),
            })
        kwargs["history_filter_dates"] = filter_options["dates"]
        kwargs["history_filter_statuses"] = status_options
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
    rendered = _original_app_render_template(template_name, *args, **kwargs)
    if template_name == "print_history.html":
        return rendered.replace(
            "</body>",
            _history_page_controls_script(kwargs["page"], kwargs["total_pages"]) + "</body>",
        )
    return rendered

_m0bambospool_app_module.render_template = _render_template_with_printer_progress

_UI_TRANSLATIONS = {
        "de": {"Home":"Home", "History":"History", "LiveCam":"LiveCam", "Inventory":"Inventar", "Settings":"Settings", "Spool":"Spule", "Hersteller":"Hersteller", "Material":"Material", "Filament":"Filament", "Farbe":"Farbe", "Restgewicht":"Restgewicht", "Drucke":"Drucke", "Alle":"Alle", "Status":"Status", "ausgewählt":"ausgewählt", "Datum":"Datum", "Auftrag":"Auftrag", "Verbrauch":"Verbrauch", "Ohne Namen":"Ohne Namen", "ungebraucht":"ungebraucht", "Keine Spulen gefunden":"Keine Spulen gefunden", "Sprache":"Sprache", "Deutsch":"Deutsch", "Englisch":"Englisch", "Speichern":"Speichern", "Print history":"Druckhistorie", "Jetzt aktualisieren":"Jetzt aktualisieren", "Druckname":"Druckname", "Aktive anzeigen":"Aktive anzeigen", "Gelöschte anzeigen":"Gelöschte anzeigen", "Previous":"Zurück", "Next":"Weiter", "Page":"Seite", "of":"von", "Abmelden":"Abmelden", "Drucker übernehmen":"Drucker übernehmen", "Code bestätigen":"Code bestätigen", "Bei Bambu anmelden":"Bei Bambu anmelden", "Drucker Verbindung":"Drucker Verbindung", "Verbindungsmodus":"Verbindungsmodus", "Lokaler LAN-Modus":"Lokaler LAN-Modus", "Online-Authentifizierung":"Online-Authentifizierung", "Direkte Verbindung zum Drucker im lokalen Netzwerk.":"Direkte Verbindung zum Drucker im lokalen Netzwerk.", "Drucker-IP":"Drucker-IP", "Seriennummer":"Seriennummer", "Druckername":"Druckername", "Printer Access LAN":"Printer Access LAN", "Drucker auswählen":"Drucker auswählen", "Keine gebundenen Drucker zurückgegeben.":"Keine gebundenen Drucker zurückgegeben."},
    "en": {"Home":"Home", "History":"History", "LiveCam":"LiveCam", "Inventory":"Inventory", "Settings":"Settings", "Spool":"Spool", "Hersteller":"Manufacturer", "Material":"Material", "Filament":"Filament", "Farbe":"Color", "Restgewicht":"Remaining weight", "Drucke":"Prints", "Alle":"All", "Status":"Status", "ausgewählt":"selected", "Datum":"Date", "Auftrag":"Job", "Verbrauch":"Usage", "Ohne Namen":"Unnamed", "ungebraucht":"Unused", "Keine Spulen gefunden":"No spools found", "Sprache":"Language", "Deutsch":"German", "Englisch":"English", "Speichern":"Save", "Print history":"Print history", "Jetzt aktualisieren":"Refresh now", "Druckname":"Print name", "Aktive anzeigen":"Show active", "Gelöschte anzeigen":"Show deleted", "Previous":"Previous", "Next":"Next", "Page":"Page", "of":"of", "Abmelden":"Sign out", "Drucker übernehmen":"Use printer", "Code bestätigen":"Confirm code", "Bei Bambu anmelden":"Sign in to Bambu", "Drucker Verbindung":"Printer connection", "Verbindungsmodus":"Connection mode", "Lokaler LAN-Modus":"Local LAN mode", "Online-Authentifizierung":"Online authentication", "Direkte Verbindung zum Drucker im lokalen Netzwerk.":"Direct connection to the printer on the local network.", "Drucker-IP":"Printer IP", "Seriennummer":"Serial number", "Druckername":"Printer name", "Printer Access LAN":"Printer LAN access", "Drucker auswählen":"Select printer", "Keine gebundenen Drucker zurückgegeben.":"No bound printers returned."},
}

# Additional labels used by the existing pages. Keeping these keys here lets
# legacy templates migrate incrementally while still using one translation
# source.
_UI_TRANSLATIONS["de"].update({
    "Deutsch oder Englisch für die Benutzeroberfläche": "Deutsch oder Englisch für die Benutzeroberfläche",
    "Ein LAN Access-Code ist gespeichert. Leer lassen, um ihn beizubehalten.": "Ein LAN Access-Code ist gespeichert. Leer lassen, um ihn beizubehalten.",
    "Bambu Cloud": "Bambu Cloud", "Angemeldet": "Angemeldet", "als": "als", "Name": "Name", "Modell": "Modell", "Online": "Online", "Lokale IP": "Lokale IP", "Bambu Cloud anmelden": "Bambu Cloud anmelden", "Bambu E-Mail": "Bambu E-Mail", "Passwort": "Passwort", "Verbunden": "Verbunden", "Nicht verbunden": "Nicht verbunden", "MQTT": "MQTT", "Status:": "Status:",
    "Bambu tag": "Bambu-Tag", "Error": "Fehler", "Back": "Zurück", "Fill Tray": "Fach füllen", "External Spool": "Externe Spule", "Solve issue": "Problem lösen", "Livebild": "Livebild", "NFC-Tags": "NFC-Tags", "Zurück": "Zurück", "Unbekannter NFC-Tag": "Unbekannter NFC-Tag", "Spule auswählen ...": "Spule auswählen ...", "Spule zuordnen": "Spule zuordnen", "Keine unbekannten NFC-Tags vorhanden.": "Keine unbekannten NFC-Tags vorhanden.", "Change spool": "Spule ändern", "Assign spool to print": "Spule dem Druck zuordnen", "Assign Tray": "Fach zuordnen", "NFC Write Process": "NFC-Schreibvorgang", "Follow the steps below to write data to your NFC tag.": "Folge den Schritten zum Schreiben deines NFC-Tags.", "Allow NFC usage if prompted.": "Erlaube die NFC-Nutzung, wenn du dazu aufgefordert wirst.", "Bring the tag close to your phone when prompted.": "Halte den Tag an dein Telefon, wenn du dazu aufgefordert wirst.", "Humidity": "Luftfeuchtigkeit", "Filament type mismatch": "Filamenttyp stimmt nicht überein", "No AMS material selected": "Kein AMS-Material ausgewählt", "Fach leer": "Fach leer", "Unknown spool detected": "Unbekannte Spule erkannt", "Last used:": "Zuletzt verwendet:", "Full": "Voll", "Remaining:": "Verbleibend:", "Assign": "Zuordnen", "Clear": "Leeren", "Fill": "Füllen", "No Image": "Kein Bild", "Print ID": "Druck-ID", "Druck": "Druck", "Layers": "Schichten", "Billed": "Abgerechnet", "Progress": "Fortschritt", "No spool assigned": "Keine Spule zugeordnet", "printed": "gedruckt", "expected": "erwartet", "Spule ändern": "Spule ändern", "Eintrag aus der History ausblenden": "Eintrag aus der History ausblenden", "Eintrag löschen": "Eintrag löschen", "Remaining Weight:": "Restgewicht:", "Remaining Length:": "Restlänge:", "Nozzle Temp:": "Düsentemperatur:", "Registered:": "Registriert:", "Last Used:": "Zuletzt verwendet:", "Keine Historie vorhanden.": "Keine Historie vorhanden.", "Success!": "Erfolgreich!"
})
_UI_TRANSLATIONS["en"].update({
    "Deutsch oder Englisch für die Benutzeroberfläche": "German or English for the user interface", "Ein LAN Access-Code ist gespeichert. Leer lassen, um ihn beizubehalten.": "A LAN access code is saved. Leave empty to keep it.", "Angemeldet": "Signed in", "als": "as", "Lokale IP": "Local IP", "Verbunden": "Connected", "Nicht verbunden": "Not connected", "Status:": "Status:", "Zurück": "Back", "Spule zuordnen": "Assign spool", "Keine unbekannten NFC-Tags vorhanden.": "No unknown NFC tags found.", "No Image": "No image", "Success!": "Success!", "Keine Historie vorhanden.": "No history available.", "Clear": "Clear", "Fill": "Fill", "Assign": "Assign", "Back": "Back", "Error": "Error", "Livebild": "Live view", "Humidity": "Humidity", "Full": "Full", "Remaining:": "Remaining:", "Last used:": "Last used:", "Unbekannter NFC-Tag": "Unknown NFC tag", "Spule auswählen ...": "Select spool ...", "Spule ändern": "Change spool", "Eintrag löschen": "Delete entry", "Eintrag aus der History ausblenden": "Hide history entry", "Keine Spule zugeordnet": "No spool assigned", "gedruckt": "printed", "erwartet": "expected", "Druck-ID": "Print ID", "Druck": "Print", "Schichten": "Layers", "Abgerechnet": "Billed", "Fortschritt": "Progress", "Restgewicht:": "Remaining weight:", "Restlänge:": "Remaining length:", "Düsentemperatur:": "Nozzle temperature:", "Registriert:": "Registered:", "Zuletzt verwendet:": "Last used:"
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
_UI_TRANSLATIONS["de"].update({"Ende": "Ende", "Dauer": "Dauer"})
_UI_TRANSLATIONS["en"].update({"Ende": "End", "Dauer": "Duration"})
_UI_TRANSLATIONS["de"].update({"Letzter Druck": "Letzter Druck", "Letzter Status": "Letzter Status", "Letzte Buchung": "Letzte Buchung"})
_UI_TRANSLATIONS["en"].update({"Letzter Druck": "Last print", "Letzter Status": "Last status", "Letzte Buchung": "Last booking"})

# The external module is the authoritative source; the legacy inline map
# above is retained only for compatibility with older local checkouts.
from translations import UI_TRANSLATIONS as _UI_TRANSLATIONS

_UI_TRANSLATIONS["de"].update({"Schicht": "Schicht", "Ende": "Ende", "Restlaufzeit": "Restlaufzeit", "Verbleibend": "verbleibend"})
_UI_TRANSLATIONS["en"].update({"Schicht": "Layer", "Ende": "End", "Restlaufzeit": "Remaining", "Verbleibend": "remaining"})

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


def _format_ui_number(value, digits=2):
    """Format quantities according to the selected UI language."""
    try:
        formatted = f"{float(0 if value in (None, '') else value):,.{int(digits)}f}"
    except (TypeError, ValueError):
        return str(value or "0")
    if _ui_language() == "de":
        return formatted.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return formatted

@app.context_processor
def inject_ui_language():
    return {
        "_": _ui_text,
        "current_language": _ui_language(),
        "available_languages": _ui_languages(),
        "language_metadata": _ui_language_metadata(),
        "format_datetime": lambda value: format_ui_datetime(value, _ui_language()),
        "format_number": _format_ui_number,
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
    return _m0bambospool_app_module.home()


@app.route("/about")
def about():
    # Keep project provenance and external references in one translated page.
    return render_template("about.html")


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
        and active_print_id is not None
        and int(last_print["id"]) == int(active_print_id)
    )
    if live_print_matches_latest:
        if live_status in {"PREPARING", "RUNNING", "PAUSED", "COMPLETED", "ABORTED", "FAILED"}:
            last_print = dict(last_print)
            last_print["status"] = live_status
        if active_print_id is not None and int(last_print["id"]) == int(active_print_id):
            live_status_at = getattr(mqtt_bambulab, "PRINTER_STATE_LAST_UPDATE_AT", None)
            if live_status_at:
                last_print = dict(last_print)
                last_print["last_status_at"] = live_status_at
            try:
                mc_percent = print_state.get("mc_percent")
                if mc_percent is not None and math.isfinite(float(mc_percent)):
                    last_print = dict(last_print)
                    last_print["printer_percent"] = float(mc_percent)
            except (TypeError, ValueError):
                pass
            for source, target in (("layer_num", "layers_printed"), ("total_layer_num", "total_layers")):
                if print_state.get(source) is not None:
                    try:
                        value = float(print_state[source])
                        if not math.isfinite(value):
                            continue
                        last_print = dict(last_print)
                        last_print[target] = int(value)
                    except (TypeError, ValueError):
                        pass
    return {
        "printer_name": PRINTER_NAME or (getattr(mqtt_bambulab, "getPrinterModel", lambda: {})() or {}).get("devicename") or PRINTER_ID,
        "mqtt_connected": connected,
        "mqtt_status": _ui_text("Verbunden" if connected else "Nicht verbunden"),
        "printer_state": print_state.get("gcode_state") or "OFFLINE",
        "printer_status": _ui_text(_printer_status_code()),
        "printer_preparation_status": _ui_text(_printer_preparation_status_code() or ""),
        "print_id": active_print_id,
        "last_print": last_print,
        "temperatures": _printer_temperature_status(),
        "ams_environment": _ams_environment_status(),
        "download": {
            key: value for key, value in JOBS_3MF.get().items()
            if key in {"transfer_id", "state", "percent", "bytes_downloaded", "bytes_total", "speed_bytes_per_second", "elapsed_seconds", "error"}
        },
    }


def _inventory_ams_status_fingerprint():
    # Return a stable fingerprint for physical AMS/external-tray changes.
    config = getattr(mqtt_bambulab, "LAST_AMS_CONFIG", {}) or {}
    tray_fields = (
        "id",
        "tray_type",
        "tray_sub_brands",
        "tray_color",
        "remain",
        "tray_uuid",
        "tray_info_idx",
        "setting_id",
        "tray_status",
        "status",
    )
    ams_snapshot = []
    for ams in config.get("ams", []) or []:
        ams_snapshot.append({
            "id": ams.get("id"),
            "trays": [
                {field: tray.get(field) for field in tray_fields}
                for tray in ams.get("tray", []) or []
            ],
        })
    external = config.get("vt_tray") or {}
    snapshot = {
        "connected": bool(mqtt_bambulab.isMqttClientConnected()),
        "ams": ams_snapshot,
        "external": {field: external.get(field) for field in tray_fields if field != "id"},
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@app.get("/inventory/state")
def inventory_state():
    # The inventory page uses this lightweight endpoint to notice physical
    # AMS changes without polling or rebuilding the complete HTML document.
    return jsonify({"ams_fingerprint": _inventory_ams_status_fingerprint()})


@app.get("/home/state")
def home_state():
    return jsonify(_current_printer_status_payload())


def _load_m0bambospool_version():
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


def _printer_temperature_waiting_code():
    print_state = getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}
    try:
        hotend = float(print_state.get("nozzle_temper"))
        hotend_target = float(print_state.get("nozzle_target_temper"))
        bed = float(print_state.get("bed_temper"))
        bed_target = float(print_state.get("bed_target_temper"))
    except (TypeError, ValueError):
        return None

    waiting_for_hotend = hotend_target > hotend + 2
    waiting_for_bed = bed_target > bed + 2
    if waiting_for_bed and waiting_for_hotend:
        return "Waiting for bed and hotend to reach target temperature"
    if waiting_for_bed:
        return "Waiting for bed to reach target temperature"
    if waiting_for_hotend:
        return "Waiting for hotend to reach target temperature"
    return None


_PRINT_PREPARATION_STAGE_CODES = {
    1: "Auto bed leveling",
    2: "Preheating heatbed",
    3: "Vibration compensation",
    4: "Changing filament",
    7: "Heating hotend",
    8: "Calibrating extrusion",
    9: "Scanning bed surface",
    10: "Inspecting first layer",
    11: "Identifying build plate",
    12: "Calibrating micro lidar",
    13: "Homing toolhead",
    14: "Cleaning nozzle tip",
    15: "Checking extruder temperature",
    19: "Calibrating extrusion flow",
    22: "Unloading filament",
    24: "Loading filament",
    25: "Calibrating motor noise",
    29: "Cooling chamber",
    31: "Motor noise calibration",
    36: "Checking absolute accuracy",
    37: "Absolute accuracy calibration",
    38: "Checking absolute accuracy",
    39: "Calibrating nozzle offset",
    40: "Bed leveling (high temperature)",
    41: "Checking quick release",
    42: "Checking door and cover",
    43: "Laser calibration",
    44: "Checking platform",
    45: "Checking camera position",
    46: "Calibrating camera",
    47: "Bed leveling phase 1",
    48: "Bed leveling phase 2",
    49: "Heating chamber",
    50: "Cooling heatbed",
    51: "Printing calibration lines",
    52: "Checking material",
    53: "Live view camera calibration",
    54: "Waiting for heatbed temperature",
    55: "Checking material position",
    56: "Cutting module offset calibration",
    57: "Measuring surface",
    58: "Thermal preconditioning",
    59: "Homing blade holder",
    60: "Calibrating camera offset",
    61: "Calibrating blade holder",
    62: "Hotend pick and place test",
    63: "Waiting for chamber temperature",
    64: "Preparing hotend",
    65: "Calibrating nozzle clump detection",
    66: "Purifying chamber air",
    77: "Preparing AMS",
}


def _printer_preparation_status_code():
    print_state = getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}
    state = str(print_state.get("gcode_state") or "").upper()
    if state not in {"PREPARE", "RUNNING", "PAUSE"}:
        return None

    temperature_waiting_code = _printer_temperature_waiting_code()
    if temperature_waiting_code:
        return temperature_waiting_code

    try:
        stage = int(print_state.get("stg_cur"))
    except (TypeError, ValueError):
        stage = None
    if stage in _PRINT_PREPARATION_STAGE_CODES:
        return _PRINT_PREPARATION_STAGE_CODES[stage]
    if state == "PREPARE":
        return "Preparing printer"
    return None


def _ams_environment_status():
    metrics = []
    for index, ams in enumerate((getattr(mqtt_bambulab, "LAST_AMS_CONFIG", {}) or {}).get("ams", []) or []):
        raw_humidity = ams.get("humidity_raw")
        humidity = f"{raw_humidity}%" if raw_humidity not in (None, "", 0, "0") else None
        try:
            humidity_level = int(ams.get("humidity"))
        except (TypeError, ValueError):
            humidity_level = None
        if humidity_level not in range(1, 6):
            humidity_level = None
        temperature = ams.get("temp")
        if (
            humidity is None
            and humidity_level is None
            and temperature in (None, "", "0", "0.0", 0, 0.0)
        ):
            continue
        metrics.append({
            "label": chr(ord("A") + index),
            "humidity": humidity,
            "humidity_level": humidity_level,
            "temperature": temperature if temperature not in (None, "", "0", "0.0", 0, 0.0) else None,
        })
    return metrics


def _printer_is_busy():
    state = (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}).get("gcode_state")
    return bool(state and str(state).upper() not in {"IDLE", "FINISH", "FAILED", "STOP"})


def _printer_status_code():
    print_state = (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {})
    state = str(print_state.get("gcode_state") or "OFFLINE").upper()
    # Bambu retains a terminal job state after that job ends.  It describes
    # the history entry, not the printer's current availability.
    if state in {"FINISH", "FAILED", "STOP", "CANCEL", "CANCELED", "CANCELLED", "ABORT", "ABORTED"}:
        return "Ready" if mqtt_bambulab.isMqttClientConnected() else "Offline"
    waiting_code = _printer_temperature_waiting_code()
    if waiting_code == "Waiting for bed and hotend to reach target temperature":
        return "Heating bed and hotend"
    if waiting_code == "Waiting for bed to reach target temperature":
        return "Heating bed"
    if waiting_code == "Waiting for hotend to reach target temperature":
        return "Heating hotend"
    return {
        "IDLE": "Ready",
        "PREPARE": "Preparing",
        "RUNNING": "Printing",
        "PAUSE": "Paused",
    }.get(state, "Offline" if state == "OFFLINE" else state.title())


@app.context_processor
def inject_m0bambospool_version():
    pending_nfc = nfc_pending_repository.has_pending_tags()
    return {
        "m0bambospool_version": _load_m0bambospool_version(),
        "m0bambospool_build_number": _runtime_build_number,
        "printer_temperatures": _printer_temperature_status(),
        "printer_ams_environment": _ams_environment_status(),
        "printer_status": _ui_text(_printer_status_code()),
        "printer_waiting_message": _ui_text(_printer_temperature_waiting_code() or ""),
        "printer_preparation_status": _ui_text(_printer_preparation_status_code() or ""),
        "mqtt_connected": mqtt_bambulab.isMqttClientConnected(),
        "PRINTER_ID": PRINTER_ID,
        "printer_is_busy": _printer_is_busy(),
        "ams_operation_pending": mqtt_bambulab.is_any_ams_operation_pending(),
        "pending_nfc": pending_nfc,
        "active_print_id": print_history_service.get_latest_active_print_id(),
    }


@app.before_request
def reconcile_stale_print_history_statuses():
    print_state = (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {})
    state = str(print_state.get("gcode_state") or "").upper()
    terminal_status = print_history_service.printer_state_to_history_status(
        state, print_state.get("print_error")
    )
    if terminal_status in {"COMPLETED", "FAILED", "ABORTED"}:
        candidate_finder = (
            print_history_service.find_active_print_for_printer_source
            if terminal_status == "COMPLETED"
            else print_history_service.find_open_print_for_printer_source
        )
        candidate = candidate_finder(
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
    return render_template("livecam.html", active_print_running=_active_print_is_running())


@app.route("/inventory")
def inventory():
    # Show the local inventory together with print consumption history.
    inventory_rows = []
    inventory_fill_context = None
    if request.args.get("fill") == "1":
        try:
            inventory_fill_context = {
                "ams_id": int(request.args["ams"]),
                "tray_id": int(request.args["tray"]),
            }
        except (KeyError, TypeError, ValueError):
            inventory_fill_context = None
    try:
        # Load archived rows as well; the inventory UI filters them by the
        # "Archivierte anzeigen" switch and can then mark them explicitly.
        spools = spool_data.list_spools(include_archived=True)
    except Exception as exc:
        _log(f"Inventory could not load archived spools: {exc}")
        spools = [spool for spool in mqtt_bambulab.fetchSpools() if not spool.get("archived")]
    for spool in spools:
        spool_id = spool.get("id")
        if spool_id is None:
            continue
        prints = print_history_service.get_spool_print_usage(spool_id)
        # The local catalog owns the authoritative timestamp.  The print history
        # is a fallback for older records that do not expose last_used.
        last_used = spool.get("last_used") or (prints[0].get("print_date") if prints else None)
        inventory_rows.append({
            "spool": spool,
            "prints": prints,
            "last_used": last_used,
        })
    show_status_column = any(bool(item["spool"].get("archived")) for item in inventory_rows)
    local_mode = True
    try:
        # The local catalog is the inventory, AMS and print-usage backend.
        for item in inventory_rows:
            inventory_db.ensure_local_spool_from_inventory(item["spool"])
        vendors = inventory_db.list_entities("vendors")
        materials = inventory_db.list_entities("materials")
        colors = inventory_db.list_colors()
        filaments = inventory_db.list_filaments()
        local_spools = inventory_db.list_spools(include_archived=True)
        inventory_last_used = {
            str(item["spool"].get("id")): item.get("last_used")
            for item in inventory_rows
            if item.get("spool", {}).get("id") is not None and item.get("last_used")
        }
        for spool in local_spools:
            # Keep the editor in sync with the inventory backend's authoritative
            # timestamp, including values stored as ISO strings with a `T`.
            backend_last_used = inventory_last_used.get(str(spool.get("id")))
            if backend_last_used:
                spool["last_used"] = backend_last_used
            # The editor displays the read-only value using the central
            # locale-aware formatter instead of the browser's native locale.
            spool["last_used_display"] = format_ui_datetime(spool.get("last_used"), _ui_language())
        # Include ownership metadata so a tag already assigned to another
        # spool can be shown but not selected in the spool editor.
        nfc_tags = inventory_db.list_nfc_tags(include_assignment=True)
        local_spools_by_id = {
            str(spool.get("id")): spool
            for spool in local_spools
            if spool.get("id") is not None
        }
        for item in inventory_rows:
            if item["spool"].get("id") is not None:
                spool_id = int(item["spool"]["id"])
                local_spool = local_spools_by_id.get(str(spool_id))
                # The local inventory database is authoritative for locks.
                # Keep all three lock states on the row consumed by the UI so
                # NFC-bound spools retain their distinct icon as well.
                item["spool"]["active_print_locked"] = bool(
                    local_spool and local_spool.get("active_print_locked")
                )
                item["spool"]["active_ams_locked"] = bool(
                    local_spool and local_spool.get("active_ams_locked")
                )
                item["spool"]["nfc_ams_locked"] = bool(
                    local_spool and local_spool.get("nfc_ams_locked")
                )
                # The local catalog owns NFC assignments. Keep the tag
                # identity on the row so the inventory can show its NFC icon
                # independently from the AMS/print lock state.
                if local_spool:
                    item["spool"]["nfc_tag_id"] = local_spool.get("nfc_tag_id")
                    item["spool"]["nfc_tag_value"] = local_spool.get("nfc_tag_value")
                    if not item["spool"].get("nfc_tag_value"):
                        extra = local_spool.get("extra") or {}
                        item["spool"]["nfc_tag_value"] = extra.get("tag")
                item["spool"]["last_used_display"] = format_ui_datetime(item.get("last_used"), _ui_language())
    except Exception as exc:
        _log(f"Local inventory editor could not load: {exc}")
        vendors, materials, colors, filaments, local_spools, nfc_tags = [], [], [], [], [], []
    active_tray_options = []
    for option in _inventory_service.get_printer_tray_options(PRINTER_ID):
        option = dict(option)
        if option.get("kind") == "external":
            option["label"] = _ui_text("External Spool")
        else:
            option["label"] = f"{_ui_text('Tray')} {option.get('tray_number')}"
        active_tray_options.append(option)
    active_print_running = _active_print_is_running()
    return render_template(
        "inventory.html",
        inventory_rows=inventory_rows,
        show_status_column=show_status_column,
        inventory_backend=spool_data.get_backend(),
        local_inventory=local_mode,
        local_catalog_visible=True,
        inventory_vendors=vendors,
        inventory_materials=materials,
        inventory_filaments=filaments,
        inventory_spools=local_spools,
        inventory_colors=colors,
        inventory_nfc_tags=nfc_tags,
        active_tray_options=active_tray_options,
        active_print_running=active_print_running,
        inventory_fill_context=inventory_fill_context,
    )


def _normalise_active_tray_value(value):
    text = str(value or "").strip()
    if not text or text == '""':
        return ""
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        decoded = text
    return str(decoded or "").strip()


def _active_print_is_running():
    if _printer_is_busy():
        return True
    try:
        return print_history_service.get_latest_active_print_id() is not None
    except Exception:
        return False


def _sync_local_spool_tray_assignment(spool_id, requested_value):
    if _active_print_is_running():
        raise ValueError(_ui_text("Das aktive Fach kann während eines Drucks nicht geändert werden."))
    if getattr(_m0bambospool_app_module, "READ_ONLY_MODE", False):
        raise ValueError(_ui_text("Die AMS-Zuordnung ist im Nur-Lese-Modus deaktiviert."))
    if not mqtt_bambulab.isMqttClientConnected():
        raise ValueError(_ui_text("MQTT ist nicht verbunden. Das Fach wurde nicht geändert."))
    if mqtt_bambulab.is_any_ams_operation_pending():
        raise ValueError(_ui_text("Eine AMS-Aktualisierung läuft noch. Bitte kurz warten."))

    desired = _normalise_active_tray_value(requested_value)
    options = _inventory_service.get_printer_tray_options(PRINTER_ID)
    option_by_value = {str(option["value"]): option for option in options}
    desired_option = option_by_value.get(desired) if desired else None
    if desired and desired_option is None:
        raise ValueError(_ui_text("Ungültige Fachzuordnung."))

    spool = inventory_db.get_spool(spool_id)
    current = _normalise_active_tray_value(spool.get("active_tray"))
    if current == desired:
        return inventory_db.update_spool_fields(spool_id, {"active_tray": desired})

    if desired:
        for other in inventory_db.list_spools(include_archived=True):
            if int(other.get("id")) == int(spool_id):
                continue
            if _normalise_active_tray_value(other.get("active_tray")) == desired:
                raise ValueError(_ui_text("Die gewählte Spule ist bereits einem anderen Fach zugeordnet."))

    current_option = option_by_value.get(current) if current else None
    if current and current_option is None:
        raise ValueError(_ui_text("Die bisherige Fachzuordnung ist unbekannt und wurde nicht geändert."))

    if current_option and not mqtt_bambulab.clear_ams_tray_assignment(
        current_option["ams_id"], current_option["tray_id"]
    ):
        raise ValueError(_ui_text("Die bisherige Fachzuordnung konnte am AMS nicht gelöscht werden."))

    if desired_option:
        try:
            _m0bambospool_app_module.setActiveSpool(
                desired_option["ams_id"], desired_option["tray_id"], spool
            )
        except Exception as exc:
            raise ValueError(f'{_ui_text("Die neue Fachzuordnung konnte am AMS nicht gesetzt werden.")} {exc}') from exc

    return inventory_db.update_spool_fields(spool_id, {"active_tray": desired})


@app.patch("/inventory/local-filaments/<int:filament_id>")
def update_local_filament(filament_id):
    """Persist one inline field without making the local catalog an active backend."""
    payload = request.get_json(silent=True) or {}
    try:
        updated = inventory_db.update_filament_fields(filament_id, payload)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local filament inline update failed ({filament_id}): {exc!r}")
        return jsonify({"error": "Speichern fehlgeschlagen."}), 500
    related_spools = [spool for spool in inventory_db.list_spools(include_archived=True)
                      if int(spool.get("filament_id") or 0) == int(filament_id)]
    return jsonify({"saved": True, "filament": updated, "spools": related_spools})


@app.post("/inventory/local-filaments")
def create_local_filament():
    payload = request.get_json(silent=True) or {}
    payload.setdefault("name", "Neues Filament")
    try:
        filament_id = inventory_db.save_entity("filaments", payload)
        created = next(item for item in inventory_db.list_filaments() if int(item["id"]) == filament_id)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local filament create failed: {exc!r}")
        return jsonify({"error": "Anlegen fehlgeschlagen."}), 500
    return jsonify({"saved": True, "filament": created}), 201


@app.post("/inventory/local-filaments/<int:filament_id>/duplicate")
def duplicate_local_filament(filament_id):
    try:
        source = next((item for item in inventory_db.list_filaments() if int(item["id"]) == int(filament_id)), None)
        if not source:
            raise KeyError(f"No filament with ID {filament_id}")
        values = {field: source.get(field) for field in (
            "vendor_id", "material_id", "color_id", "color_hex", "diameter", "weight",
            "spool_weight", "price", "nozzle_temp_min", "nozzle_temp_max",
            "cali_idx", "bambu_filament_id", "bambu_setting_id",
        )}
        values["name"] = f'{source.get("name") or "Filament"} (Kopie)'
        new_id = inventory_db.save_entity("filaments", values)
        created = next(item for item in inventory_db.list_filaments() if int(item["id"]) == int(new_id))
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local filament duplicate failed ({filament_id}): {exc!r}")
        return jsonify({"error": "Kopieren fehlgeschlagen."}), 500
    return jsonify({"saved": True, "filament": created}), 201


@app.post("/inventory/local-vendors")
def create_local_vendor():
    payload = request.get_json(silent=True) or {}
    payload.setdefault("name", "Neuer Hersteller")
    try:
        vendor_id = inventory_db.save_entity("vendors", payload)
        created = next(item for item in inventory_db.list_entities("vendors") if int(item["id"]) == vendor_id)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local vendor create failed: {exc!r}")
        return jsonify({"error": "Anlegen fehlgeschlagen."}), 500
    return jsonify({"saved": True, "vendor": created}), 201


@app.patch("/inventory/local-vendors/<int:vendor_id>")
def update_local_vendor(vendor_id):
    payload = request.get_json(silent=True) or {}
    try:
        updated = inventory_db.update_vendor_fields(vendor_id, payload)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local vendor inline update failed ({vendor_id}): {exc!r}")
        return jsonify({"error": "Speichern fehlgeschlagen."}), 500
    return jsonify({"saved": True, "vendor": updated})


@app.patch("/inventory/local-spools/<int:spool_id>")
def update_local_spool(spool_id):
    payload = request.get_json(silent=True) or {}
    try:
        if "active_tray" in payload:
            return jsonify({
                "error": _ui_text("Die Fachzuordnung erfolgt ausschließlich auf der AMS-Seite.")
            }), 400
        updated = inventory_db.update_spool_fields(spool_id, payload)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local spool inline update failed ({spool_id}): {exc!r}")
        return jsonify({"error": "Speichern fehlgeschlagen."}), 500
    return jsonify({"saved": True, "spool": updated})


@app.post("/inventory/local-spools/<int:spool_id>/assign-tray")
def assign_local_spool_to_tray(spool_id):
    payload = request.get_json(silent=True) or {}
    try:
        ams_id = int(payload.get("ams_id"))
        tray_id = int(payload.get("tray_id"))
        option = next(
            option for option in _inventory_service.get_printer_tray_options(PRINTER_ID)
            if int(option["ams_id"]) == ams_id and int(option["tray_id"]) == tray_id
        )
        updated = _sync_local_spool_tray_assignment(spool_id, option["value"])
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        return jsonify({"error": _ui_text(str(exc))}), 400
    except Exception as exc:
        _log(f"Local spool AMS assignment failed ({spool_id}): {exc!r}")
        return jsonify({"error": _ui_text("Die Spule konnte dem AMS nicht zugeordnet werden.")}), 500
    return jsonify({"saved": True, "spool": updated})


@app.post("/inventory/local-spools/<int:spool_id>/stock-adjustment")
def adjust_local_spool_stock(spool_id):
    payload = request.get_json(silent=True) or {}
    try:
        updated = inventory_db.adjust_spool_stock(spool_id, payload.get("mode"), payload.get("value"))
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local spool stock adjustment failed ({spool_id}): {exc!r}")
        return jsonify({"error": "Bestandskorrektur fehlgeschlagen."}), 500
    return jsonify({"saved": True, "spool": updated})


@app.post("/inventory/local-spools")
def create_local_spool():
    payload = request.get_json(silent=True) or {}
    try:
        filament_id = int(payload.get("filament_id"))
        spool_id = inventory_db.save_spool({"filament_id": filament_id, "archived": False})
        created = inventory_db.get_spool(spool_id)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local spool create failed: {exc!r}")
        return jsonify({"error": "Anlegen fehlgeschlagen."}), 500
    return jsonify({"saved": True, "spool": created}), 201


@app.post("/inventory/local-spools/<int:spool_id>/duplicate")
def duplicate_local_spool(spool_id):
    try:
        new_id = inventory_db.duplicate_spool(spool_id)
        created = inventory_db.get_spool(new_id)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local spool duplicate failed ({spool_id}): {exc!r}")
        return jsonify({"error": "Duplizieren fehlgeschlagen."}), 500
    return jsonify({"saved": True, "spool": created}), 201


@app.delete("/inventory/local-spools/<int:spool_id>")
def delete_local_spool(spool_id):
    try:
        inventory_db.delete_spool(spool_id)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _log(f"Local spool delete failed ({spool_id}): {exc!r}")
        return jsonify({"error": "Löschen fehlgeschlagen."}), 500
    return jsonify({"saved": True, "deleted": spool_id})


@app.get("/inventory/management")
def inventory_management_index():
    # The central administration landing page was replaced by the settings entry.
    return redirect(url_for("bambu_cloud.index"))


@app.get("/inventory/management/<entity>")
def inventory_management(entity):
    # Show one local inventory catalog in its own preview page.
    entity_config = {
        "vendors": ("Hersteller", "vendors"),
        "materials": ("Materialien", "materials"),
        "colors": ("Farben", "colors"),
        "filaments": ("Filamente", "filaments"),
        "spools": ("Spulen", "spools"),
        "nfc-tags": ("NFC-Tags", "nfc_tags"),
    }
    if entity not in entity_config:
        return render_template("error.html", error="Unbekannte Verwaltungsseite."), 404
    title, table_name = entity_config[entity]
    rows = []
    local_inventory = True
    warning = None
    form_id = request.args.get("edit", type=int)
    copy_id = request.args.get("copy", type=int)
    form_mode = "edit" if form_id else ("copy" if copy_id else ("new" if request.args.get("new") else None))
    form_record = None
    management_vendors = []
    management_materials = []
    management_filaments = []
    management_spools = []
    management_colors = []
    try:
        spool_data.ensure_local_catalog()
        management_vendors = inventory_db.list_entities("vendors")
        management_materials = inventory_db.list_entities("materials")
        management_colors = inventory_db.list_colors()
        management_filaments = inventory_db.list_filaments()
        management_spools = inventory_db.list_spools(include_archived=True)
        if entity == "spools":
            rows = inventory_db.list_spools(include_archived=True)
        elif entity == "filaments":
            rows = inventory_db.list_filaments()
        elif entity == "colors":
            rows = inventory_db.list_colors()
        elif entity == "nfc-tags":
            rows = inventory_db.list_nfc_tags(include_assignment=True)
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
    template_name = "nfc_management.html" if entity == "nfc-tags" else ("reference_management.html" if entity in {"materials", "colors"} else "inventory_management.html")
    return render_template(
        template_name,
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
        inventory_colors=management_colors,
    )


@app.post("/inventory/management/<entity>")
def inventory_management_save(entity):
    # Save only to the local catalog.
    try:
        if entity == "spools":
            inventory_db.save_spool(_inventory_payload())
        elif entity == "colors":
            inventory_db.save_color(_inventory_payload())
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
    # Update only the local catalog.
    try:
        payload = _inventory_payload()
        # Technical Bambu metadata is intentionally hidden from the editor, but
        # must survive an ordinary filament edit unchanged.
        if entity == "filaments":
            existing = next(
                (row for row in inventory_db.list_filaments() if int(row.get("id")) == int(entity_id)),
                None,
            )
            if existing:
                for field in ("cali_idx", "bambu_filament_id", "bambu_setting_id"):
                    payload.setdefault(field, existing.get(field))
        if entity == "spools":
            inventory_db.save_spool(payload, entity_id)
        elif entity == "colors":
            inventory_db.save_color(payload, entity_id)
        else:
            inventory_db.save_entity(entity, payload, entity_id)
    except (ValueError, KeyError, TypeError) as exc:
        return redirect(url_for("inventory_management", entity=entity, edit=entity_id, error=str(exc)))
    except Exception as exc:
        _log(f"Local management update failed ({entity} #{entity_id}): {exc!r}")
        return redirect(url_for("inventory_management", entity=entity, edit=entity_id, error="Speichern fehlgeschlagen."))
    return redirect(url_for("inventory_management", entity=entity, saved="updated"))


@app.post("/inventory/management/<entity>/<int:entity_id>/delete")
def inventory_management_delete(entity, entity_id):
    # Deletion is intentionally limited to the local preview catalog.
    try:
        if entity == "spools":
            inventory_db.delete_entity("spools", entity_id)
        else:
            inventory_db.delete_entity(entity, entity_id)
    except (ValueError, KeyError, TypeError) as exc:
        return redirect(url_for("inventory_management", entity=entity, edit=entity_id, error=str(exc)))
    except Exception as exc:
        _log(f"Local management delete failed ({entity} #{entity_id}): {exc!r}")
        return redirect(url_for("inventory_management", entity=entity, edit=entity_id, error="Löschen fehlgeschlagen."))
    return redirect(url_for("inventory_management", entity=entity, saved="deleted"))


def _inventory_payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def _require_local_inventory():
    return None


@app.post("/inventory/manage/<entity>")
@app.put("/inventory/manage/<entity>/<int:entity_id>")
def inventory_save_entity(entity, entity_id=None):
    denied = _require_local_inventory()
    if denied:
        return denied
    try:
        payload = _inventory_payload()
        saved_id = inventory_db.save_color(payload, entity_id) if entity == "colors" else inventory_db.save_entity(entity, payload, entity_id)
        return jsonify({"ok": True, "id": saved_id, "record": {"id": saved_id, **payload}})
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
    except ValueError as exc:
        return jsonify({"error": _ui_text(str(exc))}), 409
    except Exception as exc:
        if "FOREIGN KEY constraint failed" in str(exc):
            return jsonify({"error": "Der Eintrag wird noch verwendet und kann nicht gelöscht werden."}), 409
        _log(f"Local inventory delete failed ({entity} #{entity_id}): {exc!r}")
        return jsonify({"error": "Löschen fehlgeschlagen."}), 500


@app.post("/inventory/manage/nfc-tags")
@app.put("/inventory/manage/nfc-tags/<int:tag_id>")
def inventory_save_nfc_tag(tag_id=None):
    try:
        payload = _inventory_payload()
        if tag_id is None:
            value = inventory_db.normalize_nfc_tag(payload.get("tag_value"))
            if not value:
                raise ValueError("Der Tag-Wert darf nicht leer sein")
            inventory_db.register_nfc_tag(value)
            tag_id = next(item["id"] for item in inventory_db.list_nfc_tags() if item["tag_value"] == value)
            if "comment" in payload:
                inventory_db.update_nfc_tag(tag_id, {"comment": payload.get("comment")})
        else:
            inventory_db.update_nfc_tag(tag_id, payload)
        record = next(item for item in inventory_db.list_nfc_tags(include_assignment=True) if int(item["id"]) == int(tag_id))
        return jsonify({"ok": True, "id": int(tag_id), "record": record})
    except (ValueError, KeyError) as exc:
        return jsonify({"error": _ui_text(str(exc))}), 400
    except Exception as exc:
        _log(f"Local NFC tag save failed ({tag_id}): {exc!r}")
        return jsonify({"error": "NFC-Tag konnte nicht gespeichert werden."}), 500


@app.delete("/inventory/manage/nfc-tags/<int:tag_id>")
def inventory_delete_nfc_tag(tag_id):
    try:
        inventory_db.delete_nfc_tag(tag_id)
        return jsonify({"ok": True})
    except (ValueError, KeyError) as exc:
        return jsonify({"error": _ui_text(str(exc))}), 409
    except Exception as exc:
        _log(f"Local NFC tag delete failed ({tag_id}): {exc!r}")
        return jsonify({"error": "NFC-Tag konnte nicht gelöscht werden."}), 500
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
        "printer_waiting_message": _ui_text(_printer_temperature_waiting_code() or ""),
        "printer_preparation_status": _ui_text(_printer_preparation_status_code() or ""),
        "print_id": active_print_id,
        "download": {
            key: value for key, value in JOBS_3MF.get().items()
            if key in {"transfer_id", "state", "percent", "bytes_downloaded", "bytes_total", "speed_bytes_per_second", "elapsed_seconds", "error"}
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

    if getattr(_m0bambospool_app_module, "READ_ONLY_MODE", False):
        return render_template(
            "error.html",
            exception="Live read-only mode: clearing tray assignments is disabled.",
        )

    if mqtt_bambulab.is_any_ams_operation_pending():
        return render_template(
            "error.html",
            exception="Das Fach wird noch aktualisiert. Clear ist erst nach stabilem Fachstatus möglich.",
        )

    nfc_spool = inventory_service.get_nfc_bound_spool_for_tray(ams_id, tray_id)
    if nfc_spool:
        return render_template(
            "error.html",
            exception="Diese Spule wurde per NFC erkannt und kann nur durch das physische Entfernen aus dem AMS freigegeben werden.",
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
                    f"Tray cleared in m0BamboSpool and on printer for AMS {ams_id}, "
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
    guarded_endpoints = {"fill", "tray_load", "assign_bambu_spool"}
    if request.endpoint not in guarded_endpoints:
        return None

    ams_id = request.args.get("ams")
    tray_id = request.args.get("tray")
    spool_id = request.args.get("spool_id")
    if ams_id and tray_id is not None:
        nfc_spool = _inventory_service.get_nfc_bound_spool_for_tray(ams_id, tray_id)
        if nfc_spool and (not spool_id or str(nfc_spool.get("id")) != str(spool_id)):
            return render_template(
                "error.html",
                exception="Diese Spule wurde per NFC erkannt und kann nicht manuell ersetzt werden.",
            )
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


_original_set_active_spool = _m0bambospool_app_module.setActiveSpool
_original_builtin_print = builtins.print
_original_upstream_log = getattr(_m0bambospool_app_module, "log", None)

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
    _m0bambospool_app_module.log = _quiet_upstream_log

def _quiet_upstream_ams_print(*args, **kwargs):
    # Suppress legacy AMS detail dumps emitted by upstream app.py.
    text = " ".join(str(arg) for arg in args).lstrip()
    if text.startswith("[m0BamboSpool] AMS Fill v2:") or text.startswith("{'print':"):
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
    # The assignment endpoint updates the local catalog, and the service-level spool
    # cache may still contain the previous active_tray value.  Invalidate it
    # so the next home render immediately reflects the confirmed assignment.
    import inventory_service
    inventory_service.SPOOLS = []
    return result

_m0bambospool_app_module.setActiveSpool = _set_active_spool_bambu_compatible


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
