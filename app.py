import json
import math
import os
import traceback
import uuid
from collections import Counter

from flask import Flask, request, render_template, redirect, url_for

from config import (
    BASE_URL,
    AUTO_SPEND,
    EXTERNAL_SPOOL_AMS_ID,
    EXTERNAL_SPOOL_ID,
    PRINTER_NAME,
    CLEAR_ASSIGNMENT_WHEN_EMPTY,
)
from filament import generate_filament_brand_code, generate_filament_temperatures
from frontend_utils import color_is_dark
from messages import AMS_FILAMENT_SETTING
import mqtt_bambulab
import print_history as print_history_service
import inventory_database
import inventory_repository as spool_data
import inventory_service
import nfc_pending_repository
from inventory_service import augment_tray_data, trayUid, normalize_color_hex
from logger import log

READ_ONLY_MODE = os.getenv("M0BAMBOSPOOL_LIVE_READONLY") == "1"

LAYER_TRACKING_STATUS_DISPLAY = {
    "RUNNING": ("Printing", "warning"),
    "COMPLETED": ("Finished", "success"),
    "ABORTED": ("Cancelled", "danger"),
    "FAILED": ("Failed", "danger"),
}

mqtt_bambulab.init_mqtt()

app = Flask(__name__)

@app.context_processor
def fronted_utilities():
  printer_model = mqtt_bambulab.getPrinterModel() or {}
  ams_models_by_id = mqtt_bambulab.getDetectedAmsModelsById()

  return dict(
    AUTO_SPEND=AUTO_SPEND,
    AMS_MODELS_BY_ID=ams_models_by_id,
    color_is_dark=color_is_dark,
    BASE_URL=BASE_URL,
    EXTERNAL_SPOOL_AMS_ID=EXTERNAL_SPOOL_AMS_ID,
    EXTERNAL_SPOOL_ID=EXTERNAL_SPOOL_ID,
    PRINTER_MODEL=printer_model,
    PRINTER_NAME=PRINTER_NAME,
  )


def build_ams_labels(ams_data):
  models_by_id = mqtt_bambulab.getDetectedAmsModelsById()
  base_labels = []
  for ams in ams_data:
    ams_id = ams.get("id")
    key = str(ams_id)
    base_name = models_by_id.get(key) or models_by_id.get(ams_id) or "AMS"
    base_labels.append(base_name)

  totals = Counter(base_labels)
  takt = Counter()
  labels = {}

  for ams, base_name in zip(ams_data, base_labels):
    takt[base_name] += 1
    suffix = f" {takt[base_name]}" if totals[base_name] > 1 else ""
    label = f"{base_name}{suffix}"
    ams_id = ams.get("id")
    labels[str(ams_id)] = label
    labels[ams_id] = label

  return labels


def _augment_tray(spool_list, tray_data, ams_id, tray_id):
  augment_tray_data(spool_list, tray_data, ams_id, tray_id)
  if tray_data.get("unmapped_bambu_tag"):
    inventory_service.clear_active_spool_for_tray(ams_id, tray_id)
    augment_tray_data(spool_list, tray_data, ams_id, tray_id)
  empty_condition = (
      CLEAR_ASSIGNMENT_WHEN_EMPTY
      and not tray_data.get("spool_material")
      and not tray_data.get("unmapped_bambu_tag")
  )
  if empty_condition:
    inventory_service.clear_active_spool_for_tray(ams_id, tray_id)
    mqtt_bambulab.clear_ams_tray_assignment(ams_id, tray_id)


def _select_spool_color_hex(spool_data):
  filament = spool_data.get("filament", {})
  multi = filament.get("multi_color_hexes")
  candidate = ""

  if multi:
    if isinstance(multi, (list, tuple)) and multi:
      candidate = multi[0]
    elif isinstance(multi, str):
      candidate = multi.split(",")[0]

  if not candidate:
    candidate = filament.get("color_hex") or ""

  return normalize_color_hex(candidate)

@app.route("/issue")
def issue():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
    
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  if not all([ams_id, tray_id]):
    return render_template('error.html', exception="Missing AMS ID, or Tray ID.")

  fix_ams = None
  tray_data = None

  spool_list = mqtt_bambulab.fetchSpools()
  last_ams_config = mqtt_bambulab.getLastAMSConfig()
  if ams_id == EXTERNAL_SPOOL_AMS_ID:
    fix_ams = last_ams_config.get("vt_tray", {})
    tray_data = fix_ams
  else:
    for ams in last_ams_config.get("ams", []):
      if str(ams["id"]) == str(ams_id):
        fix_ams = ams
        break

  if fix_ams:
    for tray in fix_ams.get("tray", []):
      if str(tray["id"]) == str(tray_id):
        tray_data = tray
        break

  active_spool = None
  for spool in spool_list:
    if spool.get("extra") and spool["extra"].get("active_tray") and spool["extra"].get("active_tray") == json.dumps(trayUid(ams_id, tray_id)):
      active_spool = spool
      break

  if tray_data:
    _augment_tray(spool_list, tray_data, ams_id, tray_id)

  #TODO: Determine issue
  #New bambulab spool
  # Tray empty, but the local catalog has a record.
  #Extra tag mismatch?
  #COLor/type mismatch

  return render_template('issue.html', fix_ams=fix_ams, tray_data=tray_data, ams_id=ams_id, tray_id=tray_id, active_spool=active_spool)

@app.route("/fill")
def fill():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
    
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  if not all([ams_id, tray_id]):
    return render_template('error.html', exception="Missing AMS ID, or Tray ID.")

  spool_id = request.args.get("spool_id")
  if spool_id:
    if READ_ONLY_MODE:
      return render_template('error.html', exception="Live read-only mode: assigning spools to trays is disabled.")

    spool_record = spool_data.get_spool(spool_id)
    mqtt_bambulab.setActiveTray(spool_id, spool_record["extra"], ams_id, tray_id)
    setActiveSpool(ams_id, tray_id, spool_record)
    return redirect(url_for(
      'home',
      success_message=(
        f"Spool ID {spool_id} assigned in m0BamboSpool to AMS {ams_id}, Tray {tray_id}. "
        "AMS material update sent to printer; verify the tray status below."
      )
    ))
  else:
    # AMS filling is handled in the local inventory editor.  The editor gets
    # the target tray as context and exposes only its dedicated assignment
    # action there.
    return redirect(url_for('inventory', fill=1, ams=ams_id, tray=tray_id))

@app.route("/assign_bambu_spool")
def assign_bambu_spool():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")

  bambu_tag = request.args.get("tag")
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  spool_id = request.args.get("spool_id")

  if not all([bambu_tag, ams_id, tray_id]):
    return render_template('error.html', exception="Missing AMS ID, Tray ID, or Bambu spool tag.")

  if bambu_tag == "00000000000000000000000000000000":
    return render_template('error.html', exception="No Bambu spool was detected in this tray.")

  if spool_id:
    if READ_ONLY_MODE:
      return render_template('error.html', exception="Live read-only mode: linking Bambu spools is disabled.")

    try:
      inventory_database.update_spool_fields(spool_id, {"tag": bambu_tag})
      spool_record = inventory_database.get_spool(spool_id)
      mqtt_bambulab.setActiveTray(
        spool_id,
        spool_record.get("extra") or {},
        ams_id,
        tray_id,
        source="nfc",
        nfc_uid=bambu_tag,
      )
      setActiveSpool(ams_id, tray_id, spool_record)
      nfc_pending_repository.remove_pending_tag(bambu_tag)
    except Exception as exc:
      traceback.print_exc()
      return render_template('error.html', exception=str(exc))

    return redirect(url_for('home', success_message=f"Bambu-Spule {spool_id} lokal mit AMS {ams_id}, Fach {int(tray_id) + 1} verknüpft."))

  spools = mqtt_bambulab.fetchSpools()
  materials = extract_materials(spools)
  selected_materials = []

  try:
    last_ams_config = mqtt_bambulab.getLastAMSConfig()
    default_material = None

    if ams_id == EXTERNAL_SPOOL_AMS_ID:
      default_material = last_ams_config.get("vt_tray", {}).get("tray_type")
    else:
      for ams in last_ams_config.get("ams", []):
        if str(ams.get("id")) != str(ams_id):
          continue

        for tray in ams.get("tray", []):
          if str(tray.get("id")) == str(tray_id):
            default_material = tray.get("tray_type")
            break

    if default_material and default_material in materials:
      selected_materials.append(default_material)
  except Exception:
    pass

  return render_template(
    'assign_bambu_spool.html',
    spools=spools,
    ams_id=ams_id,
    tray_id=tray_id,
    bambu_tag=bambu_tag,
    materials=materials,
    selected_materials=selected_materials,
  )

@app.route("/spool_info")
def spool_info():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")

  try:
    tag_id = request.args.get("tag_id")
    spool_id = request.args.get("spool_id")
    last_ams_config = mqtt_bambulab.getLastAMSConfig()
    ams_data = last_ams_config.get("ams", [])
    vt_tray_data = last_ams_config.get("vt_tray", {})
    spool_list = mqtt_bambulab.fetchSpools()

    issue = False
    #TODO: Fix issue when external spool info is reset via bambulab interface
    _augment_tray(spool_list, vt_tray_data, EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID)
    issue |= vt_tray_data.get("issue", False)

    for ams in ams_data:
      for tray in ams["tray"]:
        _augment_tray(spool_list, tray, ams["id"], tray["id"])
        issue |= tray.get("issue", False)

    if not tag_id and not spool_id:
      return render_template('error.html', exception="TAG ID or spool_id is required as a query parameter (e.g., ?tag_id=RFID123 or ?spool_id=1)")

    spools = mqtt_bambulab.fetchSpools()
    current_spool = None

    spool_id_int = None
    if spool_id is not None:
      try:
        spool_id_int = int(spool_id)
      except ValueError:
        return render_template('error.html', exception="Invalid spool_id provided")

    for spool in spools:
      if spool_id_int is not None and spool['id'] == spool_id_int:
        current_spool = spool
        if not tag_id:
          tag_value = spool.get("extra", {}).get("tag")
          if tag_value:
            tag_id = json.loads(tag_value)
        break

      if not tag_id:
        continue

      if not spool.get("extra", {}).get("tag"):
        continue

      tag = json.loads(spool["extra"]["tag"])
      if tag != tag_id:
        continue

      current_spool = spool
      break

    if current_spool:
      ams_labels = build_ams_labels(ams_data)
      return render_template('spool_info.html', tag_id=tag_id, current_spool=current_spool, ams_data=ams_data, vt_tray_data=vt_tray_data, issue=issue, ams_labels=ams_labels)
    else:
      return render_template('error.html', exception="Spool not found")
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))


@app.route("/spool/info/<int:spool_id>")
@app.route("/spool/show/<int:spool_id>")
def compatible_spool_info(spool_id):
  query_params = {"spool_id": spool_id}
  tag_id = request.args.get("tag_id")

  if tag_id:
    query_params["tag_id"] = tag_id

  return redirect(url_for('spool_info', **query_params))


@app.post("/tray_clear")
def tray_clear():
  # Remove only the local assignment for a tray.
  #
  #   This deliberately does not send AMS_FILAMENT_SETTING to the printer, so the
  #   material/color configured in Bambu remains untouched.
  ams_id = request.form.get("ams")
  tray_id = request.form.get("tray")

  if ams_id is None or tray_id is None:
    return render_template('error.html', exception="Missing AMS ID or Tray ID.")

  if READ_ONLY_MODE:
    return render_template('error.html', exception="Live read-only mode: clearing tray assignments is disabled.")

  try:
    inventory_service.clear_active_spool_for_tray(ams_id, tray_id)
    return redirect(url_for(
      'home',
      success_message=f"Tray assignment cleared for AMS {ams_id}, Tray {int(tray_id) + 1}."
    ))
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))


@app.route("/tray_load")
def tray_load():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
  
  tag_id = request.args.get("tag_id")
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  spool_id = request.args.get("spool_id")

  if not all([ams_id, tray_id, spool_id]):
    return render_template('error.html', exception="Missing AMS ID, or Tray ID or spool_id.")

  if READ_ONLY_MODE:
    return render_template('error.html', exception="Live read-only mode: assigning spools to trays is disabled.")

  try:
    # Update the local catalog with the selected tray.
    spool_record = spool_data.get_spool(spool_id)
    mqtt_bambulab.setActiveTray(spool_id, spool_record["extra"], ams_id, tray_id)
    setActiveSpool(ams_id, tray_id, spool_record)

    return redirect(url_for('home', success_message=f"Spule {spool_id} mit NFC-Tag {tag_id} dem AMS {ams_id}, Fach {int(tray_id) + 1} zugeordnet."))
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

def _resolve_bambu_profile_ids(spool_data):
  # Resolve Bambu filament/profile IDs for AMS writes.
  #
  #   Preference:
  #   1. Valid IDs already stored with the filament metadata.
  #   2. Known Bambu profile mapping derived from the printer/profile data.
  #
  #   Legacy numeric placeholders such as "12345" are deliberately ignored.
  filament = spool_data.get("filament", {}) or {}
  extra = filament.get("extra", {}) or {}
  vendor = ((filament.get("vendor") or {}).get("name") or "").strip().upper()
  material = str(filament.get("material") or "").strip().upper().replace(" ", "")

  def clean(value):
    value = str(value or "").strip().strip('"')
    if not value or value.isdigit():
      return ""
    return value

  filament_id = clean(extra.get("filament_id"))
  setting_id = clean(extra.get("setting_id"))

  if filament_id and setting_id:
    return filament_id, setting_id, "local"

  # Bambu profile observed for SUNLU PLA+ 2.0 in the printer/profile data.
  # Bambu accepts the base tray_type PLA; these IDs retain the concrete PLA+ profile.
  known_profiles = {
    ("SUNLU", "PLA+"): ("GFL99", "GFSL99"),
  }

  resolved = known_profiles.get((vendor, material))
  if resolved:
    return resolved[0], resolved[1], "bambu-profile-map"

  return filament_id, setting_id, "unresolved"


def _parse_nozzle_temperature_range(value):
  # Local inventory values may be JSON-encoded once or twice.
  current = value
  for _ in range(2):
    if not isinstance(current, str):
      break
    text = current.strip()
    try:
      decoded = json.loads(text)
    except (TypeError, ValueError):
      break
    if decoded == current:
      break
    current = decoded
  if isinstance(current, (list, tuple)) and len(current) >= 2:
    return int(float(current[0])), int(float(current[1]))
  text = str(current).strip().strip('"').strip("[]")
  parts = [part.strip() for part in text.split(",")]
  if len(parts) < 2:
    raise ValueError(f"Ungültiger Düsentemperaturbereich: {value!r}")
  return int(float(parts[0])), int(float(parts[1]))


def setActiveSpool(ams_id, tray_id, spool_data):
  if READ_ONLY_MODE:
    return None

  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
  
  import copy
  ams_message = copy.deepcopy(AMS_FILAMENT_SETTING)
  ams_message["print"]["sequence_id"] = 0
  ams_message["print"]["ams_id"] = int(ams_id)
  ams_message["print"]["tray_id"] = int(tray_id)
  color_hex = _select_spool_color_hex(spool_data)
  if color_hex:
    ams_message["print"]["tray_color"] = color_hex.upper() + "FF"
  else:
    ams_message["print"]["tray_color"] = ""
      
  if "nozzle_temperature" in spool_data["filament"]["extra"]:
    nozzle_min, nozzle_max = _parse_nozzle_temperature_range(
      spool_data["filament"]["extra"]["nozzle_temperature"]
    )
    ams_message["print"]["nozzle_temp_min"] = nozzle_min
    ams_message["print"]["nozzle_temp_max"] = nozzle_max
  else:
    nozzle_temperature_range_obj = generate_filament_temperatures(spool_data["filament"]["material"],
                                                                  spool_data["filament"]["vendor"]["name"])
    ams_message["print"]["nozzle_temp_min"] = int(nozzle_temperature_range_obj["filament_min_temp"])
    ams_message["print"]["nozzle_temp_max"] = int(nozzle_temperature_range_obj["filament_max_temp"])

  # Bambu rejects PLA+ as tray_type on affected AMS/firmware versions.
  # Keep PLA+ in the local catalog, but write Bambu's accepted base material "PLA".
  # The concrete PLA+ profile remains identified by filament_id/setting_id below.
  spool_material = str(spool_data["filament"]["material"] or "").strip()
  if spool_material.upper().replace(" ", "") == "PLA+":
    ams_message["print"]["tray_type"] = "PLA"
  else:
    ams_message["print"]["tray_type"] = spool_material

  filament_brand_code = {}
  filament_brand_code["brand_code"] = str(
    spool_data["filament"].get("extra", {}).get("filament_id") or ""
  ).strip().strip('"')
  filament_brand_code["sub_brand_code"] = ""

  if filament_brand_code["brand_code"] == "":
    filament_brand_code = generate_filament_brand_code(spool_data["filament"]["material"],
                                                      spool_data["filament"]["vendor"]["name"],
                                                      spool_data["filament"]["extra"].get("type", ""))
    
  # Bambu expects the real filament/profile identifiers when available.
  # Local custom fields may contain them with JSON-style quotes, hence strip().
  filament_id, setting_id, profile_source = _resolve_bambu_profile_ids(spool_data)

  material_key = str(spool_data["filament"].get("material") or "").strip().upper().replace(" ", "")
  if material_key == "PLA+":
    if not filament_id or not setting_id:
      raise ValueError(
        "No usable Bambu PLA+ profile could be resolved for this filament: "
        f"vendor={spool_data['filament'].get('vendor', {}).get('name')!r}, "
        f"material={spool_data['filament'].get('material')!r}."
      )
    ams_message["print"]["tray_info_idx"] = filament_id
    ams_message["print"]["setting_id"] = setting_id
  else:
    raw_filament_id = str(spool_data["filament"].get("extra", {}).get("filament_id", "")).strip().strip('"')
    raw_setting_id = str(spool_data["filament"].get("extra", {}).get("setting_id", "")).strip().strip('"')
    ams_message["print"]["tray_info_idx"] = raw_filament_id or filament_brand_code["brand_code"]
    ams_message["print"]["setting_id"] = raw_setting_id
    profile_source = "legacy-generic"

  # TODO: test sub_brand_code
  # ams_message["print"]["tray_sub_brands"] = filament_brand_code["sub_brand_code"]
  ams_message["print"]["tray_sub_brands"] = ""

  print(
    "[m0BamboSpool] AMS Fill v2: "
    f"ams={ams_id} tray={tray_id} "
    f"tray_type={ams_message['print'].get('tray_type')!r} "
    f"tray_info_idx={ams_message['print'].get('tray_info_idx')!r} "
    f"setting_id={ams_message['print'].get('setting_id')!r} "
    f"profile_source={profile_source!r}"
  )
  log(ams_message)
  publish_result = mqtt_bambulab.publish(mqtt_bambulab.getMqttClient(), ams_message)
  if publish_result is False:
    raise RuntimeError("MQTT publish of ams_filament_setting failed.")
  return ams_message

@app.route("/")
def home():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")

  try:
    last_ams_config = mqtt_bambulab.getLastAMSConfig()
    ams_data = last_ams_config.get("ams", [])
    vt_tray_data = last_ams_config.get("vt_tray", {})
    spool_list = mqtt_bambulab.fetchSpools()
    success_message = request.args.get("success_message")
    
    issue = False
    #TODO: Fix issue when external spool info is reset via bambulab interface
    _augment_tray(spool_list, vt_tray_data, EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID)
    issue |= vt_tray_data["issue"]

    for ams in ams_data:
      for tray in ams["tray"]:
        _augment_tray(spool_list, tray, ams["id"], tray["id"])
        issue |= tray["issue"]

    ams_labels = build_ams_labels(ams_data)
    return render_template('index.html', success_message=success_message, ams_data=ams_data, vt_tray_data=vt_tray_data, issue=issue, ams_labels=ams_labels)
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

def sort_spools(spools):
  def condition(item):
    # Ensure the item has an "extra" key and is a dictionary
    if not isinstance(item, dict) or "extra" not in item or not isinstance(item["extra"], dict):
      return False

    # Check the specified condition
    return item["extra"].get("tag") or item["extra"].get("tag") == ""

  # Sort with the custom condition: False values come first
  return sorted(spools, key=lambda spool: bool(condition(spool)))


def extract_materials(spools):
  materials = set()

  for spool in spools:
    filament = None

    if isinstance(spool, dict):
      filament = spool.get("filament")
    else:
      filament = getattr(spool, "filament", None)

    if isinstance(filament, dict):
      material = filament.get("material")
    else:
      material = getattr(filament, "material", None)

    if material:
      materials.add(material)

  return sorted(materials)

@app.route("/write_tag")
def write_tag():
  try:
    spool_id = request.args.get("spool_id")

    if not spool_id:
      return render_template('error.html', exception="spool ID is required as a query parameter (e.g., ?spool_id=1)")

    myuuid = str(uuid.uuid4())

    inventory_database.update_spool_fields(spool_id, {"tag": myuuid})
    return render_template('write_tag.html', myuuid=myuuid)
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

@app.route('/health', methods=['GET'])
def health():
  return "OK", 200

@app.route("/print_history")
def print_history():
  inventory_settings = inventory_service.getSettings()

  try:
    def _to_float(value):
      try:
        return float(value)
      except (TypeError, ValueError):
        return None

    def _to_int(value):
      try:
        return int(value)
      except (TypeError, ValueError):
        return None

    page = max(int(request.args.get("page", 1)), 1)
  except ValueError:
    page = 1
  per_page = 50
  offset = max((page - 1) * per_page, 0)

  ams_slot = request.args.get("ams_slot")
  print_id = request.args.get("print_id")
  spool_id = request.args.get("spool_id")
  old_spool_id = request.args.get("old_spool_id")

  if not old_spool_id:
    old_spool_id = -1

  if READ_ONLY_MODE and all([ams_slot, print_id, spool_id]):
    return render_template('error.html', exception="Live read-only mode: updating print-to-spool assignments is disabled.")

  def _consume_for_spool(spool_id_value, grams_value=None, length_value=None):
    if spool_id_value is None:
      return
    if length_value is not None:
      spool_data.record_consumption(spool_id_value, length_mm=length_value)
    elif grams_value is not None:
      spool_data.record_consumption(spool_id_value, weight_grams=grams_value)

  if all([ams_slot, print_id, spool_id]):
    filament = print_history_service.get_filament_for_slot(print_id, ams_slot)
    print_history_service.update_filament_spool(print_id, ams_slot, spool_id)

    if(filament["spool_id"] != int(spool_id) and (not old_spool_id or (old_spool_id and filament["spool_id"] == int(old_spool_id)))):
      grams_used = _to_float(filament.get("grams_used"))
      length_used = _to_float(filament.get("length_used"))
      use_length = length_used is not None and length_used > 0

      if old_spool_id and int(old_spool_id) != -1:
        _consume_for_spool(
            old_spool_id,
            grams_value=-(grams_used or 0),
            length_value=-(length_used or 0) if use_length else None,
        )

      _consume_for_spool(
          spool_id,
          grams_value=grams_used,
          length_value=length_used if use_length else None,
      )

  show_deleted = request.args.get("show_deleted") == "1"
  prints, total_prints = print_history_service.get_prints_with_filament(limit=per_page, offset=offset, include_deleted=show_deleted)
  layer_tracking_map = print_history_service.get_layer_tracking_for_prints([print["id"] for print in prints])

  # Historical print assignments are resolved from our local catalog.
  spool_list = inventory_database.list_spools(include_archived=True)

  # The AMS slot is authoritative for historical rows that were recorded
  # before the spool identity was resolved.  Build a current slot index so a
  # transient ``spool_id = NULL`` row cannot appear as an extra filament in
  # history when the slot is already assigned locally.
  active_spools_by_slot = {}
  for assigned_spool in spool_list:
    active_tray = assigned_spool.get("active_tray")
    if not active_tray:
      active_tray = (assigned_spool.get("extra") or {}).get("active_tray")
    try:
      active_text = str(active_tray or "").strip().strip('"')
      active_parts = active_text.rsplit("_", 2)
      if len(active_parts) == 3:
        assigned_slot = int(active_parts[1]) * 4 + int(active_parts[2])
        if 0 <= assigned_slot < 16:
          existing = active_spools_by_slot.get(assigned_slot)
          if existing is None:
            active_spools_by_slot[assigned_slot] = assigned_spool
          else:
            # Never guess when the local catalog contains a conflicting
            # assignment.  The AMS refresh must resolve this before a row can
            # be rebound safely.
            active_spools_by_slot[assigned_slot] = False
            log(
              f"[History] Mehrdeutige lokale Fachzuordnung A{assigned_slot + 1}: "
              f"Spulen {existing.get('id')} und {assigned_spool.get('id')}"
            )
    except (TypeError, ValueError):
      continue

  for print in prints:
    tracking_row = layer_tracking_map.get(print["id"])
    if tracking_row:
      status_key = (tracking_row.get("status") or "").upper()
      status_label, status_badge = LAYER_TRACKING_STATUS_DISPLAY.get(
          status_key, ("Unbekannt", "secondary")
      )
      total_layers = _to_int(tracking_row.get("total_layers"))
      layers_printed = _to_int(tracking_row.get("layers_printed")) or 0
      billed = _to_float(tracking_row.get("filament_grams_billed"))
      total_grams = _to_float(tracking_row.get("filament_grams_total"))

      progress = None
      if total_layers:
        progress = min(100, int(layers_printed / total_layers * 100))

      print["layer_tracking"] = {
        "status_label": status_label,
        "status_badge": status_badge,
        "layers_printed": layers_printed,
        "total_layers": total_layers,
        "progress_percent": progress,
        "filament_grams_billed": billed,
        "filament_grams_total": total_grams,
        "predicted_end_time": tracking_row.get("predicted_end_time"),
        "actual_end_time": tracking_row.get("actual_end_time"),
      }
      print["is_running"] = status_key == "RUNNING"
    else:
      print["layer_tracking"] = None
      print["is_running"] = False

    filament_usage_data = json.loads(print["filament_info"])
    # Keep the historical filament rows in physical AMS order (A1, A2, ...).
    # The JSON is written in filament-index order and is not guaranteed to
    # match the tray order shown to the user.
    def _history_slot_order(entry):
      physical = entry.get("physical_ams_slot")
      logical = entry.get("ams_slot")
      try:
        return int(physical if physical is not None else logical)
      except (TypeError, ValueError):
        return 999

    filament_usage_data.sort(key=_history_slot_order)
    filament_usage_sum = sum(
        _to_float(f.get("grams_used")) or 0 for f in filament_usage_data
    )
    tracking_total = (
        _to_float(print["layer_tracking"]["filament_grams_total"])
        if print["layer_tracking"]
        else None
    )
    print["display_filament_total"] = tracking_total if tracking_total is not None else filament_usage_sum

    print["filament_usage"] = filament_usage_data
    print["total_cost"] = 0

    # Physical slots already represented by a resolved spool in this print.
    # This prevents an unresolved logical filament from being attached to the
    # same AMS slot again merely because its logical index happens to match.
    occupied_history_slots = set()
    logical_history_slots = set()
    resolved_history_spools = set()
    for existing_filament in print["filament_usage"]:
      try:
        if existing_filament.get("ams_slot") is not None:
          logical_history_slots.add(int(existing_filament.get("ams_slot")))
      except (TypeError, ValueError):
        pass
      existing_id = existing_filament.get("spool_id")
      if existing_id is None:
        continue
      resolved_history_spools.add(existing_id)
      existing_spool = next((item for item in spool_list if item.get("id") == existing_id), None)
      if existing_spool is None:
        continue
      existing_active = existing_spool.get("active_tray") or (existing_spool.get("extra") or {}).get("active_tray")
      try:
        existing_parts = str(existing_active or "").strip().strip('"').rsplit("_", 2)
        if len(existing_parts) == 3:
          existing_slot = int(existing_parts[1]) * 4 + int(existing_parts[2])
          if 0 <= existing_slot < 16:
            occupied_history_slots.add(existing_slot)
      except (TypeError, ValueError):
        continue

    for filament in print["filament_usage"]:
      # A tracker row can be emitted before the RFID/AMS identity is known.
      # Resolve that row from its physical (or logical fallback) AMS slot;
      # never infer the spool from colour.
      if filament.get("spool_id") is None:
        slot_value = filament.get("physical_ams_slot")
        if slot_value is None:
          slot_value = filament.get("ams_slot")
        try:
          resolved_slot = int(slot_value)
        except (TypeError, ValueError):
          resolved_slot = None
        resolved_spool = active_spools_by_slot.get(resolved_slot)
        if resolved_spool not in (None, False) and resolved_slot in occupied_history_slots:
          # Prefer an otherwise unrepresented active AMS slot. This is still
          # purely AMS-based and avoids rendering a second copy of the spool
          # already assigned to the candidate slot.
          free_slots = sorted(
            slot for slot, candidate in active_spools_by_slot.items()
            if candidate not in (None, False) and slot not in occupied_history_slots
          )
          if free_slots:
            # If a logical filament still has no resolved spool, use the
            # remaining active AMS slot even for a completed/aborted print.
            # Once all logical filaments already have a spool, the row is a
            # stale provisional duplicate and must not invent Orange/A2.
            if len(resolved_history_spools) < len(logical_history_slots):
              resolved_slot = free_slots[0]
              resolved_spool = active_spools_by_slot[resolved_slot]
            else:
              # A completed/aborted job may contain a stale provisional row
              # for a logical filament that was never used. Do not invent an
              # unrelated AMS spool (for example Orange in A2) for history.
              filament["_drop_unresolved_duplicate"] = True
        if resolved_spool not in (None, False):
          filament["spool_id"] = resolved_spool.get("id")
          filament["physical_ams_slot"] = resolved_slot
          occupied_history_slots.add(resolved_slot)
          resolved_history_spools.add(filament.get("spool_id"))
      if filament["spool_id"]:
        for spool in spool_list:
          if spool['id'] == filament["spool_id"]:
            filament["spool"] =  spool
            # The local AMS assignment is authoritative.  The initial 3MF
            # mapping can be provisional (especially after reconnect/resume),
            # so resolve the displayed tray from the spool's current AMS
            # assignment.  This is an AMS lookup, never a colour lookup.
            active_tray = spool.get("active_tray")
            if not active_tray:
              active_tray = (spool.get("extra") or {}).get("active_tray")
            try:
              active_text = str(active_tray or "").strip().strip('"')
              active_parts = active_text.rsplit("_", 2)
              if len(active_parts) == 3:
                physical_slot = int(active_parts[1]) * 4 + int(active_parts[2])
                if 0 <= physical_slot < 16:
                  filament["physical_ams_slot"] = physical_slot
            except (TypeError, ValueError):
              pass
            initial_weight = _to_float(spool.get("initial_weight")) or 0.0
            price = _to_float(spool.get("price")) or 0.0
            cost_per_gram = _to_float(spool.get("cost_per_gram"))
            if cost_per_gram is None:
              cost_per_gram = price / initial_weight if initial_weight > 0 else 0.0
            filament["cost"] = (_to_float(filament.get("grams_used")) or 0.0) * cost_per_gram
            print["total_cost"] += filament["cost"]
            break

    # AMS tray order is the stable history order.  This is deliberately done
    # after enrichment because legacy rows may receive a fallback slot above.
    print["filament_usage"].sort(key=_history_slot_order)

    # One physical spool can be represented by multiple logical 3MF
    # filament entries. Show it once in history and aggregate its usage.
    merged_usage = []
    merged_by_slot = {}
    for filament in print["filament_usage"]:
      if filament.get("_drop_unresolved_duplicate"):
        continue
      # Keep every logical G-Code filament visible.  A reconnect can create
      # duplicate rows for the same logical index, but two different logical
      # filament indices must never be collapsed merely because the printer
      # temporarily mapped them to the same physical spool/tray.
      key = (
        filament.get("spool_id"),
        filament.get("physical_ams_slot"),
        filament.get("ams_slot"),
      )
      if filament.get("spool_id") is not None and key in merged_by_slot:
        target = merged_by_slot[key]
        for field in ("grams_used", "length_used", "calculated_length", "consumed_length"):
          current = _to_float(target.get(field))
          added = _to_float(filament.get(field))
          if added is not None:
            target[field] = (current or 0.0) + added
        # Expected values describe the logical G-Code filament and must not
        # be doubled when a reconnect created a duplicate tracker row.
        for field in ("estimated_grams", "estimated_length"):
          current = _to_float(target.get(field))
          added = _to_float(filament.get(field))
          if current is None:
            target[field] = added
          elif added is not None:
            target[field] = max(current, added)
        continue
      merged_by_slot[key] = filament
      merged_usage.append(filament)
    print["filament_usage"] = merged_usage
  
  total_pages = max(1, math.ceil(total_prints / per_page))

  return render_template(
    'print_history.html',
    prints=prints,
    currencysymbol=inventory_settings["currency_symbol"],
    page=page,
    total_pages=total_pages,
    per_page=per_page,
    show_deleted=show_deleted,
    has_deleted_prints=print_history_service.has_deleted_prints(),
  )


@app.post("/print_history/<int:print_id>/delete")
def delete_print_history_entry(print_id):
  print_history_service.soft_delete_print(print_id)
  return redirect(url_for("print_history"))


@app.post("/print_history/<int:print_id>/restore")
def restore_print_history_entry(print_id):
  print_history_service.restore_print(print_id)
  return redirect(url_for("print_history", show_deleted=1))

@app.route("/print_select_spool")
def print_select_spool():

  try:
    ams_slot = request.args.get("ams_slot")
    print_id = request.args.get("print_id")
    old_spool_id = request.args.get("old_spool_id")
    
    change_spool = request.args.get("change_spool", "false").lower() == "true"
    
    if not old_spool_id:
      old_spool_id = -1

    if not all([ams_slot, print_id]):
      return render_template('error.html', exception="Missing spool ID or print ID.")

    spools = mqtt_bambulab.fetchSpools()

    materials = extract_materials(spools)
    selected_materials = []

    filament = print_history_service.get_filament_for_slot(print_id, ams_slot)

    try:
      filament_material = filament["filament_type"] if filament else None

      if filament_material and filament_material in materials:
        selected_materials.append(filament_material)
    except Exception:
      pass

    return render_template(
      'print_select_spool.html',
      spools=spools,
      ams_slot=ams_slot,
      print_id=print_id,
      old_spool_id=old_spool_id,
      change_spool=change_spool,
      materials=materials,
      selected_materials=selected_materials,
    )
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

# Register REST API blueprint
from api_routes import api_bp
app.register_blueprint(api_bp)
