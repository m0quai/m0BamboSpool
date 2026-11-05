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
    # Failed printer outcomes are shown in the history together with aborted
    # jobs.  Both mean that the job did not finish successfully and should be
    # handled consistently by the user-facing status filter.
    "FAILED": ("Cancelled", "danger"),
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
# Integrated m0BamboSpool fork extensions.
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

import sys

# The fork extensions are integrated into this module; no upstream app import remains.
_m0bambospool_app_module = sys.modules[__name__]

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
    tray_data["ams_loading_state"] = "loading" if tray_data["ams_loading"] else ""
    tray_data["ams_activity_state"] = tray_data["ams_loading_state"]
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
            tray_data["ams_loading_state"] = "unloading" if tray_status in {"unloading", "unload"} else ("loading" if tray_data["ams_loading"] or tray_status in {"loading", "load", "changing"} else "")
            tray_data["ams_loading"] = (
                mqtt_bambulab.is_ams_tray_operation_pending(ams_id, tray_id)
                or tray_status in {"loading", "unloading", "changing", "load", "unload"}
            )
            # During a print the printer reports filament changes primarily
            # through stg_cur/mc_print_sub_stage and tray_tar rather than a
            # tray_status field. Reflect that live transition on the AMS card.
            print_state = (getattr(mqtt_bambulab, "PRINTER_STATE", {}) or {}).get("print", {}) or {}
            try:
                target_tray = str((print_state.get("ams") or {}).get("tray_tar"))
                physical_tray = int(ams_id) * 4 + int(tray_id)
                stage = int(print_state.get("stg_cur", -1))
                sub_stage = int(print_state.get("mc_print_sub_stage", -1))
            except (TypeError, ValueError):
                target_tray, physical_tray, stage, sub_stage = "", -1, -1, -1
            transition = stage == 4 or sub_stage in {2, 4}
            if transition and target_tray == str(physical_tray):
                tray_data["ams_loading"] = True
                tray_data["ams_loading_state"] = "unloading" if target_tray in {"254", "255"} else "loading"
                tray_data["ams_activity_state"] = tray_data["ams_loading_state"]
            if target_tray in {"254", "255"}:
                previous_print = (getattr(mqtt_bambulab, "PRINTER_STATE_LAST", {}) or {}).get("print", {}) or {}
                previous_target = str((previous_print.get("ams") or {}).get("tray_tar") or "")
                if previous_target == str(physical_tray) and transition:
                    tray_data["ams_loading"] = True
                    tray_data["ams_loading_state"] = "unloading"
                    tray_data["ams_activity_state"] = "unloading"
            if not tray_data["ams_activity_state"] and str(print_state.get("gcode_state") or "").upper() == "RUNNING":
                try:
                    active_mapping = mqtt_bambulab._active_tray_mapping(print_state)
                    live_tray_value = int((print_state.get("ams") or {}).get("tray_tar"))
                    if 0 <= live_tray_value < 16:
                        active_mapping = live_tray_value
                    if int(active_mapping) == physical_tray:
                        tray_data["ams_activity_state"] = "printing"
                except (TypeError, ValueError):
                    pass
            if tray_data["ams_loading"] and not tray_data["ams_loading_state"]:
                tray_data["ams_loading_state"] = "loading"
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

from local_settings_routes import bp as settings_bp, get_printer_cost_settings
from nfc_routes import bp as ams_nfc_bp
import nfc_pending_repository
from flask import jsonify, redirect, request, url_for, render_template, send_from_directory, Response, stream_with_context, session, g, has_request_context
import mqtt_bambulab
import database as app_database
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
else:
    try:
        _density_repair = print_history_service.recalculate_usage_grams_from_lengths()
        if _density_repair.get("usage_rows") or _density_repair.get("tracking_rows"):
            _log(
                "[History] Materialdichte-Rückrechnung abgeschlossen: "
                f"{_density_repair['usage_rows']} Verbrauchszeilen, "
                f"{_density_repair['tracking_rows']} Drucksummen."
            )
    except Exception as exc:
        _log(f"Materialdichte-Rückrechnung fehlgeschlagen: {exc!r}")
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
# history data without relying on a second application entry point.
_m0bambospool_app_module.LAYER_TRACKING_STATUS_DISPLAY.update({
    "PREPARING": ("Preparing", "info"),
    "PAUSED": ("Paused", "secondary"),
})
_MOS_HISTORY_PAGE_SIZE = 20
_MOS_HISTORY_PAGE_SIZE_OPTIONS = (10, 20, 50, 100)

def _history_page_size_from_request():
    """Return the selected history page size, or ``None`` for all rows."""
    raw_value = str(request.args.get("per_page", _MOS_HISTORY_PAGE_SIZE)).strip().lower()
    if raw_value in {"all", "alle"}:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return _MOS_HISTORY_PAGE_SIZE
    return value if value in _MOS_HISTORY_PAGE_SIZE_OPTIONS else _MOS_HISTORY_PAGE_SIZE

_original_get_prints_with_filament = print_history_service.get_prints_with_filament

def _get_prints_with_mos_pagination(limit=None, offset=None, include_deleted=False):
    # Apply MOS pagination without changing the upstream history route.
    if not has_request_context() or request.endpoint != "print_history":
        return _original_get_prints_with_filament(limit, offset, include_deleted)

    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    page_size = _history_page_size_from_request()
    requested_print_id = request.args.get("print_id")
    if requested_print_id and page_size is not None:
        target_page = print_history_service.get_print_page(
            requested_print_id,
            page_size,
            include_deleted=include_deleted,
        )
        if target_page is not None:
            page = target_page
    if page_size is None:
        prints, total_prints = _original_get_prints_with_filament(
            limit=None,
            offset=None,
            include_deleted=include_deleted,
        )
        g.mos_history_page = 1
        g.mos_history_page_size = None
        g.mos_history_total_prints = total_prints
        return prints, total_prints
    g.mos_history_page = page
    g.mos_history_page_size = page_size
    prints, total_prints = _original_get_prints_with_filament(
        limit=page_size,
        offset=(page - 1) * page_size,
        include_deleted=include_deleted,
    )
    max_page = max(1, math.ceil(total_prints / page_size))
    if page > max_page:
        page = max_page
        g.mos_history_page = page
        prints, total_prints = _original_get_prints_with_filament(
            limit=page_size,
            offset=(page - 1) * page_size,
            include_deleted=include_deleted,
        )
    g.mos_history_total_prints = total_prints
    return prints, total_prints

print_history_service.get_prints_with_filament = _get_prints_with_mos_pagination
_original_app_render_template = _m0bambospool_app_module.render_template


def _history_spool_cost_per_gram(spool):
    """Use the same canonical cost rate as the history query service."""
    return print_history_service.spool_cost_per_gram(spool)


def _print_duration_minutes(print_entry, *, actual=False):
    tracking = print_entry.get("layer_tracking") if isinstance(print_entry, dict) else None
    tracking = tracking if isinstance(tracking, dict) else print_entry
    if not isinstance(tracking, dict):
        return 0.0
    if not actual:
        try:
            estimated = float(tracking.get("estimated_duration_minutes") or 0)
            if estimated > 0:
                return estimated
        except (TypeError, ValueError):
            pass
    start_value = print_entry.get("print_date")
    end_value = tracking.get("actual_end_time") or print_entry.get("actual_end_time")
    try:
        start = datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
        if end_value:
            end = datetime.fromisoformat(str(end_value).replace("Z", "+00:00"))
        else:
            end = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
        return max(0.0, (end - start).total_seconds() / 60.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _add_machine_costs(print_entry):
    """Add energy/depreciation overhead to a history or Home print record."""
    if not isinstance(print_entry, dict):
        return print_entry
    settings = get_printer_cost_settings()
    expected_minutes = _print_duration_minutes(print_entry, actual=False)
    actual_minutes = _print_duration_minutes(print_entry, actual=True)
    per_minute = settings["printer_cost_per_minute"]
    expected_machine = expected_minutes * per_minute
    actual_machine = actual_minutes * per_minute
    print_entry["expected_machine_cost"] = expected_machine
    print_entry["actual_machine_cost"] = actual_machine
    print_entry["expected_gross_cost_total"] = float(print_entry.get("expected_cost_total") or 0) + expected_machine
    print_entry["actual_gross_cost_total"] = float(print_entry.get("actual_cost_total") or 0) + actual_machine
    print_entry["expected_duration_minutes_for_cost"] = expected_minutes
    print_entry["actual_duration_minutes_for_cost"] = actual_minutes
    return print_entry


def _history_slot_for_spool(spool):
    if not isinstance(spool, dict):
        return None
    active_tray = spool.get("active_tray") or (spool.get("extra") or {}).get("active_tray")
    try:
        parts = str(active_tray or "").strip().strip('"').rsplit("_", 2)
        if len(parts) != 3:
            return None
        slot = int(parts[1]) * 4 + int(parts[2])
        return slot if 0 <= slot < 16 else None
    except (TypeError, ValueError):
        return None


def _history_normalized_hex(value):
    return str(value or "").strip().replace("#", "").upper()[0:6]


def _history_spool_signature(spool):
    if not isinstance(spool, dict):
        return None
    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    color = _history_normalized_hex(filament.get("color_hex"))
    material = str(filament.get("material") or "").strip().upper()
    return (material, color) if material and color else None


def _enrich_history_costs_and_repair_duplicate_spools(prints):
    """Add costs and repair stale duplicate spool bindings for display.

    Older jobs can contain two logical G-code rows bound to the same local
    spool even though the AMS had two distinct assigned trays.  The local AMS
    tray assignment is authoritative; colour metadata is deliberately not used
    to choose a spool.  We retain the row on its assigned tray and use an
    otherwise-unused active tray for the stale duplicate.  If no unambiguous
    tray exists, the original row is left untouched for manual correction.
    """
    try:
        spool_list = inventory_db.list_spools(include_archived=True)
    except Exception:
        # Cost fields remain available for rows already enriched by app.py;
        # never turn a transient catalogue read into a history page failure.
        spool_list = []
    spool_by_id = {item.get("id"): item for item in spool_list if item.get("id") is not None}
    active_by_slot = {}
    for spool in spool_list:
        slot = _history_slot_for_spool(spool)
        if slot is None or slot in active_by_slot:
            continue
        active_by_slot[slot] = spool
    active_by_signature = {}
    for slot, spool in active_by_slot.items():
        signature = _history_spool_signature(spool)
        if signature is not None:
            active_by_signature.setdefault(signature, []).append((slot, spool))

    for print_entry in prints or []:
        rows = print_entry.get("filament_usage") or []
        # Repair legacy rows whose logical AMS index was attached to the
        # wrong physical tray.  This is not colour-based selection: the
        # G-code material/color is used only as a consistency key, and the
        # candidate must be the unique matching spool already assigned in the
        # local AMS.  Duplicate colours remain ambiguous and are untouched.
        for row in rows:
            gcode_signature = (
                str(row.get("gcode_filament_type") or "").strip().upper(),
                _history_normalized_hex(row.get("gcode_color")),
            )
            candidates = active_by_signature.get(gcode_signature, [])
            if len(candidates) != 1:
                continue
            slot, candidate = candidates[0]
            if row.get("spool_id") == candidate.get("id"):
                row["physical_ams_slot"] = slot
                continue
            row["spool_id"] = candidate.get("id")
            row["spool"] = candidate
            row["physical_ams_slot"] = slot

        # Detect only a genuine cross-colour duplicate.  Same-colour reconnect
        # rows are already merged by the upstream history transformation.
        by_spool = {}
        for row in rows:
            spool_id = row.get("spool_id")
            if spool_id is not None:
                by_spool.setdefault(spool_id, []).append(row)
        occupied_slots = {
            _history_slot_for_spool(row.get("spool"))
            for row in rows
            if row.get("spool") is not None and _history_slot_for_spool(row.get("spool")) is not None
        }
        for spool_id, duplicate_rows in by_spool.items():
            if len(duplicate_rows) < 2:
                continue
            logical_slots = {row.get("ams_slot") for row in duplicate_rows}
            gcode_colours = {str(row.get("gcode_color") or "").strip().lower() for row in duplicate_rows}
            if len(logical_slots) < 2 or len(gcode_colours) < 2:
                continue
            source_spool = spool_by_id.get(spool_id) or next(
                (row.get("spool") for row in duplicate_rows if row.get("spool")), None
            )
            source_slot = _history_slot_for_spool(source_spool)
            # Keep the largest expected segment on the currently assigned
            # physical tray. This is deterministic and never colour-based.
            ordered = sorted(
                duplicate_rows,
                key=lambda row: float(row.get("estimated_grams") or 0),
                reverse=True,
            )
            if source_slot is not None:
                occupied_slots.add(source_slot)
            for stale_row in ordered[1:]:
                candidates = [
                    (slot, candidate)
                    for slot, candidate in sorted(active_by_slot.items())
                    if slot not in occupied_slots and candidate.get("id") != spool_id
                ]
                if not candidates:
                    continue
                slot, replacement = candidates[0]
                stale_row["spool_id"] = replacement.get("id")
                stale_row["spool"] = replacement
                stale_row["physical_ams_slot"] = slot
                occupied_slots.add(slot)

        actual_total = 0.0
        expected_total = 0.0
        for row in rows:
            spool = row.get("spool")
            rate = _history_spool_cost_per_gram(spool)
            try:
                actual_grams = float(row.get("grams_used") or 0)
            except (TypeError, ValueError):
                actual_grams = 0.0
            try:
                expected_grams = float(row.get("estimated_grams")) if row.get("estimated_grams") is not None else 0.0
            except (TypeError, ValueError):
                expected_grams = 0.0
            row["actual_cost"] = max(0.0, actual_grams * rate)
            row["expected_cost"] = max(0.0, expected_grams * rate)
            # Keep the upstream alias available for any extension templates.
            row["cost"] = row["actual_cost"]
            actual_total += row["actual_cost"]
            expected_total += row["expected_cost"]
        print_entry["actual_cost_total"] = actual_total
        print_entry["expected_cost_total"] = expected_total
        _add_machine_costs(print_entry)


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
            selected_page_size = getattr(g, "mos_history_page_size", _MOS_HISTORY_PAGE_SIZE)
            kwargs["page"] = requested_page
            kwargs["per_page"] = selected_page_size if selected_page_size is not None else total_prints
            kwargs["history_page_size"] = selected_page_size
            kwargs["history_page_size_options"] = _MOS_HISTORY_PAGE_SIZE_OPTIONS
            kwargs["total_pages"] = (
                1 if selected_page_size is None
                else max(1, math.ceil(total_prints / selected_page_size))
            )
        filter_options = print_history_service.get_history_filter_options(
            request.args.get("show_deleted") == "1"
        )
        status_options = []
        seen_status_values = set()
        # Build the menu from the complete history domain, not the rows on
        # the current page.  Include every status the tracker can expose so
        # the filter remains stable even when a status has no row on page 1.
        all_status_keys = set(filter_options["statuses"]) | {
            str(key).upper()
            for key in _m0bambospool_app_module.LAYER_TRACKING_STATUS_DISPLAY
        }
        for status_key in sorted(all_status_keys):
            status_label = _m0bambospool_app_module.LAYER_TRACKING_STATUS_DISPLAY.get(
                status_key, (status_key.title(), "secondary")
            )[0]
            status_value = status_label.lower()
            if status_value in seen_status_values:
                continue
            seen_status_values.add(status_value)
            status_options.append({
                "value": status_value,
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
            # scroll to its table row.
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
        _enrich_history_costs_and_repair_duplicate_spools(kwargs.get("prints", []) or [])
        kwargs["history_currency_symbol"] = "€"
    rendered = _original_app_render_template(template_name, *args, **kwargs)
    if template_name == "print_history.html":
        return rendered.replace(
            "</body>",
            _history_page_controls_script(kwargs["page"], kwargs["total_pages"]) + "</body>",
        )
    return rendered

_m0bambospool_app_module.render_template = _render_template_with_printer_progress

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

app.register_blueprint(settings_bp)
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
    # The bare application URL is the local Home dashboard.  It must remain
    # reachable even while MQTT is offline so the user can see the offline
    # state and use the rest of the local UI; local settings remain available
    # through its explicit route.
    if request.endpoint == "home" and request.path != "/" and not mqtt_bambulab.isMqttClientConnected():
        return redirect(url_for("settings.index"))


@app.route("/home")
def home_status():
    # Show the connected printer and its current connection/job status.
    status_payload = _current_printer_status_payload()
    return render_template(
        "home_status.html",
        last_print=status_payload.get("last_print"),
        active_print_running=_active_print_is_running(),
        park_printer_available=status_payload.get("park_printer_available", False),
    )


@app.route("/home/", endpoint="home_status_trailing")
def home_status_trailing():
    # Accept the trailing-slash variant without creating a second dashboard.
    return home_status()


# app.py owns the historical `/` route; point that existing rule at the local
# Home dashboard.  `/ams` remains the explicit legacy AMS dashboard route.
_m0bambospool_app_module.app.view_functions["home"] = home_status


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
    printer_is_active = str(print_state.get("gcode_state") or "").upper() in {"PREPARE", "PREPARING", "RUNNING", "PAUSE", "PAUSED", "SLICING"}
    park_printer_available = (
        connected
        and not printer_is_active
        and not mqtt_bambulab.is_bed_parked()
        and mqtt_bambulab.is_filament_unloaded() is not True
    )
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
    if last_print:
        _add_machine_costs(last_print)
    return {
        "printer_name": PRINTER_NAME or (getattr(mqtt_bambulab, "getPrinterModel", lambda: {})() or {}).get("devicename") or PRINTER_ID,
        "mqtt_connected": connected,
        "mqtt_status": _ui_text("Verbunden" if connected else "Nicht verbunden"),
        "printer_state": print_state.get("gcode_state") or "OFFLINE",
        "printer_status": _ui_text(_printer_status_code()),
        "printer_preparation_status": _ui_text(_printer_preparation_status_code() or ""),
        "filament_activity": _current_filament_activity(),
        "print_id": active_print_id,
        "last_print": last_print,
        "temperatures": _printer_temperature_status(),
        "ams_environment": _ams_environment_status(),
        "download": {
            key: value for key, value in JOBS_3MF.get().items()
            if key in {"transfer_id", "state", "percent", "bytes_downloaded", "bytes_total", "speed_bytes_per_second", "elapsed_seconds", "error"}
        },
        "park_printer_available": park_printer_available,
    }


def _current_filament_activity():
    # Expose the physical filament currently being loaded or used for printing.
    # The AMS tray assignment is authoritative; color is only the display label.
    print_state = (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {})
    ams_config = getattr(mqtt_bambulab, "LAST_AMS_CONFIG", {}) or {}
    candidate = None
    for ams in ams_config.get("ams", []) or []:
        for tray in ams.get("tray", []) or []:
            tray_status = str(tray.get("tray_status") or tray.get("status") or "").strip().lower()
            tray_pending = False
            try:
                tray_pending = mqtt_bambulab.is_ams_tray_operation_pending(ams.get("id"), tray.get("id"))
            except Exception:
                tray_pending = False
            if tray_pending or tray_status in {"loading", "unloading", "changing", "load", "unload"}:
                activity_state = "unloading" if tray_status in {"unloading", "unload"} else "loading"
                candidate = (ams.get("id"), tray.get("id"), activity_state)
                break
        if candidate:
            break
    try:
        mapping = mqtt_bambulab._active_tray_mapping(print_state)
    except Exception:
        mapping = None
    # For the currently printing filament, tray_tar is the printer's live
    # physical tray and takes precedence over the 3MF filament-order mapping.
    try:
        live_tray = int((print_state.get("ams") or {}).get("tray_tar"))
        if 0 <= live_tray < 16:
            mapping = live_tray
    except (TypeError, ValueError):
        pass
    state = str(print_state.get("gcode_state") or "").upper()
    try:
        stage = int(print_state.get("stg_cur", -1))
        sub_stage = int(print_state.get("mc_print_sub_stage", -1))
        tray_target = str((print_state.get("ams") or {}).get("tray_tar"))
    except (TypeError, ValueError):
        stage, sub_stage, tray_target = -1, -1, ""
    if candidate is None and (stage == 4 or sub_stage in {2, 4}) and tray_target not in {"", "254", "255"}:
        try:
            physical_tray = int(tray_target)
            candidate = (mqtt_bambulab.getAMSFromTray(physical_tray), physical_tray % 4, "loading")
        except (TypeError, ValueError):
            candidate = None
    if candidate is None and tray_target in {"254", "255"}:
        previous_print = (getattr(mqtt_bambulab, "PRINTER_STATE_LAST", {}) or {}).get("print", {}) or {}
        previous_target = str((previous_print.get("ams") or {}).get("tray_tar") or "")
        if previous_target.isdigit() and 0 <= int(previous_target) < 16:
            candidate = (mqtt_bambulab.getAMSFromTray(int(previous_target)), int(previous_target) % 4, "unloading")
    if candidate is None and state in {"PREPARE", "RUNNING", "PAUSE"} and mapping is not None:
        try:
            ams_id = mqtt_bambulab.getAMSFromTray(mapping)
            tray_id = int(mapping) - int(ams_id) * 4
            if 0 <= tray_id < 4:
                candidate = (ams_id, tray_id, "printing")
        except (TypeError, ValueError):
            candidate = None
    if candidate is None:
        return None
    ams_id, tray_id, activity_state = candidate
    tray_key = json.dumps(f"{PRINTER_ID}_{ams_id}_{tray_id}")
    ams_tray = next(
        (tray for ams in ams_config.get("ams", []) or []
         if str(ams.get("id")) == str(ams_id)
         for tray in ams.get("tray", []) or []
         if str(tray.get("id")) == str(tray_id)),
        None,
    )
    spool = next(
        (item for item in inventory_db.list_spools(include_archived=True)
         if str(item.get("active_tray") or "") in {tray_key, tray_key.strip('"')}),
        None,
    )
    filament = (spool or {}).get("filament") or {}
    # The local filament record is authoritative for the activity label.  Do
    # not derive the name from the physical tray colour: identical colours may
    # exist in multiple filaments and the AMS colour is only presentation data.
    label = str(filament.get("name") or filament.get("material") or "").strip()
    physical_color = str((ams_tray or {}).get("tray_color") or "").strip().replace("#", "").upper()
    if len(physical_color) >= 8:
        physical_color = physical_color[:6]
    if not label and physical_color:
        try:
            color_match = next(
                (color for color in inventory_db.list_colors()
                 if str(color.get("hex_code") or "").replace("#", "").upper()[:6] == physical_color),
                None,
            )
            label = str((color_match or {}).get("name") or "").strip()
        except Exception:
            label = ""
    if not label:
        label = "Filament"
    return {
        "label": label,
        "material": str(filament.get("material") or "").strip(),
        "spool_id": spool.get("id") if spool else None,
        "state": activity_state,
        "tray": f"{ams_id}:{tray_id}",
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


@app.post("/home/unload-filament")
def home_unload_filament():
    last_print = print_history_service.get_latest_print_summary()
    last_status = str(last_print.get("status") or "").upper() if last_print else ""
    current_state = str((getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}).get("gcode_state") or "").upper()
    live_status = print_history_service.printer_state_to_history_status(
        current_state,
        (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}).get("print_error"),
    )
    if last_status != "ABORTED" and live_status != "ABORTED":
        return jsonify({"ok": False, "error": _ui_text("Filament entladen ist nur nach einem abgebrochenen Druck möglich.")}), 409
    if current_state in {"PREPARING", "RUNNING", "PAUSED", "SLICING"}:
        return jsonify({"ok": False, "error": _ui_text("Filament kann während eines aktiven Drucks nicht entladen werden.")}), 409
    if not mqtt_bambulab.isMqttClientConnected():
        return jsonify({"ok": False, "error": _ui_text("Der Drucker ist nicht verbunden.")}), 503
    if not mqtt_bambulab.unload_filament():
        return jsonify({"ok": False, "error": _ui_text("Der Entladebefehl konnte nicht gesendet werden.")}), 502
    return jsonify({"ok": True})


@app.post("/home/park-printer")
def home_park_printer():
    current_state = str((getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}).get("gcode_state") or "").upper()
    if current_state in {"PREPARING", "RUNNING", "PAUSED", "SLICING"}:
        return jsonify({"ok": False, "error": _ui_text("Das Druckbett kann während eines aktiven Drucks nicht abgesenkt werden.")}), 409
    if mqtt_bambulab.is_bed_parked():
        return jsonify({"ok": False, "error": _ui_text("Das Druckbett ist bereits abgesenkt.")}), 409
    if mqtt_bambulab.is_filament_unloaded() is True:
        return jsonify({"ok": False, "error": _ui_text("Das Filament ist bereits entladen.")}), 409
    if not mqtt_bambulab.isMqttClientConnected():
        return jsonify({"ok": False, "error": _ui_text("Der Drucker ist nicht verbunden.")}), 503
    if not mqtt_bambulab.park_printer():
        return jsonify({"ok": False, "error": _ui_text("Der Parkbefehl konnte nicht gesendet werden.")}), 502
    return jsonify({"ok": True})


@app.get("/home/humidity-history")
def home_humidity_history():
    return jsonify({
        "history": app_database.list_humidity_history(
            PRINTER_ID,
            ams_id=request.args.get("ams_id"),
        ),
    })


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
    ams_rows = (getattr(mqtt_bambulab, "LAST_AMS_CONFIG", {}) or {}).get("ams", []) or []
    try:
        app_database.record_humidity_snapshots(PRINTER_ID, ams_rows)
    except Exception as humidity_error:
        _log(f"Humidity history could not be recorded: {humidity_error!r}")
    for index, ams in enumerate(ams_rows):
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
            "id": str(ams.get("id", index)),
            "label": chr(ord("A") + index),
            "humidity": humidity,
            "humidity_raw": raw_humidity,
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
        "filament_activity": _current_filament_activity(),
        "mqtt_connected": mqtt_bambulab.isMqttClientConnected(),
        "PRINTER_ID": PRINTER_ID,
        "printer_is_busy": _printer_is_busy(),
        "ams_operation_pending": mqtt_bambulab.is_any_ams_operation_pending(),
        "pending_nfc": pending_nfc,
        "active_print_id": print_history_service.get_latest_active_print_id(),
    }


@app.before_request
def reconcile_stale_print_history_statuses():
    # The printer has one physical job at a time.  Replayed MQTT packets can
    # leave an older history attempt marked RUNNING beside the current one;
    # close that stale attempt before any page renders active-job state.
    try:
        print_history_service.reconcile_multiple_active_prints(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    except Exception as exc:
        _log(f"[History] Mehrfach aktive Drucke konnten nicht bereinigt werden: {exc!r}")
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
    spool_ids = [spool.get("id") for spool in spools if spool.get("id") is not None]
    prints_by_spool = print_history_service.get_spool_print_usage_for_spools(spool_ids)
    for spool in spools:
        spool_id = spool.get("id")
        if spool_id is None:
            continue
        prints = prints_by_spool.get(int(spool_id), [])
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
        # The local catalog is already authoritative.  Do not re-synchronize
        # every spool during a read-only page request; that caused one schema
        # initialization and several SQLite writes per spool.
        vendors = inventory_db.list_entities("vendors")
        materials = inventory_db.list_entities("materials")
        colors = inventory_db.list_colors()
        filaments = inventory_db.list_filaments()
        local_spools = spools
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


@app.delete("/inventory/local-filaments/<int:filament_id>")
def delete_local_filament(filament_id):
    try:
        inventory_db.delete_entity("filaments", filament_id)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": _ui_text(str(exc))}), 409
    except Exception as exc:
        _log(f"Local filament delete failed ({filament_id}): {exc!r}")
        return jsonify({"error": "Löschen fehlgeschlagen."}), 500
    return jsonify({"saved": True, "deleted": filament_id})


@app.post("/inventory/local-filaments/<int:filament_id>/duplicate")
def duplicate_local_filament(filament_id):
    try:
        source = next((item for item in inventory_db.list_filaments() if int(item["id"]) == int(filament_id)), None)
        if not source:
            raise KeyError(f"No filament with ID {filament_id}")
        values = {field: source.get(field) for field in (
            "vendor_id", "material_id", "color_id", "color_hex", "diameter", "weight",
            "spool_weight", "price", "nozzle_temp_min", "nozzle_temp_max",
            "cali_idx", "bambu_filament_id", "bambu_setting_id", "comment",
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
    return redirect(url_for("settings.index"))


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
        record = {"id": saved_id, **payload}
        if entity in {"vendors", "materials"}:
            record = next((item for item in inventory_db.list_entities(entity) if int(item["id"]) == int(saved_id)), record)
        return jsonify({"ok": True, "id": saved_id, "record": record})
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            duplicate_message = {
                "materials": "Dieses Material ist bereits vorhanden.",
                "colors": "Diese Farbe ist bereits vorhanden.",
            }.get(entity, "Dieser Eintrag ist bereits vorhanden.")
            return jsonify({"error": duplicate_message}), 409
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
            if any(item.get("tag_value") == value for item in inventory_db.list_nfc_tags()):
                raise ValueError("Dieser NFC-Tag-Wert ist bereits bekannt")
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
        # The action originates on the AMS page; return there so the user can
        # immediately verify the physical tray status and the local mapping.
        return redirect(
            url_for(
                "ams",
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
    # Suppress the verbose raw AMS command dump from the legacy implementation.
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
    # Suppress legacy AMS detail dumps emitted by the legacy implementation.
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


