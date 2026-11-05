"""Local-only printer settings for m0BamboSpool.

The application talks to the printer through its LAN MQTT/FTPS endpoint.  No
cloud account, online connection mode, device discovery, or online
authentication belongs in this workflow.
"""

from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, url_for

import config as app_config
import database as app_database
import inventory_database
import mqtt_bambulab


bp = Blueprint("settings", __name__, url_prefix="/settings")
ROOT = Path(__file__).resolve().parent
CONFIG_ENV = ROOT / "config.env"

DEFAULT_PRINTER_COST_SETTINGS = {
    "electricity_price_eur_per_kwh": 0.35,
    "printer_power_w": 100.0,
    "printer_purchase_price_eur": 599.0,
    "printer_lifetime_hours": 5000.0,
}


def get_printer_cost_settings() -> dict[str, float]:
    """Return validated local assumptions and derived costs per minute."""
    # Cost assumptions are optional runtime metadata.  A transient SQLite
    # lock/I/O problem must not take Home or Settings down with it; defaults
    # remain valid until the metadata can be read again.
    try:
        raw = app_database.get_runtime_metadata("settings", "printer_costs")
    except Exception:
        raw = None
    values = dict(DEFAULT_PRINTER_COST_SETTINGS)
    if isinstance(raw, dict):
        for key in values:
            try:
                number = float(raw.get(key, values[key]))
            except (TypeError, ValueError):
                continue
            if number >= 0:
                values[key] = number
    if values["printer_lifetime_hours"] <= 0:
        values["printer_lifetime_hours"] = DEFAULT_PRINTER_COST_SETTINGS["printer_lifetime_hours"]
    values["energy_cost_per_minute"] = (
        values["printer_power_w"] / 1000.0
        * values["electricity_price_eur_per_kwh"] / 60.0
    )
    values["depreciation_per_minute"] = (
        values["printer_purchase_price_eur"]
        / values["printer_lifetime_hours"]
        / 60.0
    )
    values["printer_cost_per_minute"] = (
        values["energy_cost_per_minute"] + values["depreciation_per_minute"]
    )
    return values


@bp.app_context_processor
def mqtt_menu_context():
    try:
        return {"mqtt_connected": bool(mqtt_bambulab.isMqttClientConnected())}
    except Exception:
        return {"mqtt_connected": False}


def _write_config_env(values: dict[str, str]) -> None:
    """Update only local settings and preserve the rest of config.env."""
    lines = CONFIG_ENV.read_text(encoding="utf-8").splitlines() if CONFIG_ENV.exists() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    CONFIG_ENV.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _page():
    local_materials: list[dict] = []
    local_colors: list[dict] = []
    local_nfc_tags: list[dict] = []
    try:
        local_materials = inventory_database.list_entities("materials")
        local_colors = inventory_database.list_colors()
        local_nfc_tags = inventory_database.list_nfc_tags(include_assignment=False)
    except Exception:
        pass
    return render_template(
        "local_settings.html",
        config={
            "printer_name": app_config.PRINTER_NAME,
            "printer_ip": app_config.PRINTER_IP,
            "printer_id": app_config.PRINTER_ID,
            "port": 8883,
            "username": "bblp",
            "lan_access_code_present": bool(app_config.PRINTER_ACCESS_LAN),
        },
        connected=mqtt_bambulab.isMqttClientConnected(),
        local_materials=local_materials,
        local_colors=local_colors,
        local_nfc_tags=local_nfc_tags,
        printer_cost_settings=get_printer_cost_settings(),
    )


@bp.get("/")
def index():
    return _page()


@bp.post("/save")
def save():
    printer_ip = (request.form.get("printer_ip") or "").strip()
    printer_id = (request.form.get("printer_id") or "").strip().upper()
    printer_name = (request.form.get("printer_name") or "").strip()
    access_code = (request.form.get("printer_access_lan") or "").strip()
    if not access_code:
        access_code = app_config.PRINTER_ACCESS_LAN or ""

    if not printer_ip:
        flash("Bitte die lokale IP-Adresse des Druckers eintragen.", "danger")
        return redirect(url_for("settings.index"))
    if not printer_id:
        flash("Bitte die Seriennummer des Druckers eintragen.", "danger")
        return redirect(url_for("settings.index"))
    if not access_code:
        flash("Bitte den lokalen Printer Access Code eintragen.", "danger")
        return redirect(url_for("settings.index"))

    cost_fields = {
        "electricity_price_eur_per_kwh": request.form.get("electricity_price_eur_per_kwh"),
        "printer_power_w": request.form.get("printer_power_w"),
        "printer_purchase_price_eur": request.form.get("printer_purchase_price_eur"),
        "printer_lifetime_hours": request.form.get("printer_lifetime_hours"),
    }
    cost_settings = {}
    try:
        for key, raw_value in cost_fields.items():
            value = float(str(raw_value or "").replace(",", "."))
            if value < 0 or (key == "printer_lifetime_hours" and value <= 0):
                raise ValueError
            cost_settings[key] = value
    except (TypeError, ValueError):
        flash("Bitte gültige, nicht negative Kostenwerte und eine Lebensdauer größer 0 eintragen.", "danger")
        return redirect(url_for("settings.index"))

    values = {
        "PRINTER_IP": printer_ip,
        "PRINTER_ID": printer_id,
        "PRINTER_NAME": printer_name,
        "PRINTER_ACCESS_LAN": access_code,
    }
    _write_config_env(values)
    for key, value in values.items():
        setattr(app_config, key, value)
    app_config.PRINTER_CODE = access_code
    app_database.set_runtime_metadata("settings", "printer_costs", cost_settings)

    connected = mqtt_bambulab.reconfigure_printer(
        printer_id,
        access_code,
        printer_ip,
        wait_seconds=12,
    )
    if connected:
        flash("Lokale Konfiguration gespeichert und MQTT verbunden.", "success")
    else:
        flash("Lokale Konfiguration gespeichert, MQTT konnte aber nicht verbunden werden.", "danger")
    return redirect(url_for("settings.index"))
