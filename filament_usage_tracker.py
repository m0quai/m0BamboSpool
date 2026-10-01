import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from config import EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID, TRACK_LAYER_USAGE
from inventory_repository import record_consumption
from inventory_service import fetchSpools, getAMSFromTray, trayUid
from tools_3mf import available_thumbnail_filename, download3mfFromCloud, download3mfFromFTP, download3mfFromLocalFilesystem, getMetaDataFrom3mf, getMetaDataFromLocal3mf, get_source_file_name
from print_history import bind_filament_usage_spool, record_filament_usage_segment, claim_filament_usage_event, set_filament_usage_event_status, finalize_filament_usage_events, get_all_filament_usage_for_print, update_layer_tracking, update_print_image, update_print_file_name, get_print_image, get_latest_running_print_id, find_latest_print_id, printer_state_to_history_status

GCODE_STATE_LABELS = {
    "IDLE": "Drucker bereit",
    "PREPARE": "Druck wird vorbereitet",
    "RUNNING": "Druck läuft",
    "PAUSE": "Druck pausiert",
    "FINISH": "Druck abgeschlossen",
    "FAILED": "Druck fehlgeschlagen",
    "SLICING": "Datei wird verarbeitet",
}
from logger import log


CHECKPOINT_DIR = Path(__file__).resolve().parent / "data" / "checkpoint"
CHECKPOINT_VERSION = 4
LAYER_TRACKING_STATUS_RUNNING = "RUNNING"
LAYER_TRACKING_STATUS_COMPLETED = "COMPLETED"
LAYER_TRACKING_STATUS_ABORTED = "ABORTED"
LAYER_TRACKING_STATUS_FAILED = "FAILED"
ABORT_INDICATOR_STATES = {
    "STOP",
    "ABORT",
    "ABORTED",
    "CANCEL",
    "CANCELED",
    "CANCELLED",
    "ERROR",
    "IDLE",
    "FAILED",
}


def _checkpoint_dir() -> Path:
  CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
  return CHECKPOINT_DIR


def _checkpoint_metadata_path() -> Path:
  return _checkpoint_dir() / "metadata.json"


def _get_checkpoint_metadata() -> dict:
  metadata_path = _checkpoint_metadata_path()
  if not metadata_path.exists():
    return {}
  try:
    return json.loads(metadata_path.read_text(encoding="utf-8"))
  except Exception as exc:
    log(f"[filament-tracker] Ungültige Checkpoint-Metadaten werden ignoriert: {exc!r}")
    return {}


def _save_checkpoint_metadata(metadata: dict) -> None:
  _checkpoint_dir()
  metadata_path = _checkpoint_metadata_path()
  temp_path = metadata_path.with_suffix(".json.tmp")
  temp_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
  temp_path.replace(metadata_path)


def _safe_checkpoint_model_name(source_name: str | None, task_id=None, subtask_id=None) -> str:
  # Preserve the original 3MF name, but never allow path traversal.
  raw_name = os.path.basename(str(source_name or "").replace("\\", "/")).strip()
  if not raw_name.lower().endswith(".3mf"):
    fallback = "-".join(str(value) for value in (task_id, subtask_id) if value not in (None, ""))
    raw_name = f"{fallback or 'print'}.3mf"
  safe_name = re.sub(r"[^A-Za-z0-9._() -]+", "_", raw_name).strip(" .")
  return safe_name or "print.3mf"


def _file_sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def archive_checkpoint_model(
    model_path: str,
    model_file_name: str | None = None,
    task_id=None,
    subtask_id=None,
) -> Path:
  """Keep a completed download under its original, sanitized 3MF filename."""
  source = Path(model_path)
  checkpoint_name = _safe_checkpoint_model_name(model_file_name, task_id, subtask_id)
  destination = _checkpoint_dir() / checkpoint_name
  temporary = destination.with_suffix(destination.suffix + ".part")
  with source.open("rb") as source_stream, temporary.open("wb") as target_stream:
    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
  temporary.replace(destination)
  log(
    f"[filament-tracker] 3MF archiviert: job={task_id}:{subtask_id}, "
    f"quelle={str(source)!r}, datei={checkpoint_name!r}, bytes={destination.stat().st_size}"
  )
  return destination


def save_checkpoint(
    *,
    model_path: str,
    current_layer: int,
    task_id,
    subtask_id,
    ams_mapping,
    gcode_file_name: str,
    model_file_name: str | None = None,
) -> None:
  destination = archive_checkpoint_model(
    model_path,
    model_file_name=model_file_name,
    task_id=task_id,
    subtask_id=subtask_id,
  )
  checkpoint_name = destination.name

  metadata = {
    "task_id": task_id,
    "subtask_id": subtask_id,
    "current_layer": current_layer,
    "ams_mapping": ams_mapping,
    "gcode_file_name": gcode_file_name,
    "model_file_name": checkpoint_name,
    "model_size": destination.stat().st_size,
    "model_sha256": _file_sha256(destination),
    "checkpoint_version": CHECKPOINT_VERSION,
  }
  _save_checkpoint_metadata(metadata)
  log(
    f"[filament-tracker] Checkpoint gespeichert: job={task_id}:{subtask_id}, "
    f"datei={checkpoint_name!r}, bytes={metadata['model_size']}"
  )


def clear_checkpoint(*, preserve_models: bool = False) -> None:
  # Remove the model files as well once the checkpoint is no longer active.
  # Recovery paths that must retain a verified model can opt in explicitly.
  metadata_path = CHECKPOINT_DIR / "metadata.json"
  if metadata_path.exists():
    metadata_path.unlink()
  for part_file in CHECKPOINT_DIR.glob("*.part") if CHECKPOINT_DIR.exists() else []:
    part_file.unlink()
  if not preserve_models and CHECKPOINT_DIR.exists():
    for item in CHECKPOINT_DIR.iterdir():
      if item.is_file():
        item.unlink()
    try:
      CHECKPOINT_DIR.rmdir()
    except OSError:
      pass


def update_checkpoint_layer(layer: int) -> None:
  metadata = _get_checkpoint_metadata()
  if not metadata:
    return
  metadata["current_layer"] = layer
  _save_checkpoint_metadata(metadata)


def _same_job_id(left, right) -> bool:
  if left in (None, "") or right in (None, ""):
    return False
  return str(left) == str(right)


def recover_model(task_id, subtask_id):
  metadata = _get_checkpoint_metadata()
  if not metadata:
    return None

  if metadata.get("checkpoint_version") != CHECKPOINT_VERSION:
    log("[filament-tracker] Alter Checkpoint wird nicht verwendet.")
    clear_checkpoint(preserve_models=False)
    return None

  checkpoint_task_id = metadata.get("task_id")
  checkpoint_subtask_id = metadata.get("subtask_id")
  comparable_ids = []
  if task_id not in (None, ""):
    comparable_ids.append(_same_job_id(checkpoint_task_id, task_id))
  if subtask_id not in (None, ""):
    comparable_ids.append(_same_job_id(checkpoint_subtask_id, subtask_id))
  if not comparable_ids or not all(comparable_ids):
    log(
      f"[filament-tracker] Checkpoint gehört zu anderem oder nicht eindeutigem Job: "
      f"{checkpoint_task_id}:{checkpoint_subtask_id} != {task_id}:{subtask_id}"
    )
    clear_checkpoint(preserve_models=False)
    return None

  model_file_name = metadata.get("model_file_name")
  if not model_file_name or os.path.basename(model_file_name) != model_file_name:
    clear_checkpoint(preserve_models=False)
    return None
  model_path = _checkpoint_dir() / model_file_name
  if not model_path.is_file():
    clear_checkpoint(preserve_models=False)
    return None

  expected_size = metadata.get("model_size")
  expected_hash = metadata.get("model_sha256")
  if expected_size is None or int(expected_size) != model_path.stat().st_size:
    log("[filament-tracker] Checkpoint-Größe stimmt nicht; Datei wird nicht verwendet.")
    clear_checkpoint(preserve_models=False)
    return None
  if not expected_hash or _file_sha256(model_path) != expected_hash:
    log("[filament-tracker] Checkpoint-Prüfsumme stimmt nicht; Datei wird nicht verwendet.")
    clear_checkpoint(preserve_models=False)
    return None

  current_layer = metadata.get("current_layer")
  if current_layer is None:
    return None
  return (
    str(model_path),
    metadata.get("gcode_file_name"),
    current_layer,
    metadata.get("ams_mapping"),
  )


def _restore_thumbnail(model_path: str, print_id: int) -> None:
  # Extract a missing print thumbnail from the persisted 3MF checkpoint.
  try:
    existing_image = get_print_image(print_id)
    if existing_image and (Path(__file__).resolve().parent / "static" / "prints" / existing_image).is_file():
      log(f"[filament-tracker] Thumbnail already present: {existing_image}")
      return
    log(f"[filament-tracker] Thumbnail restore: print_id={print_id}, model={model_path!r}")
    with zipfile.ZipFile(model_path) as archive:
      source_name = get_source_file_name(archive)
      candidates = [name for name in archive.namelist() if name.startswith("Metadata/plate_") and name.endswith(".png")]
      log(f"[filament-tracker] Thumbnail restore: found {len(candidates)} PNG candidate(s)")
      if not candidates:
        return
      target_dir = Path(__file__).resolve().parent / "static" / "prints"
      target_dir.mkdir(parents=True, exist_ok=True)
      image_name = available_thumbnail_filename(source_name, str(target_dir), f"print_{print_id}")
      (target_dir / image_name).write_bytes(archive.read(candidates[0]))
      update_print_image(print_id, image_name)
      log(f"[filament-tracker] Restored print thumbnail {image_name} at {target_dir / image_name}")
  except Exception as exc:
    log(f"[filament-tracker] Could not restore print thumbnail: {exc!r}")


class GCodeOperation:
  def __init__(self, raw_line: str):
    self.operation = None
    self.params = {}
    self.comment = None
    self._parse(raw_line)

  def _parse(self, raw_line: str) -> None:
    parts = list(map(lambda x: x.strip(), raw_line.split(";")))
    if len(parts) > 1:
      self.comment = parts[1].strip()

    parts = parts[0].split()
    self.operation = parts[0]
    for part in parts[1:]:
      key = part[0]
      value = part[1:]
      self.params[key] = value


def _parse_gcode(gcode: str) -> list[GCodeOperation]:
  operations = []
  for line in gcode.split("\n"):
    line = line.strip()
    if not line or line.startswith(";"):
      continue
    operations.append(GCodeOperation(line))
  return operations


def evaluate_gcode(gcode: str) -> dict:
  #
  # Evaluate the gcode and return the filament usage (in mm) per layer.
  operations = _parse_gcode(gcode)

  current_layer = 0
  current_extrusion = {}
  active_filament = None
  layer_filaments = {}

  for operation in operations:
    if operation.operation == "M73":
      next_layer = operation.params.get("L")
      if next_layer is not None:
        if current_extrusion:
          layer_filaments[current_layer] = current_extrusion.copy()
          current_extrusion = {}
        current_layer = int(next_layer)

    if operation.operation == "M620":
      filament = operation.params.get("S")
      if filament is not None:
        if filament == "255":
          active_filament = None
          continue
        active_filament = int(filament[:-1])

    if operation.operation in ("G0", "G1", "G2", "G3"):
      extrusion = operation.params.get("E")
      if extrusion is None:
        continue
      if active_filament is None:
        continue
      extrusion_amount = float(extrusion)
      current_extruded = current_extrusion.get(active_filament, 0)
      current_extrusion[active_filament] = current_extruded + extrusion_amount

  if current_extrusion:
    layer_filaments[current_layer] = current_extrusion.copy()
  if layer_filaments:
    log(f"[filament-tracker] Model layers available: {max(layer_filaments)}")
  return layer_filaments


def extract_gcode_from_3mf(path: str, gcode_path: str | None) -> str | None:
  with zipfile.ZipFile(path, "r") as z:
    if gcode_path is None:
      config_path = "Metadata/model_settings.config"
      if config_path not in z.namelist():
        return None
      root = ET.parse(z.open(config_path)).getroot()
      plate = root[0]
      for item in plate:
        if item.attrib.get("key") == "gcode_file":
          gcode_path = item.attrib.get("value")
          break
      if gcode_path is None:
        return None
    if gcode_path not in z.namelist():
      return None
    with z.open(gcode_path) as g_file:
      return g_file.read().decode("utf-8")


class FilamentUsageTracker:
  def __init__(self):
    self.active_model = None
    self.ams_mapping = None
    self.spent_layers = set()
    self.using_ams = False
    self.gcode_state = None
    self.current_layer = None
    self.print_metadata = None
    self.print_id = None
    self.cumulative_grams_used = {}  # Track cumulative grams used per filament index
    self.cumulative_length_used = {}  # Track cumulative length (mm) per filament index
    self._layer_tracking_total_layers = None
    self._total_usage_mm_per_filament = {}
    self._layer_tracking_predicted_total = None
    self._filament_spool_id_map = {}
    self._spool_data_cache = {}
    self._layer_tracking_status = None
    self._layer_tracking_start_time = None
    self._pending_usage_mm = {}
    self._mc_remaining_time_minutes = None
    self._last_logged_gcode_state = None
  def set_print_metadata(self, metadata: dict | None) -> None:
    metadata = metadata or {}
    incoming_id = metadata.get("print_id")
    if (
        self.print_id
        and incoming_id
        and incoming_id != self.print_id
        and self._layer_tracking_status == LAYER_TRACKING_STATUS_RUNNING
    ):
      self._set_layer_tracking_status(
          LAYER_TRACKING_STATUS_ABORTED, target_print_id=self.print_id
      )
    self.print_metadata = metadata
    self.print_id = incoming_id

  def begin_pending_print(self, metadata: dict | None) -> None:
    # Bind the newly created history row immediately, before the 3MF worker
    # finishes.  Never let a previous model/checkpoint leak into the new job.
    metadata = metadata or {}
    incoming_id = metadata.get("print_id")
    if incoming_id is None:
      return
    if self.print_id != incoming_id:
      # A new printer job supersedes any previous recovery checkpoint.
      clear_checkpoint()
      self.set_print_metadata(metadata)
      self.active_model = None
      self.ams_mapping = metadata.get("ams_mapping") or None
      self.using_ams = bool(self.ams_mapping and self.ams_mapping[0] != EXTERNAL_SPOOL_ID)
      self.current_layer = None
      self.spent_layers = set()
      self.cumulative_grams_used = {}
      self.cumulative_length_used = {}
      self._reset_layer_tracking_state()
    else:
      self.print_metadata = metadata

  def on_message(self, message: dict) -> None:
    if "print" not in message:
      return

    print_obj = message.get("print", {})
    command = print_obj.get("command")
    if print_obj.get('gcode_state') is not None:
      state = print_obj.get('gcode_state')
      state_label = (
        "Druck abgebrochen"
        if state == "FAILED" and str(print_obj.get("print_error")) == "0"
        else GCODE_STATE_LABELS.get(state, state)
      )
      if state != self._last_logged_gcode_state:
        log("Drucker bereit" if state == "IDLE" else f"Filament Tracker: {state_label}")
        self._last_logged_gcode_state = state
      else:
        pass

    previous_state = self.gcode_state
    self.gcode_state = print_obj.get("gcode_state", self.gcode_state)
    if "mc_remaining_time" in print_obj:
      try:
        self._mc_remaining_time_minutes = float(print_obj["mc_remaining_time"])
      except (TypeError, ValueError):
        self._mc_remaining_time_minutes = None

    if command == "project_file" and not print_obj.get("_metadata_job_queued"):
      self._handle_print_start(print_obj)

    if command == "push_status":
      self._apply_current_ams_tray(print_obj)
      if "layer_num" in print_obj:
        last_layer = self.current_layer
        layer = print_obj["layer_num"]
        if layer != last_layer:
          self._handle_layer_change(layer)
          self.current_layer = layer

    if self.gcode_state == "FINISH" and previous_state != "FINISH" and self.active_model is not None:
      self._handle_print_end()

    elif (
        previous_state == "RUNNING"
        and self._is_abort_state(self.gcode_state)
        and self.active_model is not None
    ):
        print_error = print_obj.get("print_error")
        if print_error is None:
          try:
            import mqtt_bambulab
            print_error = (getattr(mqtt_bambulab, "PRINTER_STATE", {}).get("print", {}) or {}).get("print_error")
          except Exception:
            pass
        status = printer_state_to_history_status(
            self.gcode_state, print_error
        ) or LAYER_TRACKING_STATUS_ABORTED
        self._handle_print_abort(status=status)

    if self.gcode_state == "RUNNING" and previous_state != "RUNNING" and self.active_model is None:
      task_id = print_obj.get("task_id")
      subtask_id = print_obj.get("subtask_id")
      self._attempt_print_resume(task_id, subtask_id, print_obj.get("url"), print_obj)

  def _handle_print_start(self, print_obj: dict) -> None:
    log("[filament-tracker] Print start")

    model_url = print_obj.get("url")
    model_path = print_obj.get("local_model_path") or self._retrieve_model(model_url)

    if model_path is None:
      log("Failed to retrieve model. Print will not be tracked.")
      return

    use_ams = bool(print_obj.get("use_ams", False))
    ams_mapping = print_obj.get("ams_mapping", []) if use_ams else None
    current_tray = self._active_ams_tray(print_obj)
    if current_tray is not None and not ams_mapping:
      use_ams = True
      ams_mapping = [current_tray]
      log(f"[filament-tracker] AMS mapping from current tray: filament 0 -> tray {current_tray}")
    gcode_file_name = print_obj.get("param")
    model_file_name = (
      print_obj.get("model_file_name")
      or os.path.basename(str(model_url or model_path).replace("\\", "/"))
    )
    self._start_layer_tracking_for_model(
      model_path=model_path,
      gcode_file_name=gcode_file_name,
      model_file_name=model_file_name,
      use_ams=use_ams,
      ams_mapping=ams_mapping,
      task_id=print_obj.get("task_id"),
      subtask_id=print_obj.get("subtask_id"),
      resume_layer=print_obj.get("_resume_layer"),
    )
    metadata = _get_checkpoint_metadata()
    metadata["model_url"] = model_url
    _save_checkpoint_metadata(metadata)
    log(f"[filament-tracker] Checkpoint source saved: model_url_present={bool(model_url)}")

  def _start_layer_tracking_for_model(
      self,
      model_path: str,
      gcode_file_name: str | None,
      model_file_name: str | None,
      use_ams: bool,
      ams_mapping: list[int] | None,
      task_id,
      subtask_id,
      resume_layer=None,
  ) -> None:
    self._reset_layer_tracking_state()
    clear_checkpoint()
    self.spent_layers = set()
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}
    if resume_layer is not None:
      self.current_layer = resume_layer
      self.spent_layers.update(range(resume_layer + 1))
      for slot, usage in get_all_filament_usage_for_print(self.print_id).items():
        self.cumulative_grams_used[slot] = usage.get("grams_used") or 0.0
        self.cumulative_length_used[slot] = usage.get("length_used") or 0.0

    if use_ams:
      self.ams_mapping = ams_mapping or []
      self.using_ams = True
      log(f"[filament-tracker] Using AMS mapping: {self.ams_mapping}")
    else:
      self.using_ams = False
      self.ams_mapping = None
      log("[filament-tracker] Not using AMS, defaulting to external spool")

    self._load_model(model_path, gcode_file_name)
    if self.print_id:
      _restore_thumbnail(model_path, self.print_id)

    if self.active_model:
      self._layer_tracking_total_layers = self._infer_total_layers()
      self._accumulate_total_usage_mm()
      self._layer_tracking_start_time = datetime.now()
      self._bind_initial_spools()
      self._maybe_update_predicted_total()
      if self.print_id:
        initial_fields = {"status": LAYER_TRACKING_STATUS_RUNNING}
        if self._layer_tracking_total_layers is not None:
          initial_fields["total_layers"] = self._layer_tracking_total_layers
        update_layer_tracking(self.print_id, **initial_fields)
        self._layer_tracking_status = LAYER_TRACKING_STATUS_RUNNING
        self._update_layer_tracking_progress()

    save_checkpoint(
      model_path=model_path,
      current_layer=resume_layer or 0,
      task_id=task_id,
      subtask_id=subtask_id,
      ams_mapping=self.ams_mapping,
      gcode_file_name=gcode_file_name,
      model_file_name=model_file_name,
    )

    try:
      os.remove(model_path)
    except OSError:
      pass

    self._handle_layer_change(0)

  def start_local_print_from_metadata(self, metadata: dict | None, local_model_path: str | None = None) -> None:
    if not metadata:
      return
    model_path = metadata.get("model_path", "").replace("local:", "")
    model_url = metadata.get("model_url")

    if model_path and not model_url:
      model_url = model_path

    if not model_path and not model_url:
      log("[filament-tracker] Metadata missing model_path or URL, cannot start local tracking")
      return

    log("[filament-tracker] Starting local print from cached metadata")
    self.set_print_metadata(metadata)

    ams_mapping = metadata.get("ams_mapping") or []
    fake_print = {
      "param": metadata.get("gcode_path"),
      "use_ams": bool(ams_mapping),
      "ams_mapping": ams_mapping,
      "task_id": metadata.get("task_id"),
      "subtask_id": metadata.get("subtask_id"),
      "model_file_name": metadata.get("file"),
    }
    
    fake_print["url"] = model_url
    fake_print["local_model_path"] = local_model_path or metadata.get("local_model_path")

    self._handle_print_start(fake_print)

  def start_tracking_from_cached_model(
      self,
      metadata: dict | None,
      local_model_path: str,
      print_obj: dict,
  ) -> None:
    if not metadata or not local_model_path or self.active_model is not None:
      return
    current_print = dict(print_obj or {})
    current_print["local_model_path"] = local_model_path
    current_print["url"] = current_print.get("url") or metadata.get("model_url") or metadata.get("file")
    current_print["model_file_name"] = metadata.get("file")
    current_print["ams_mapping"] = metadata.get("ams_mapping") or []
    current_print["use_ams"] = bool(current_print["ams_mapping"])
    current_print["task_id"] = metadata.get("task_id", current_print.get("task_id"))
    current_print["subtask_id"] = metadata.get("subtask_id", current_print.get("subtask_id"))
    try:
      current_print["_resume_layer"] = max(0, int(current_print.get("layer_num") or 0))
    except (TypeError, ValueError):
      current_print["_resume_layer"] = 0
    self.set_print_metadata(metadata)
    self._handle_print_start(current_print)
    if self.active_model is None:
      return

    try:
      current_layer = max(0, int(current_print.get("layer_num") or 0))
    except (TypeError, ValueError):
      current_layer = 0
    self.spent_layers.update(range(current_layer + 1))
    self.current_layer = current_layer
    update_checkpoint_layer(current_layer)
    self._update_layer_tracking_progress()
    log(
      f"[filament-tracker] Tracking nach 3MF-Download fortgesetzt: "
      f"print_id={self.print_id}, current_layer={current_layer}; "
      "bereits gedruckte Layer werden nicht doppelt verbucht"
    )

  def apply_ams_mapping(self, ams_mapping: list[int] | None) -> None:
    if not ams_mapping:
      return
    if self.ams_mapping == ams_mapping and self.using_ams:
      return

    log(f"[filament-tracker] Applying AMS mapping: {ams_mapping}")
    self.ams_mapping = ams_mapping
    self.using_ams = True
    if self.print_metadata is not None:
      self.print_metadata["ams_mapping"] = ams_mapping

    self._bind_initial_spools()
    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()

  def _retrieve_model(self, model_url: str | None) -> str | None:
    if not model_url:
      log("[filament-tracker] No model URL provided")
      return None

    uri = urlparse(model_url)
    try:
      with tempfile.NamedTemporaryFile(suffix=".3mf", delete=False) as model_file:
        if uri.scheme in ("https", "http"):
          log(f"[filament-tracker] Downloading model via HTTP(S): {model_url}")
          download3mfFromCloud(model_url, model_file)
        elif uri.scheme == "local":
          log(f"[filament-tracker] Loading model from local path: {uri.path}")
          download3mfFromLocalFilesystem(uri.path, model_file)
        else:
          log(f"[filament-tracker] Downloading model via FTP: {model_url}")
          download3mfFromFTP(model_url.rpartition('/')[-1], model_file) # Pull just filename to clear out any unexpected paths
        return model_file.name
    except Exception as exc:
      log(f"Failed to fetch model: {exc}")
      return None

  def _handle_layer_change(self, layer: int) -> None:
    if self.active_model is None:
      return
    if layer in self.spent_layers:
      return

    self.spent_layers.add(layer)
    last_layer = self.current_layer

    if last_layer is not None:
      for i in range(last_layer + 1, layer + 1):
        self._spend_filament_for_layer(i)
    else:
      self._spend_filament_for_layer(layer)

    update_checkpoint_layer(layer)

  def _handle_print_end(self) -> None:
    if self.active_model is None:
      return

    log("[filament-tracker] Print end, spending remaining layers")
    for layer in set(self.active_model.keys()) - self.spent_layers:
      self._handle_layer_change(layer)

    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()
    if self.print_id:
      finalize_filament_usage_events(self.print_id)
      self._set_layer_tracking_status(
          LAYER_TRACKING_STATUS_COMPLETED,
          extra_fields={"actual_end_time": self._format_timestamp(datetime.now())},
      )

    self.active_model = None
    self.ams_mapping = None
    self.using_ams = False
    self.current_layer = None
    self.print_metadata = None
    self.print_id = None
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}
    self._reset_layer_tracking_state()
    clear_checkpoint()

  def _handle_print_abort(self, status: str = LAYER_TRACKING_STATUS_ABORTED) -> None:
    if self.active_model is None:
      return

    log("[filament-tracker] Print aborted, stopping tracking")
    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()
    if self.print_id:
      finalize_filament_usage_events(self.print_id)
      self._set_layer_tracking_status(
          status,
          extra_fields={"actual_end_time": self._format_timestamp(datetime.now())},
      )

    self.active_model = None
    self.ams_mapping = None
    self.using_ams = False
    self.current_layer = None
    self.print_metadata = None
    self.print_id = None
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}
    self._reset_layer_tracking_state()
    clear_checkpoint()

  def _mm_to_grams(self, length_mm: float, diameter_mm: float, density_g_per_cm3: float) -> float:
    #
    # Convert filament length in mm to grams.
    # Formula: grams = (π * (diameter/2)^2 * length_mm / 1000) * density
    radius_cm = (diameter_mm / 2) / 10  # Convert mm to cm
    volume_cm3 = math.pi * radius_cm * radius_cm * (length_mm / 10)
    grams = volume_cm3 * density_g_per_cm3
    return grams

  def _spend_filament_for_layer(self, layer: int) -> None:
    if self.active_model is None:
      return

    if not TRACK_LAYER_USAGE:
      log("[filament-tracker] Layer usage tracking disabled, skipping filament spend")
      self._update_layer_tracking_progress()
      return
    layer_usage = self.active_model.get(int(layer))
    if layer_usage is None:
      return

    for filament, usage_mm in layer_usage.items():
        self._apply_usage_for_filament(filament, usage_mm, layer)

    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()

  def _apply_usage_for_filament(self, filament: int, usage_mm: float, layer: int | None = None) -> bool:
    if not TRACK_LAYER_USAGE:
      return False
    pending_mm = self._pending_usage_mm.pop(filament, 0.0)
    total_mm = (pending_mm or 0.0) + (usage_mm or 0.0)
    if total_mm <= 0:
      return False

    mapping_value = self._resolve_tray_mapping(filament)
    if mapping_value is None:
      self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
      return False

    tray_uid = self._tray_uid_from_mapping(mapping_value)
    if tray_uid is None:
      self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
      return False

    spool_id = self._lookup_spool_for_tray(tray_uid)
    if spool_id is None:
      log(f"[filament-tracker] Queued {round(total_mm, 5)}mm for filament {filament} (tray {tray_uid} has no assigned spool)")
      self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
      return False

    spool_data = self._spool_data_cache.get(spool_id)
    if spool_data is None:
      spool_data = self._get_spool_data(spool_id)
      if spool_data is None:
        log(f"[filament-tracker] Could not get spool data for {spool_id}, re-queueing usage")
        self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
        return False
      self._spool_data_cache[spool_id] = spool_data

    filament_data = spool_data.get("filament", {})
    diameter_mm = filament_data.get("diameter", 1.75)
    density = filament_data.get("density", 1.24)
    usage_grams = self._mm_to_grams(total_mm, diameter_mm, density)

    usage_rounded = round(total_mm, 5)
    event_layer = self.current_layer if layer is None else layer
    event_layer = event_layer or 0
    event_status = claim_filament_usage_event(self.print_id, event_layer, filament, spool_id, usage_rounded)
    if event_status in {"confirmed", "sent"}:
      log(f"[filament-tracker] Skipping already transferred consumption event: print_id={self.print_id}, layer={event_layer}, filament={filament}, status={event_status}")
      return False
    if self.print_id:
      set_filament_usage_event_status(self.print_id, event_layer, filament, "sent")
    filament_key = filament + 1
    previous_grams = self.cumulative_grams_used.get(filament_key, 0.0)
    self.cumulative_grams_used[filament_key] = previous_grams + usage_grams
    previous_length = self.cumulative_length_used.get(filament_key, 0.0)
    cumulative_length = previous_length + usage_rounded
    self.cumulative_length_used[filament_key] = cumulative_length

    grams_rounded = round(self.cumulative_grams_used[filament_key], 2)
    log(f"[filament-tracker] Consume spool {spool_id} for filament {filament} with {usage_rounded}mm ({grams_rounded}g cumulative) (tray_uid={tray_uid})")

    try:
      record_consumption(spool_id, length_mm=usage_rounded)
    except Exception as exc:
      log(f"[filament-tracker] Spoolman transfer failed; event remains sent: print_id={self.print_id}, layer={event_layer}, filament={filament}, error={exc!r}")
      return False

    if self.print_id:
      try:
        record_filament_usage_segment(
          self.print_id,
          filament_key,
          spool_id,
          mapping_value if mapping_value != EXTERNAL_SPOOL_ID else None,
          filament_data.get("material") or filament_data.get("name") or "",
          filament_data.get("color_hex") or "",
          usage_grams,
          usage_rounded,
        )
      except Exception as exc:
        log(f"[filament-tracker] History segment could not be saved: print_id={self.print_id}, filament={filament}, spool_id={spool_id}, error={exc!r}")
        return False
      set_filament_usage_event_status(self.print_id, event_layer, filament, "confirmed")

    self._filament_spool_id_map[filament] = spool_id
    self._spool_data_cache[spool_id] = spool_data
    return True

  def _flush_all_pending_usage(self) -> None:
    if not self._pending_usage_mm:
      return
    for filament in list(self._pending_usage_mm.keys()):
      self._apply_usage_for_filament(filament, 0.0)

  def _reset_layer_tracking_state(self) -> None:
    self._layer_tracking_total_layers = None
    self._total_usage_mm_per_filament = {}
    self._layer_tracking_predicted_total = None
    self._filament_spool_id_map = {}
    self._spool_data_cache = {}
    self._layer_tracking_status = None
    self._layer_tracking_start_time = None
    self._pending_usage_mm = {}
    self._mc_remaining_time_minutes = None
    self.cumulative_length_used = {}
    self.cumulative_grams_used = {}

  def _is_abort_state(self, state: str | None) -> bool:
    if not state:
      return False
    return state.upper() in ABORT_INDICATOR_STATES

  def _infer_total_layers(self) -> int | None:
    if not self.active_model:
      return None
    try:
      # Bambu reports the highest zero-based layer index as total_layer_num.
      # Keep the Print History value aligned with that printer value.
      return max(self.active_model.keys())
    except Exception:
      return None

  def _accumulate_total_usage_mm(self) -> None:
    self._total_usage_mm_per_filament = {}
    for layer_usage in self.active_model.values():
      for filament, usage_mm in layer_usage.items():
        self._total_usage_mm_per_filament[filament] = (
            self._total_usage_mm_per_filament.get(filament, 0.0) + usage_mm
        )

  def _format_timestamp(self, moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")

  def _compute_predicted_end_time(self, layers_printed: int, total_layers: int | None) -> str | None:
    if (
        not self._layer_tracking_start_time
        or layers_printed <= 0
        or not total_layers
        or total_layers <= layers_printed
    ):
      return None

    now = datetime.now()
    elapsed_seconds = (now - self._layer_tracking_start_time).total_seconds()
    if layers_printed == 0:
      return None

    rate_per_layer = elapsed_seconds / layers_printed
    remaining_layers = total_layers - layers_printed
    remaining_seconds = max(rate_per_layer * remaining_layers, 0)
    predicted = now + timedelta(seconds=remaining_seconds)
    return self._format_timestamp(predicted)

  def _get_spool_id_for_filament(self, filament_index: int) -> int | None:
    mapping_value = self._resolve_tray_mapping(filament_index)
    if mapping_value is None:
      return None
    tray_uid = self._tray_uid_from_mapping(mapping_value)
    if tray_uid is None:
      return None
    return self._lookup_spool_for_tray(tray_uid)

  def _maybe_update_predicted_total(self) -> None:
    if self._layer_tracking_predicted_total is not None:
      return
    if not self._total_usage_mm_per_filament:
      return

    total_grams = 0.0

    for filament, total_mm in self._total_usage_mm_per_filament.items():
      spool_id = self._filament_spool_id_map.get(filament)
      if spool_id is None:
        spool_id = self._get_spool_id_for_filament(filament)
        if spool_id is None:
          return
        self._filament_spool_id_map[filament] = spool_id

      spool_data = self._spool_data_cache.get(spool_id)
      if spool_data is None:
        spool_data = self._get_spool_data(spool_id)
        if spool_data is None:
          return
        self._spool_data_cache[spool_id] = spool_data

      filament_info = spool_data.get("filament", {})
      diameter_mm = filament_info.get("diameter", 1.75)
      density = filament_info.get("density", 1.24)
      total_grams += self._mm_to_grams(total_mm, diameter_mm, density)

    self._layer_tracking_predicted_total = total_grams
    if self.print_id:
      update_layer_tracking(self.print_id, filament_grams_total=round(total_grams, 2))

  def _bind_initial_spools(self) -> None:
    if not self.print_id:
      return

    for filament_index in self._total_usage_mm_per_filament:
      spool_id = self._get_spool_id_for_filament(filament_index)
      if spool_id is None:
        continue

      mapping_value = self._resolve_tray_mapping(filament_index)
      bind_filament_usage_spool(
        self.print_id,
        filament_index + 1,
        spool_id,
        mapping_value if mapping_value is not None and mapping_value != EXTERNAL_SPOOL_ID else None,
      )
      self._filament_spool_id_map[filament_index] = spool_id

      if spool_id not in self._spool_data_cache:
        spool_data = self._get_spool_data(spool_id)
        if spool_data is not None:
          self._spool_data_cache[spool_id] = spool_data

  def _update_layer_tracking_progress(self) -> None:
    if not self.print_id:
      return

    layers_printed = max(self.spent_layers) if self.spent_layers else 0
    grams_used = sum(self.cumulative_grams_used.values())

    payload = {
        "layers_printed": layers_printed,
        "filament_grams_billed": round(grams_used, 2),
    }
    if self._layer_tracking_total_layers is not None:
      payload["total_layers"] = self._layer_tracking_total_layers
    if self._layer_tracking_predicted_total is not None:
      payload["filament_grams_total"] = round(self._layer_tracking_predicted_total, 2)
    predicted_end = None
    if self._mc_remaining_time_minutes is not None and self._mc_remaining_time_minutes > 0:
      predicted_end = self._format_timestamp(
          datetime.now() + timedelta(minutes=self._mc_remaining_time_minutes)
      )
    else:
      predicted_end = self._compute_predicted_end_time(layers_printed, self._layer_tracking_total_layers)
    if predicted_end:
      payload["predicted_end_time"] = predicted_end

    update_layer_tracking(self.print_id, **payload)

  def _set_layer_tracking_status(
      self,
      status: str,
      target_print_id: int | None = None,
      extra_fields: dict | None = None,
  ) -> None:
    print_id = target_print_id or self.print_id
    if not print_id:
      return
    if target_print_id is None and self._layer_tracking_status == status:
      return
    payload = {"status": status}
    if extra_fields:
      payload.update(extra_fields)
    update_layer_tracking(print_id, **payload)
    if target_print_id is None:
      self._layer_tracking_status = status

  def _tray_uid_from_mapping(self, mapping_value: int) -> str | None:
    if mapping_value == EXTERNAL_SPOOL_ID:
      return trayUid(EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID)

    ams_id = getAMSFromTray(mapping_value)
    tray_id = mapping_value - ams_id * 4
    return trayUid(ams_id, tray_id)

  def _resolve_tray_mapping(self, filament_index: int) -> int | None:
    if self.using_ams:
      if self.ams_mapping is None or filament_index >= len(self.ams_mapping):
        log(f"No AMS mapping for filament {filament_index}")
        return None
      return self.ams_mapping[filament_index]
    return EXTERNAL_SPOOL_ID

  def _lookup_spool_for_tray(self, tray_uid: str):
    for spool in fetchSpools():
      extra = spool.get("extra") or {}
      active = extra.get("active_tray")
      if not active:
        continue
      try:
        active_value = json.loads(active)
      except Exception:
        active_value = active
      if active_value == tray_uid:
        return spool.get("id")
    return None

  def _get_spool_data(self, spool_id: int):
    # Get full spool data including filament information.
    for spool in fetchSpools():
      if spool.get("id") == spool_id:
        return spool
    return None

  def _load_model(self, model_path: str, gcode_file: str | None) -> None:
    gcode = extract_gcode_from_3mf(model_path, gcode_file)
    if gcode is None:
      log("Failed to extract gcode from model")
      return
    self.active_model = evaluate_gcode(gcode)

  @staticmethod
  def _active_ams_tray(print_obj: dict) -> int | None:
    # Return the physical AMS tray currently selected by the printer.
    #
    #     ``vt_tray.id=254`` is the separate external-spool channel and must not be
    #     used as the active AMS mapping.  Only a real AMS tray index (0..15) is
    #     accepted here.
    ams = print_obj.get("ams") or {}
    value = ams.get("tray_tar")
    try:
      tray = int(value)
    except (TypeError, ValueError):
      return None
    return tray if 0 <= tray < 16 else None

  def _apply_current_ams_tray(self, print_obj: dict) -> None:
    # Keep the logical filament mapped to the physical tray the printer is
    # currently using.  During AMS runout recovery, Bambu can switch to another
    # tray that is not present in the original 3MF mapping.
    if self.active_model is None:
      return
    tray = self._active_ams_tray(print_obj)
    if tray is None:
      return

    mapping = list(self.ams_mapping or [])
    if tray in mapping:
      return

    layer = print_obj.get("layer_num", self.current_layer)
    try:
      layer_usage = self.active_model.get(int(layer), {}) if layer is not None else {}
    except (TypeError, ValueError):
      layer_usage = {}
    active_indexes = []
    for index in layer_usage:
      try:
        logical_index = int(index)
      except (TypeError, ValueError):
        continue
      if logical_index >= 0:
        active_indexes.append(logical_index)

    if len(active_indexes) == 1:
      filament_index = active_indexes[0]
    else:
      # When the active layer contains multiple logical filaments, only remap
      # if the model has exactly one filament mapped to a physical AMS tray.
      mapped_indexes = [
        index for index, mapped_tray in enumerate(mapping)
        if mapped_tray is not None and mapped_tray != EXTERNAL_SPOOL_ID
      ]
      if len(mapped_indexes) != 1:
        log(
          f"[filament-tracker] AMS tray changed to {tray}, but active filament is ambiguous; "
          f"layer={layer!r}, candidates={active_indexes}, mapping={mapping}"
        )
        return
      filament_index = mapped_indexes[0]

    while len(mapping) <= filament_index:
      mapping.append(None)
    previous_tray = mapping[filament_index]
    mapping[filament_index] = tray
    log(
      f"[filament-tracker] AMS tray switch detected: filament={filament_index}, "
      f"tray={previous_tray} -> {tray}; remapping usage to the active spool"
    )
    self.apply_ams_mapping(mapping)

  def _attempt_print_resume(self, task_id, subtask_id, model_url=None, print_obj=None) -> None:
    print_obj = print_obj or {}
    try:
      from jobs_3mf import JOBS_3MF, make_job_key
      job_key = make_job_key(task_id, subtask_id)
      worker_status = JOBS_3MF.get(job_key) if job_key is not None else JOBS_3MF.get_active()
      job_state = worker_status.get("state")
      if job_state in {"queued", "resolving", "downloading", "processing"}:
        log(
          f"[filament-tracker] Resume wartet auf zentralen 3MF-Worker: "
          f"{worker_status.get('job_key') or job_key} ({job_state})"
        )
        return
      if job_state == "ready" and worker_status.get("local_path"):
        try:
          import mqtt_bambulab
          metadata = getattr(mqtt_bambulab, "PENDING_PRINT_METADATA", None)
          if metadata and self.print_id == metadata.get("print_id"):
            self.start_tracking_from_cached_model(
              metadata, worker_status["local_path"], print_obj
            )
            return
        except Exception as worker_error:
          log(f"[filament-tracker] Caches 3MF konnte nicht aktiviert werden: {worker_error!r}")
    except Exception as worker_error:
      log(f"[filament-tracker] 3MF-Workerstatus nicht verfügbar: {worker_error}")
    if self.print_id is None:
      from print_history import find_active_print_for_printer_job
      from jobs_3mf import make_job_key
      candidate = find_active_print_for_printer_job(
        print_obj.get("subtask_name"), print_obj.get("gcode_file"), model_url,
        job_key=make_job_key(task_id, subtask_id),
      )
      self.print_id = int(candidate["id"]) if candidate else None
      if self.print_id is None:
        log("[filament-tracker] Kein passender aktiver Druck; alte History wird nicht wiederverwendet.")
        return
    checkpoint_metadata = _get_checkpoint_metadata()
    previous_version = checkpoint_metadata.get("checkpoint_version")
    if checkpoint_metadata and previous_version != CHECKPOINT_VERSION:
      log(
        f"[filament-tracker] Checkpoint-Version {previous_version!r} ist veraltet; "
        "Metadaten werden verworfen, die 3MF bleibt nur zur Diagnose liegen."
      )
      clear_checkpoint(preserve_models=False)
      checkpoint_metadata = {}
    # Never take the source URL from an unverified/old checkpoint.
    if checkpoint_metadata and (
      _same_job_id(checkpoint_metadata.get("task_id"), task_id)
      or _same_job_id(checkpoint_metadata.get("subtask_id"), subtask_id)
    ):
      model_url = model_url or checkpoint_metadata.get("model_url")
    log(
      f"[filament-tracker] Resume diagnostics: print_id={self.print_id}, "
      f"checkpoint_version={checkpoint_metadata.get('checkpoint_version')!r}, "
      f"model_url_present={bool(model_url)}"
    )
    result = recover_model(task_id, subtask_id)
    if result is None:
      log(
        "[filament-tracker] No checkpoint to recover; attempting reconstruction "
        f"from current printer status (print_id={self.print_id})"
      )
      if self.print_id is None:
        log("[filament-tracker] Resume reconstruction aborted: no matching history entry")
        return

      gcode_file = print_obj.get("gcode_file")
      if not model_url and gcode_file:
        model_url = f"ftp://{str(gcode_file).lstrip('/')}"
      log(
        f"[filament-tracker] Resume reconstruction input: model_url={model_url!r}, "
        f"gcode_file={gcode_file!r}, layer={print_obj.get('layer_num')!r}, "
        f"total_layers={print_obj.get('total_layer_num')!r}"
      )
      model_path = self._retrieve_model(model_url)
      if model_path is None:
        log("[filament-tracker] Resume reconstruction failed: 3MF could not be retrieved")
        return

      # Recover the user-facing name from the downloaded 3MF before the
      # temporary model is consumed by the layer tracker.
      recovered_metadata = getMetaDataFromLocal3mf(model_path, model_url)
      recovered_name = (
        recovered_metadata.get("source_file_name")
        or print_obj.get("snapshot_name")
        or print_obj.get("subtask_name")
        or recovered_metadata.get("model_name")
      )
      if self.print_id and recovered_name:
        update_print_file_name(self.print_id, recovered_name)

      ams_mapping = print_obj.get("ams_mapping") or []
      current_tray = self._active_ams_tray(print_obj)
      if not ams_mapping and current_tray is not None:
        ams_mapping = [current_tray]
        log(f"[filament-tracker] Resume AMS mapping from current tray: filament 0 -> tray {current_tray}")
      use_ams = bool(ams_mapping) or bool(print_obj.get("use_ams", False))
      self._start_layer_tracking_for_model(
        model_path=model_path,
        gcode_file_name=print_obj.get("param"),
        model_file_name=os.path.basename(str(model_url or model_path).replace("\\", "/")),
        use_ams=use_ams,
        ams_mapping=ams_mapping if use_ams else None,
        task_id=task_id,
        subtask_id=subtask_id,
      )
      if self.active_model is None:
        log("[filament-tracker] Resume reconstruction failed: 3MF contains no usable G-code")
        return

      try:
        current_layer = max(0, int(print_obj.get("layer_num") or 0))
      except (TypeError, ValueError):
        current_layer = 0
      self.spent_layers = set(range(current_layer + 1))
      self.current_layer = current_layer
      update_checkpoint_layer(current_layer)
      self._update_layer_tracking_progress()
      log(
        f"[filament-tracker] Resume reconstruction complete: print_id={self.print_id}, "
        f"current_layer={current_layer}, total_layers={self._layer_tracking_total_layers}, "
        f"checkpoint_saved=True"
      )
      return
    log(f"[filament-tracker] Recovering from checkpoint task={task_id} subtask={subtask_id}")
    model_path, gcode_file_name, current_layer, ams_mapping = result
    checkpoint_layer = max(0, int(current_layer))
    try:
      printer_layer = max(0, int(print_obj.get("layer_num") or 0))
    except (TypeError, ValueError):
      printer_layer = checkpoint_layer
    current_layer = max(checkpoint_layer, printer_layer)
    if current_layer != checkpoint_layer:
      log(
        f"[filament-tracker] Checkpoint-Layer an Druckerstand angepasst: "
        f"{checkpoint_layer} -> {current_layer}; keine rückwirkende Doppelbuchung"
      )
    current_tray = self._active_ams_tray(print_obj)
    if not ams_mapping and current_tray is not None:
      ams_mapping = [current_tray]
      log(f"[filament-tracker] Checkpoint AMS mapping from current tray: filament 0 -> tray {current_tray}")
    self._load_model(model_path, gcode_file_name)
    if self.print_id:
      _restore_thumbnail(model_path, self.print_id)
    self.spent_layers = set(range(current_layer + 1))
    self.ams_mapping = ams_mapping
    self.current_layer = current_layer
    self.using_ams = ams_mapping is not None
    
    # Initialize cumulative usage from database to continue tracking correctly
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}
    if self.print_id:
      existing_usage = get_all_filament_usage_for_print(self.print_id)
      for ams_slot, usage in existing_usage.items():
        grams_value = usage.get("grams_used") if isinstance(usage, dict) else usage
        length_value = usage.get("length_used") if isinstance(usage, dict) else None
        if grams_value is not None:
          self.cumulative_grams_used[ams_slot] = grams_value
        if length_value is not None:
          self.cumulative_length_used[ams_slot] = length_value
        log(f"[filament-tracker] Resumed cumulative usage for filament {ams_slot}: {grams_value}g, {length_value or 0}mm")
    self._update_layer_tracking_progress()
