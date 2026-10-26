import os
import time
import re
import builtins
import threading
from datetime import datetime


_pending_ams_tray = None
_pending_ams_uid = None
_live_line_open = False
_recent_log_lock = threading.Lock()
_recent_log_entries = {}
_DUPLICATE_LOG_WINDOW_SECONDS = 2.0
_ZERO_AMS_UID = "00000000000000000000000000000000"
_APPLICATION_ROOT = os.path.dirname(os.path.abspath(__file__))


def application_log_file(filename: str) -> str:
    # Return a log path that works from the repository and in Docker.
    return os.path.join(_APPLICATION_ROOT, "logs", filename)


def append_to_rotating_file(file_path: str, text: str, max_size: int = 1_048_576, max_files: int = 5) -> None:
    #
    # Appends the given text with a timestamp to a rotating log file.
    # If the file exceeds the maximum size, it is renamed with a timestamp, and a new file is created.
    # If the maximum number of log files is reached, the oldest file matching the exact naming pattern is deleted.
    directory, base_filename = os.path.split(file_path)
    base_filename = os.path.splitext(base_filename)[0]
    os.makedirs(directory, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} :: {text}\n"

    # Rotate the file if it exceeds the size limit
    if os.path.exists(file_path) and os.path.getsize(file_path) > max_size:
        archive_filename = f"{base_filename}_{time.strftime('%Y%m%d_%H%M%S')}.log"
        archived_file = os.path.join(directory, archive_filename)
        os.rename(file_path, archived_file)

    # Append the text with timestamp to the current file
    with open(file_path, "a", encoding="utf-8") as file:
        file.write(log_entry)

    # Find all log files that exactly match the expected pattern
    pattern = re.compile(rf"^{re.escape(base_filename)}_\d{{8}}_\d{{6}}\.log$")
    log_files = sorted(
        [f for f in os.listdir(directory) if pattern.match(f)],
        key=lambda f: os.path.getctime(os.path.join(directory, f))  # Sort by creation time
    )

    while len(log_files) > max_files:
        os.remove(os.path.join(directory, log_files.pop(0)))  # Remove the oldest file


def _is_real_ams_uid(uid) -> bool:
    value = str(uid or "").strip()
    return bool(value and value != _ZERO_AMS_UID and any(ch != "0" for ch in value))


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_local_spool(spool_id):
    try:
        # Imported lazily to avoid a logger <-> mqtt_bambulab import cycle.
        import mqtt_bambulab

        for spool in mqtt_bambulab.fetchSpools(True) or []:
            if str(spool.get("id")) == str(spool_id):
                return spool
    except Exception:
        pass
    return None


def _remaining_text(spool) -> str:
    if not isinstance(spool, dict):
        return ""

    remaining = _as_float(spool.get("remaining_weight"))
    used = _as_float(spool.get("used_weight"))
    filament = spool.get("filament") or {}
    full_weight = _as_float(filament.get("weight"))

    # The filament weight is the nominal net filament weight. Older or
    # incomplete records may omit it; remaining + used still gives a useful
    # original net amount in that case.
    if (full_weight is None or full_weight <= 0) and remaining is not None and used is not None:
        full_weight = remaining + used

    if remaining is None:
        return ""

    remaining_g = int(round(remaining))
    if full_weight is None or full_weight <= 0:
        return f" | Rest: {remaining_g} g"

    full_g = int(round(full_weight))
    percent = int(round(max(0.0, min(100.0, remaining / full_weight * 100.0))))
    return f" | Rest: {remaining_g} g / {full_g} g ({percent} %)"


def _uid_suffix(uid) -> str:
    return f" [{uid}]" if _is_real_ams_uid(uid) else ""


def _format_ams_console_message(text):
    # Collapse verbose Bambu tray diagnostics into one local-catalog line.
    #
    #     Raw Bambu tray color/remain values are intentionally hidden. The local
    #     catalog is authoritative for the assigned spool and remaining amount. A real RFID UID
    #     is retained only as a final debug suffix; the all-zero non-RFID UID is not
    #     displayed.
    global _pending_ams_tray, _pending_ams_uid

    # app_custom currently translates the raw Bambu humidity into one of these
    # human-readable variants before forwarding it to this logger. Normalize all
    # variants to the explicit Bambu humidity index wording.
    match = re.match(
        r"^AMS \[([A-Z])\] \(hum: [^,]+%, level: ([^,]+), temp: (.+)\)$",
        text,
    )
    if match:
        _pending_ams_tray = None
        _pending_ams_uid = None
        return f"AMS [{match.group(1)}] (humidity_index: {match.group(2)}, temp: {match.group(3)})"

    match = re.match(
        r"^AMS \[([A-Z])\] \(humidity level: ([^,]+), temp: (.+)\)$",
        text,
    )
    if match:
        _pending_ams_tray = None
        _pending_ams_uid = None
        return f"AMS [{match.group(1)}] (humidity_index: {match.group(2)}, temp: {match.group(3)})"

    match = re.match(
        r"^AMS \[([A-Z])\] \(hum: ([^,]+), temp: (.+)\)$",
        text,
    )
    if match:
        _pending_ams_tray = None
        _pending_ams_uid = None
        return f"AMS [{match.group(1)}] (humidity_index: {match.group(2)}, temp: {match.group(3)})"

    # Raw tray header. app_custom may already have removed (-01%) and a zero
    # UUID, so accept both the original and cleaned forms. Suppress this line;
    # the following local-catalog result is emitted as the single authoritative row.
    tray_match = re.match(r"^\s*- \[([A-Z]\d+)\](.*)$", text)
    if tray_match:
        remainder = tray_match.group(2) or ""
        uid_match = re.search(r"\[\[\s*([^\]]+?)\s*\]\]", remainder)
        _pending_ams_tray = tray_match.group(1)
        _pending_ams_uid = uid_match.group(1).strip() if uid_match else None
        return None

    spool_match = re.match(r"^\s*- (?:Local )?Spool #(\d+):\s*(.*)$", text)
    if spool_match and _pending_ams_tray:
        spool_id = spool_match.group(1)
        description = spool_match.group(2).strip() or "zugeordnet"
        spool = _find_local_spool(spool_id)
        output = (
            f"    - [{_pending_ams_tray}] #{spool_id}: {description}"
            f"{_remaining_text(spool)}{_uid_suffix(_pending_ams_uid)}"
        )
        _pending_ams_tray = None
        _pending_ams_uid = None
        return output

    if _pending_ams_tray and (
        re.match(r"^\s*- Keine Spule diesem Tray zugeordnet\.$", text)
        or re.match(r"^\s*- Keine Spule$", text)
        or re.match(r"^\s*- Fach leer$", text)
        or re.match(r"^\s*- ---$", text)
        or re.match(r"^\s*- No Spool!$", text)
        or re.match(r"^\s*- Not found\. Update spool tag!$", text)
    ):
        if re.match(r"^\s*- Fach leer$", text) or re.match(r"^\s*- No Spool!$", text):
            status = "Fach leer"
        else:
            status = "Keine Spule"
        output = (
            f"    - [{_pending_ams_tray}] {status}"
            f"{_uid_suffix(_pending_ams_uid)}"
        )
        _pending_ams_tray = None
        _pending_ams_uid = None
        return output

    return text


def log_with_timestamp(*args, sep=" ", end="\n", file=None, flush=True) -> None:
    #
    # Print a message with a leading timestamp, preserving the standard print API.
    global _live_line_open, _recent_log_entries
    if _live_line_open:
        builtins.print()
        _live_line_open = False
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # AMS console output is produced as one string per log call. Keep all other
    # logger usage byte-for-byte equivalent to the previous print behavior.
    if file is None and len(args) == 1 and isinstance(args[0], str):
        formatted = _format_ams_console_message(args[0])
        if formatted is None:
            return
        args = (formatted,)

    # MQTT reconnects can replay the same status packet immediately. Avoid
    # filling the logbook with byte-identical entries while preserving later
    # legitimate repeats. The key deliberately includes the complete rendered
    # message, so different bookings are never collapsed.
    if file is None and end == "\n":
        dedup_key = (sep, tuple(str(value) for value in args))
        now = time.monotonic()
        with _recent_log_lock:
            previous = _recent_log_entries.get(dedup_key)
            _recent_log_entries[dedup_key] = now
            if previous is not None and now - previous <= _DUPLICATE_LOG_WINDOW_SECONDS:
                return
            if len(_recent_log_entries) > 2048:
                cutoff = now - _DUPLICATE_LOG_WINDOW_SECONDS
                _recent_log_entries = {
                    key: timestamp
                    for key, timestamp in _recent_log_entries.items()
                    if timestamp >= cutoff
                }

    builtins.print(f"[{timestamp}]", *args, sep=sep, end=end, file=file, flush=flush)


# Alias for brevity where logging-style prints are used
log = log_with_timestamp


def mark_live_line_open() -> None:
    # Mark that a live progress indicator currently occupies the line.
    global _live_line_open
    _live_line_open = True
