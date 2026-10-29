"""Local, normalized spool inventory stored in the shared m0BamboSpool DB."""

import sqlite3
import json
import threading
from functools import wraps
from datetime import datetime, timezone
from typing import Any

from config import DATABASE_PATH, DATABASE_TYPE
from filament_color_catalog import SUNLU_COLOR_OPTIONS
from ui_formatting import LOCAL_TIMEZONE

DEFAULT_MATERIALS = (
    "PLA", "PLA+", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "HIPS",
)

_INITIALIZE_LOCK = threading.RLock()


def _synchronized_initialize(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with _INITIALIZE_LOCK:
            return function(*args, **kwargs)
    return wrapper


class _ClosingConnection(sqlite3.Connection):
    # sqlite's context manager commits or rolls back but does not close the
    # connection; close it here so temporary databases can be removed safely.
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    if DATABASE_TYPE != "sqlite":
        raise RuntimeError(f"Unsupported inventory database type: {DATABASE_TYPE}")
    path = DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _now() -> str:
    return datetime.now(LOCAL_TIMEZONE).isoformat(timespec="seconds")


def _number(value: Any) -> float:
    """Accept both German decimal commas and decimal points from the UI."""
    if isinstance(value, str):
        value = value.strip().replace(" ", "")
        comma = value.rfind(",")
        point = value.rfind(".")
        if comma >= 0 and point >= 0:
            value = value.replace(".", "").replace(",", ".") if comma > point else value.replace(",", "")
        elif comma >= 0:
            value = value.replace(",", ".")
    return float(value)


def _date_only(value: Any) -> str | None:
    text = str(value or "").strip().replace("T", " ")
    return text.split(" ", 1)[0] if text else None


def _temperature_range(value: Any) -> tuple[int | None, int | None]:
    # Normalize legacy single/range values into the two printer fields.
    if value in (None, ""):
        return None, None
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
    if isinstance(current, (list, tuple)):
        values = list(current)
        if len(values) >= 2:
            return int(float(values[0])), int(float(values[1]))
        if len(values) == 1:
            value = values[0]
        else:
            return None, None
    text = str(current).strip().strip("[]()")
    parts = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
    if len(parts) >= 2:
        return int(float(parts[0])), int(float(parts[1]))
    try:
        number = int(float(parts[0] if parts else text))
    except (TypeError, ValueError):
        return None, None
    return number, number


def _normalize_hex(value: Any) -> str:
    raw = str(value or "").strip().lstrip("#")
    if len(raw) != 6 or any(char not in "0123456789abcdefABCDEF" for char in raw):
        raise ValueError("Ungültiger Farbcode")
    return raw.upper()


def _rgb_components(hex_code: Any) -> tuple[int, int, int]:
    normalized = _normalize_hex(hex_code)
    return int(normalized[0:2], 16), int(normalized[2:4], 16), int(normalized[4:6], 16)


def _rgb_hex(values: dict[str, Any]) -> str:
    components = []
    for field in ("red", "green", "blue"):
        raw = values.get(field)
        if raw in (None, ""):
            return ""
        number = _number(raw)
        if int(number) != number or number < 0 or number > 255:
            raise ValueError("RGB-Werte müssen ganze Zahlen von 0 bis 255 sein")
        components.append(int(number))
    return "".join(f"{component:02X}" for component in components)


def _canonical_nfc_value(value: Any) -> str:
    return str(value or "").strip().upper().replace(":", "-")


@_synchronized_initialize
def initialize() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vendors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS colors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                hex_code TEXT NOT NULL COLLATE NOCASE,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS filaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor_id INTEGER REFERENCES vendors(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                material_id INTEGER REFERENCES materials(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                name TEXT NOT NULL,
                color_id INTEGER REFERENCES colors(id) ON UPDATE CASCADE ON DELETE SET NULL,
                diameter REAL NOT NULL DEFAULT 1.75,
                weight REAL,
                spool_weight REAL,
                price REAL,
                nozzle_temp_min INTEGER,
                nozzle_temp_max INTEGER,
                cali_idx INTEGER,
                bambu_filament_id TEXT,
                bambu_setting_id TEXT,
                comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS spools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filament_id INTEGER NOT NULL REFERENCES filaments(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                price REAL,
                archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                initial_weight REAL,
                spool_weight REAL,
                weight_correction REAL NOT NULL DEFAULT 0,
                nfc_tag_id INTEGER,
                active_tray TEXT,
                active_tray_source TEXT,
                active_tray_nfc_uid TEXT,
                purchase_date TEXT,
                comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS nfc_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_value TEXT NOT NULL COLLATE NOCASE UNIQUE,
                comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS spool_consumption_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                spool_id INTEGER NOT NULL REFERENCES spools(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                weight_grams REAL,
                length_mm REAL,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
            );
        """)
        for table in ("vendors", "materials", "colors", "filaments", "spools", "nfc_tags", "spool_consumption_events"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "deleted" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        for table in ("spool_usage", "filament_usage_events", "prints", "print_layer_tracking", "runtime_metadata", "humidity_history", "nfc_pending_tags"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "deleted" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        # Remove the obsolete one-time import marker from older installations.
        conn.execute("DROP TABLE IF EXISTS inventory_migrations")
        # Older databases declared the hex value UNIQUE.  Hex values describe
        # a color only; multiple named colors may intentionally share one.
        color_indexes = conn.execute("PRAGMA index_list(colors)").fetchall()
        hex_unique = False
        for index in color_indexes:
            if not index[2]:
                continue
            index_columns = [row[2] for row in conn.execute(f'PRAGMA index_info("{index[1]}")')]
            if "hex_code" in index_columns:
                hex_unique = True
                break
        if hex_unique:
            # Recreate dependent views after the table swap so SQLite does not
            # retain a view definition pointing at the temporary table.
            conn.execute("DROP VIEW IF EXISTS v_filaments")
            conn.execute("DROP VIEW IF EXISTS v_spool_usage")
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            conn.execute("""
                CREATE TABLE colors_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    hex_code TEXT NOT NULL COLLATE NOCASE,
                    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
                )
            """)
            conn.execute(
                "INSERT INTO colors_new(id, name, hex_code, deleted) SELECT id, name, hex_code, deleted FROM colors"
            )
            conn.execute("DROP TABLE colors")
            conn.execute("ALTER TABLE colors_new RENAME TO colors")
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
        spool_columns = {row["name"] for row in conn.execute("PRAGMA table_info(spools)")}
        if "nfc_tag_id" not in spool_columns:
            conn.execute("ALTER TABLE spools ADD COLUMN nfc_tag_id INTEGER")
        if "active_tray_source" not in spool_columns:
            conn.execute("ALTER TABLE spools ADD COLUMN active_tray_source TEXT")
        if "active_tray_nfc_uid" not in spool_columns:
            conn.execute("ALTER TABLE spools ADD COLUMN active_tray_nfc_uid TEXT")
        if "tag" in spool_columns:
            # Migrate the former inline tag values into the NFC catalog before
            # removing the redundant column from the spool table.
            for legacy in conn.execute("SELECT id, tag, nfc_tag_id FROM spools WHERE tag IS NOT NULL AND trim(tag) <> ''").fetchall():
                if legacy["nfc_tag_id"]:
                    continue
                tag_value = _canonical_nfc_value(legacy["tag"])
                if not tag_value:
                    continue
                now = _now()
                conn.execute(
                    "INSERT INTO nfc_tags(tag_value, comment, created_at, updated_at) VALUES (?, NULL, ?, ?) "
                    "ON CONFLICT(tag_value) DO UPDATE SET updated_at = excluded.updated_at",
                    (tag_value, now, now),
                )
                tag_row = conn.execute("SELECT id FROM nfc_tags WHERE tag_value = ?", (tag_value,)).fetchone()
                if tag_row:
                    conn.execute("UPDATE spools SET nfc_tag_id = ? WHERE id = ?", (int(tag_row["id"]), int(legacy["id"])))
            conn.execute("DROP INDEX IF EXISTS ux_spools_tag")
            try:
                conn.execute("ALTER TABLE spools DROP COLUMN tag")
            except sqlite3.OperationalError:
                # Older SQLite versions keep the legacy column physically, but
                # all application queries ignore it after this migration.
                pass
            spool_columns.discard("tag")
        nfc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(nfc_tags)")}
        if "comment" not in nfc_columns:
            conn.execute("ALTER TABLE nfc_tags ADD COLUMN comment TEXT")
        if "spool_id" in nfc_columns:
            # Move the legacy reverse reference into the spool-owned foreign
            # key before removing that column from the NFC catalog.
            conn.execute(
                "UPDATE spools SET nfc_tag_id = ("
                "SELECT nt.id FROM nfc_tags nt WHERE nt.spool_id = spools.id) "
                "WHERE nfc_tag_id IS NULL AND EXISTS ("
                "SELECT 1 FROM nfc_tags nt WHERE nt.spool_id = spools.id)"
            )
            conn.execute("DROP INDEX IF EXISTS idx_nfc_tags_spool_id")
            try:
                conn.execute("ALTER TABLE nfc_tags DROP COLUMN spool_id")
            except sqlite3.OperationalError:
                # Older SQLite versions may not support DROP COLUMN.  The
                # application still ignores the legacy column when present.
                pass
            nfc_columns.discard("spool_id")
        # Do not allocate AUTOINCREMENT values for rows that already exist.
        existing_materials = {
            str(row[0]).casefold()
            for row in conn.execute("SELECT name FROM materials")
        }
        for name in DEFAULT_MATERIALS:
            key = str(name).casefold()
            if key not in existing_materials:
                conn.execute("INSERT INTO materials(name) VALUES (?)", (name,))
                existing_materials.add(key)
        # The initial palette is kept locally so every color selector uses the
        # same editable source instead of a hard-coded template list.
        existing_color_names = {
            str(row[0]).casefold() for row in conn.execute("SELECT name FROM colors")
        }
        for name, hex_code in SUNLU_COLOR_OPTIONS:
            normalized_hex = str(hex_code).replace("#", "").upper()
            name_key = str(name).casefold()
            if name_key not in existing_color_names:
                conn.execute(
                    "INSERT INTO colors(name, hex_code) VALUES (?, ?)",
                    (name, normalized_hex),
                )
                existing_color_names.add(name_key)
        # Keep migrations compatible with osm.db files created before Bambu
        # profile IDs became part of the internal filament record.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(filaments)")}
        if "color_id" not in columns:
            conn.execute("ALTER TABLE filaments ADD COLUMN color_id INTEGER REFERENCES colors(id) ON UPDATE CASCADE ON DELETE SET NULL")
            columns.add("color_id")
        if "color_hex" in columns:
            legacy_colors = [row["color_hex"] for row in conn.execute("SELECT DISTINCT color_hex FROM filaments WHERE color_hex IS NOT NULL AND trim(color_hex) <> ''")]
            for raw_hex in legacy_colors:
                normalized = str(raw_hex).strip().lstrip("#").upper()
                if not normalized:
                    continue
                color = conn.execute("SELECT id FROM colors WHERE upper(replace(hex_code, '#', '')) = ?", (normalized,)).fetchone()
                if color is None:
                    cursor = conn.execute("INSERT INTO colors(name, hex_code) VALUES (?, ?)", (f"Farbe #{normalized}", normalized))
                    color_id = cursor.lastrowid
                else:
                    color_id = color["id"]
                conn.execute("UPDATE filaments SET color_id = ? WHERE upper(replace(color_hex, '#', '')) = ?", (color_id, normalized))
            conn.execute("ALTER TABLE filaments DROP COLUMN color_hex")
            columns.remove("color_hex")
        if "diameter" not in columns:
            conn.execute("ALTER TABLE filaments ADD COLUMN diameter REAL NOT NULL DEFAULT 1.75")
            columns.add("diameter")
        if "density" in columns:
            conn.execute("ALTER TABLE filaments DROP COLUMN density")
            columns.remove("density")
        if "nozzle_temp_min" not in columns:
            conn.execute("ALTER TABLE filaments ADD COLUMN nozzle_temp_min INTEGER")
            columns.add("nozzle_temp_min")
        if "nozzle_temp_max" not in columns:
            conn.execute("ALTER TABLE filaments ADD COLUMN nozzle_temp_max INTEGER")
            columns.add("nozzle_temp_max")
        if "nozzle_temp" in columns:
            for row in conn.execute("SELECT id, nozzle_temp FROM filaments WHERE nozzle_temp IS NOT NULL").fetchall():
                minimum, maximum = _temperature_range(row["nozzle_temp"])
                conn.execute(
                    "UPDATE filaments SET nozzle_temp_min = COALESCE(nozzle_temp_min, ?), "
                    "nozzle_temp_max = COALESCE(nozzle_temp_max, ?) WHERE id = ?",
                    (minimum, maximum, row["id"]),
                )
        legacy_columns = [column for column in ("extruder_temp", "bed_temp", "nozzle_temp") if column in columns]
        if legacy_columns:
            # Drop views that use SELECT f.* before removing obsolete columns.
            conn.execute("DROP VIEW IF EXISTS v_filaments")
            conn.execute("DROP VIEW IF EXISTS v_spool_usage")
            for column in legacy_columns:
                try:
                    conn.execute(f"ALTER TABLE filaments DROP COLUMN {column}")
                    columns.remove(column)
                except sqlite3.OperationalError:
                    # Older SQLite versions may retain the legacy columns;
                    # all application queries still ignore them.
                    pass
        for column, definition in (
            ("bambu_filament_id", "TEXT"),
            ("bambu_setting_id", "TEXT"),
            ("weight", "REAL"),
            ("spool_weight", "REAL"),
            ("price", "REAL"),
            ("comment", "TEXT"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE filaments ADD COLUMN {column} {definition}")
        spool_columns = {row["name"] for row in conn.execute("PRAGMA table_info(spools)")}
        if "purchase_date" not in spool_columns:
            if "first_used" in spool_columns:
                try:
                    conn.execute("ALTER TABLE spools RENAME COLUMN first_used TO purchase_date")
                except sqlite3.OperationalError:
                    conn.execute("ALTER TABLE spools ADD COLUMN purchase_date TEXT")
                    conn.execute("UPDATE spools SET purchase_date = first_used")
            else:
                conn.execute("ALTER TABLE spools ADD COLUMN purchase_date TEXT")
        spool_columns = {row["name"] for row in conn.execute("PRAGMA table_info(spools)")}
        if "comment" not in spool_columns:
            conn.execute("ALTER TABLE spools ADD COLUMN comment TEXT")
        conn.execute(
            "UPDATE spools SET purchase_date = substr(replace(purchase_date, 'T', ' '), 1, 10) "
            "WHERE purchase_date IS NOT NULL AND trim(purchase_date) <> ''"
        )
        # Archived spools keep a snapshot of inherited filament defaults so
        # later master-data changes cannot alter their historical values.
        for archived_spool in conn.execute(
            """SELECT s.id, s.initial_weight, s.spool_weight, s.price,
                      f.weight AS filament_weight,
                      f.spool_weight AS filament_spool_weight,
                      f.price AS filament_price
                 FROM spools s
                 JOIN filaments f ON f.id = s.filament_id
                WHERE s.archived = 1"""
        ).fetchall():
            snapshot = {}
            for field, default_field in (
                ("initial_weight", "filament_weight"),
                ("spool_weight", "filament_spool_weight"),
                ("price", "filament_price"),
            ):
                if archived_spool[field] in (None, 0, 0.0) and archived_spool[default_field] is not None:
                    snapshot[field] = archived_spool[default_field]
            if snapshot:
                snapshot["updated_at"] = _now()
                conn.execute(
                    f"UPDATE spools SET {','.join(f'{field} = ?' for field in snapshot)} WHERE id = ?",
                    list(snapshot.values()) + [archived_spool["id"]],
                )
        conn.executescript("""
            DROP VIEW IF EXISTS v_filaments;
            CREATE VIEW v_filaments AS
            SELECT f.id, f.vendor_id, f.material_id, f.name, f.color_id,
                   f.diameter, f.weight, f.spool_weight, f.price,
                   f.nozzle_temp_min, f.nozzle_temp_max, f.cali_idx,
                   f.bambu_filament_id, f.bambu_setting_id, f.comment,
                   f.deleted,
                   f.created_at, f.updated_at,
                   c.name AS color_name, c.hex_code AS color_hex,
                   v.name AS vendor_name, m.name AS material_name
            FROM filaments f
            LEFT JOIN colors c ON c.id = f.color_id AND c.deleted = 0
            LEFT JOIN vendors v ON v.id = f.vendor_id AND v.deleted = 0
            LEFT JOIN materials m ON m.id = f.material_id AND m.deleted = 0
            WHERE f.deleted = 0;
        """)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spool_usage'").fetchone():
            conn.executescript("""
                DROP VIEW IF EXISTS v_spool_usage;
                CREATE VIEW v_spool_usage AS
                SELECT u.*, f.name AS filament_name, m.name AS material_name,
                       c.hex_code AS color_hex
                FROM spool_usage u
                LEFT JOIN spools s ON s.id = u.spool_id AND s.deleted = 0
                LEFT JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
                LEFT JOIN materials m ON m.id = f.material_id AND m.deleted = 0
                LEFT JOIN colors c ON c.id = f.color_id AND c.deleted = 0
                WHERE u.deleted = 0;
            """)


def _color_id(conn: sqlite3.Connection, value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and int(value) == value:
        return int(value)
    normalized = str(value).strip().lstrip("#").upper()
    row = conn.execute("SELECT id, deleted FROM colors WHERE upper(replace(hex_code, '#', '')) = ? ORDER BY deleted, id LIMIT 1", (normalized,)).fetchone()
    if row:
        if row["deleted"]:
            conn.execute("UPDATE colors SET deleted = 0 WHERE id = ?", (row["id"],))
        return int(row["id"])
    cursor = conn.execute("INSERT INTO colors(name, hex_code) VALUES (?, ?)", (f"Farbe #{normalized}", normalized))
    return int(cursor.lastrowid)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _extra_value(extras: dict[str, Any] | None, key: str) -> Any:
    import json
    value = (extras or {}).get(key)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _db_value(value: Any) -> Any:
    # SQLite accepts scalar values only; preserve structured values as JSON.
    if isinstance(value, (list, dict, tuple)):
        import json
        return json.dumps(value, ensure_ascii=False)
    return value


def normalize_nfc_tag(value: Any) -> str:
    # NFC UIDs are stored once in a canonical, case-insensitive form.
    return _canonical_nfc_value(value)


def _ensure_nfc_catalog_value(conn: sqlite3.Connection, value: Any) -> int | None:
    normalized = normalize_nfc_tag(value)
    if not normalized:
        return None
    now = _now()
    row = conn.execute("SELECT id, deleted FROM nfc_tags WHERE tag_value = ?", (normalized,)).fetchone()
    if row:
        conn.execute("UPDATE nfc_tags SET deleted = 0, updated_at = ? WHERE id = ?", (now, row["id"]))
    else:
        conn.execute(
            "INSERT INTO nfc_tags(tag_value, comment, created_at, updated_at) VALUES (?, NULL, ?, ?)",
            (normalized, now, now),
        )
        row = conn.execute("SELECT id FROM nfc_tags WHERE tag_value = ? AND deleted = 0", (normalized,)).fetchone()
    return int(row["id"]) if row else None


def _sync_nfc_tag_row(
    conn: sqlite3.Connection,
    tag_value: Any,
    comment: str | None = None,
) -> int | None:
    normalized = normalize_nfc_tag(tag_value)
    if not normalized:
        return None
    now = _now()
    row = conn.execute("SELECT id, deleted FROM nfc_tags WHERE tag_value = ?", (normalized,)).fetchone()
    if row:
        conn.execute(
            "UPDATE nfc_tags SET deleted = 0, comment = COALESCE(?, comment), updated_at = ? WHERE id = ?",
            (comment, now, row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO nfc_tags(tag_value, comment, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (normalized, comment, now, now),
        )
        row = conn.execute("SELECT id FROM nfc_tags WHERE tag_value = ? AND deleted = 0", (normalized,)).fetchone()
    return int(row["id"]) if row else None


def list_nfc_tags(include_assignment: bool = False) -> list[dict[str, Any]]:
    initialize()
    with _connect() as conn:
        # Existing spool tags are imported lazily as well, so older databases
        # immediately become visible in the new selector.
        _sync_nfc_catalog_from_spools(conn)
        if include_assignment:
            rows = conn.execute(
                """SELECT nt.id, nt.tag_value, nt.comment,
                          s.id AS spool_number, f.name AS filament_name,
                          nt.created_at, nt.updated_at
                   FROM nfc_tags nt
                   LEFT JOIN spools s ON s.nfc_tag_id = nt.id AND s.deleted = 0
                   LEFT JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
                   WHERE nt.deleted = 0
                   ORDER BY nt.id"""
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, tag_value, comment, created_at, updated_at "
                "FROM nfc_tags WHERE deleted = 0 ORDER BY id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["comment"] = str(item.get("comment") or "").strip() or None
            item["spool_label"] = (
                f"#{item['spool_number']} – {item['filament_name']}"
                if include_assignment and item.get("spool_number") and item.get("filament_name")
                else (f"#{item['spool_number']}" if item.get("spool_number") else None)
            )
            if not include_assignment:
                item.pop("spool_label", None)
            item["in_use"] = bool(item.get("spool_number"))
            # The selector uses a stable human-readable label without exposing
            # the raw tag as the value saved on a spool.
            fallback_label = f"NFC #{item['id']} – {item['tag_value']}"
            if include_assignment and item.get("spool_label"):
                fallback_label += f" – {item['spool_label']}"
            item["label"] = item["comment"] or fallback_label
            result.append(item)
        return result


def register_nfc_tag(tag_value: Any) -> str:
    normalized = normalize_nfc_tag(tag_value)
    if not normalized:
        raise ValueError("NFC tag value must not be empty")
    initialize()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM nfc_tags WHERE tag_value = ? COLLATE NOCASE",
            (normalized,),
        ).fetchone()
        if existing:
            # Reusing a previously retired tag makes it visible again.
            conn.execute(
                "UPDATE nfc_tags SET deleted = 0, updated_at = ? WHERE id = ?",
                (_now(), existing["id"]),
            )
        else:
            now = _now()
            conn.execute(
                "INSERT INTO nfc_tags(tag_value, comment, created_at, updated_at) VALUES (?, NULL, ?, ?)",
                (normalized, now, now),
            )
    return normalized


def update_nfc_tag(tag_id: int | str, values: dict[str, Any]) -> dict[str, Any]:
    initialize()
    fields = {}
    if "tag_value" in values:
        normalized = normalize_nfc_tag(values.get("tag_value"))
        if not normalized:
            raise ValueError("Der Tag-Wert darf nicht leer sein")
        fields["tag_value"] = normalized
    if "comment" in values:
        fields["comment"] = str(values.get("comment") or "").strip() or None
    if not fields:
        raise ValueError("Keine editierbaren NFC-Tag-Felder übergeben")
    with _connect() as conn:
        current = conn.execute("SELECT * FROM nfc_tags WHERE id = ? AND deleted = 0", (int(tag_id),)).fetchone()
        if not current:
            raise KeyError(f"Kein NFC-Tag mit ID {tag_id}")
        if "tag_value" in fields:
            assigned = conn.execute(
                "SELECT 1 FROM spools WHERE nfc_tag_id = ? AND deleted = 0 LIMIT 1",
                (int(tag_id),),
            ).fetchone()
            if assigned and fields["tag_value"] != normalize_nfc_tag(current["tag_value"]):
                raise ValueError("Der Tag-Wert kann nicht geändert werden, solange er einer Spule zugeordnet ist.")
            owner = conn.execute(
                "SELECT id FROM nfc_tags WHERE tag_value = ? COLLATE NOCASE AND id <> ? AND deleted = 0",
                (fields["tag_value"], int(tag_id)),
            ).fetchone()
            if owner:
                raise ValueError("Dieser NFC-Tag-Wert ist bereits bekannt")
        fields["updated_at"] = _now()
        conn.execute(
            f"UPDATE nfc_tags SET {','.join(f'{key} = ?' for key in fields)} WHERE id = ?",
            list(fields.values()) + [int(tag_id)],
        )
    return next(item for item in list_nfc_tags(include_assignment=True) if int(item["id"]) == int(tag_id))


def delete_nfc_tag(tag_id: int | str) -> None:
    initialize()
    with _connect() as conn:
        row = conn.execute("SELECT id FROM nfc_tags WHERE id = ? AND deleted = 0", (int(tag_id),)).fetchone()
        if not row:
            raise KeyError(f"Kein NFC-Tag mit ID {tag_id}")
        if conn.execute("SELECT 1 FROM spools WHERE nfc_tag_id = ? AND deleted = 0 LIMIT 1", (int(tag_id),)).fetchone():
            raise ValueError("Löschen gesperrt: Der NFC-Tag wird aktuell von einer Spule verwendet.")
        conn.execute("UPDATE nfc_tags SET deleted = 1, updated_at = ? WHERE id = ?", (_now(), int(tag_id)))


def _sync_nfc_catalog_from_spools(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, nfc_tag_id, archived FROM spools WHERE nfc_tag_id IS NOT NULL AND deleted = 0"
    ).fetchall()
    for row in rows:
        if row["nfc_tag_id"]:
            tag_row = conn.execute("SELECT tag_value FROM nfc_tags WHERE id = ?", (int(row["nfc_tag_id"]),)).fetchone()
            if tag_row:
                _sync_nfc_tag_row(conn, tag_row["tag_value"])


def list_entities(entity: str) -> list[dict[str, Any]]:
    initialize()
    if entity == "filaments":
        return list_filaments()
    tables = {"vendors": "vendors", "materials": "materials", "filaments": "filaments"}
    table = tables[entity]
    with _connect() as conn:
        if entity == "materials":
            rows = conn.execute(
                "SELECT m.*, EXISTS(SELECT 1 FROM filaments f WHERE f.material_id = m.id AND f.deleted = 0) AS in_use "
                "FROM materials m WHERE m.deleted = 0 ORDER BY m.name COLLATE NOCASE, m.id"
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM {table} WHERE deleted = 0 ORDER BY name COLLATE NOCASE, id").fetchall()
        return [dict(row) for row in rows]


def list_colors() -> list[dict[str, Any]]:
    initialize()
    with _connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT c.id, c.name, c.hex_code, "
            "EXISTS(SELECT 1 FROM filaments f WHERE f.color_id = c.id AND f.deleted = 0) AS in_use "
            "FROM colors c WHERE c.deleted = 0 ORDER BY c.name COLLATE NOCASE, c.id"
        )]
    for row in rows:
        row["hex_code"] = _normalize_hex(row["hex_code"])
        row["red"], row["green"], row["blue"] = _rgb_components(row["hex_code"])
    return rows


def list_filaments() -> list[dict[str, Any]]:
    initialize()
    with _connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM v_filaments ORDER BY name COLLATE NOCASE, id"
        )]


def ensure_local_filament_from_inventory(filament: dict[str, Any] | None) -> None:
    """Create a local editable mirror only when an active inventory filament is absent."""
    filament = filament or {}
    filament_id = filament.get("id")
    if filament_id is None:
        return
    initialize()
    now = _now()
    vendor = filament.get("vendor") or {}
    vendor_name = str(vendor.get("name") or "Unbekannt").strip()
    material_name = str(filament.get("material") or "Unbekannt").strip()
    with _connect() as conn:
        existing = conn.execute("SELECT id, deleted FROM filaments WHERE id = ?", (filament_id,)).fetchone()
        if existing:
            diameter = _db_value(filament.get("diameter"))
            conn.execute(
                "UPDATE filaments SET deleted = 0, diameter = COALESCE(?, diameter), updated_at = ? WHERE id = ?",
                (diameter, now, filament_id),
            )
            return
        vendor_row = conn.execute("SELECT id FROM vendors WHERE name = ? COLLATE NOCASE", (vendor_name,)).fetchone()
        if vendor_row:
            vendor_id = int(vendor_row["id"])
            conn.execute("UPDATE vendors SET deleted = 0 WHERE id = ?", (vendor_id,))
        else:
            cursor = conn.execute("INSERT INTO vendors(name) VALUES (?)", (vendor_name,))
            vendor_id = int(cursor.lastrowid)
        material_row = conn.execute("SELECT id FROM materials WHERE name = ? COLLATE NOCASE", (material_name,)).fetchone()
        if material_row:
            material_id = int(material_row["id"])
            conn.execute("UPDATE materials SET deleted = 0 WHERE id = ?", (material_id,))
        else:
            cursor = conn.execute("INSERT INTO materials(name) VALUES (?)", (material_name,))
            material_id = int(cursor.lastrowid)
        conn.execute(
            """INSERT INTO filaments
               (id, vendor_id, material_id, name, color_id, diameter, weight, spool_weight, price,
                nozzle_temp_min, nozzle_temp_max, cali_idx, bambu_filament_id, bambu_setting_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(filament_id), vendor_id, material_id, filament.get("name") or f"Filament {filament_id}",
                _color_id(conn, filament.get("color_id") or filament.get("color_hex")), _db_value(filament.get("diameter")) or 1.75,
                _db_value(filament.get("weight")), _db_value(filament.get("spool_weight")), _db_value(filament.get("price")),
                *_temperature_range(_extra_value(filament.get("extra"), "nozzle_temperature")),
                _db_value(_extra_value(filament.get("extra"), "cali_idx")),
                _db_value(_extra_value(filament.get("extra"), "filament_id")), _db_value(_extra_value(filament.get("extra"), "setting_id")),
                now, now,
            ),
        )


def ensure_local_spool_from_inventory(spool: dict[str, Any] | None) -> None:
    """Create a local editable spool mirror without changing the active backend."""
    spool = spool or {}
    spool_id = spool.get("id")
    filament = spool.get("filament") or {}
    filament_id = filament.get("id")
    if spool_id is None or filament_id is None:
        return
    ensure_local_filament_from_inventory(filament)
    now = _now()
    with _connect() as conn:
        existing = conn.execute("SELECT id FROM spools WHERE id = ?", (spool_id,)).fetchone()
        if existing:
            conn.execute("UPDATE spools SET deleted = 0, updated_at = ? WHERE id = ?", (now, spool_id))
            return
        extras = spool.get("extra") or {}
        nfc_tag_id = _ensure_nfc_catalog_value(conn, _extra_value(extras, "tag"))
        conn.execute(
            """INSERT INTO spools
               (id, filament_id, price, archived, initial_weight, spool_weight, weight_correction, nfc_tag_id, active_tray, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (
                int(spool_id), int(filament_id), spool.get("price") if spool.get("price") is not None else filament.get("price"),
                int(bool(spool.get("archived"))), spool.get("initial_weight") if spool.get("initial_weight") is not None else filament.get("weight"),
                spool.get("spool_weight") if spool.get("spool_weight") is not None else filament.get("spool_weight"),
                nfc_tag_id, _extra_value(extras, "active_tray"), now, now,
            ),
        )


def save_entity(entity: str, values: dict[str, Any], entity_id: int | None = None) -> int:
    initialize()
    columns = {
            "vendors": ("name",),
        "materials": ("name",),
        "filaments": (
            "vendor_id", "material_id", "name", "color_id", "diameter",
            "weight", "spool_weight", "price", "nozzle_temp_min", "nozzle_temp_max",
            "cali_idx", "bambu_filament_id", "bambu_setting_id", "comment",
        ),
    }
    if entity not in columns:
        raise ValueError("Unknown inventory entity")
    fields = columns[entity]
    row = {field: values.get(field) for field in fields}
    if not str(row.get("name") or "").strip():
        raise ValueError("Name is required")
    row["name"] = str(row["name"]).strip()
    if entity == "filaments":
        row["comment"] = str(row.get("comment") or "").strip() or None
    if entity == "filaments":
        row["color_id"] = values.get("color_id") or values.get("color_hex")
        row["diameter"] = _number(row["diameter"]) if row["diameter"] not in (None, "") else 1.75
        for field in ("weight", "spool_weight", "price", "nozzle_temp_min", "nozzle_temp_max", "cali_idx"):
            if row[field] not in (None, ""):
                row[field] = _number(row[field])
    now = _now()
    with _connect() as conn:
        if entity == "materials":
            duplicate = conn.execute(
                "SELECT 1 FROM materials WHERE name = ? COLLATE NOCASE AND id <> COALESCE(?, -1) AND deleted = 0 LIMIT 1",
                (row["name"], entity_id),
            ).fetchone()
            if duplicate:
                raise ValueError("Dieses Material ist bereits vorhanden.")
            if entity_id is None:
                deleted_row = conn.execute(
                    "SELECT id FROM materials WHERE name = ? COLLATE NOCASE AND deleted = 1 LIMIT 1",
                    (row["name"],),
                ).fetchone()
                if deleted_row:
                    conn.execute("UPDATE materials SET deleted = 0 WHERE id = ?", (deleted_row["id"],))
                    return int(deleted_row["id"])
        if entity == "filaments":
            row["color_id"] = _color_id(conn, row.get("color_id"))
        if entity_id is None:
            if entity == "filaments":
                row.update(created_at=now, updated_at=now)
                fields += ("created_at", "updated_at")
            cur = conn.execute(
                f"INSERT INTO {entity} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                [row.get(field) for field in fields],
            )
            return int(cur.lastrowid)
        row["updated_at"] = now
        if entity == "filaments":
            fields += ("updated_at",)
        conn.execute(
            f"UPDATE {entity} SET {','.join(f'{field} = ?' for field in fields)} WHERE id = ?",
            [row.get(field) for field in fields] + [entity_id],
        )
        return entity_id


def save_color(values: dict[str, Any], color_id: int | None = None) -> int:
    initialize()
    name = str(values.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required")
    hex_code = _normalize_hex(values.get("hex_code") or _rgb_hex(values))
    with _connect() as conn:
        duplicate = conn.execute(
            "SELECT 1 FROM colors WHERE name = ? COLLATE NOCASE AND id <> COALESCE(?, -1) AND deleted = 0 LIMIT 1",
            (name, color_id),
        ).fetchone()
        if duplicate:
            raise ValueError("Diese Farbe ist bereits vorhanden.")
        if color_id is None:
            deleted_row = conn.execute(
                "SELECT id FROM colors WHERE name = ? COLLATE NOCASE AND deleted = 1 LIMIT 1",
                (name,),
            ).fetchone()
            if deleted_row:
                conn.execute("UPDATE colors SET hex_code = ?, deleted = 0 WHERE id = ?", (hex_code, deleted_row["id"]))
                return int(deleted_row["id"])
            cursor = conn.execute(
                "INSERT INTO colors(name, hex_code) VALUES (?, ?)", (name, hex_code)
            )
            return int(cursor.lastrowid)
        if not conn.execute("SELECT 1 FROM colors WHERE id = ? AND deleted = 0", (color_id,)).fetchone():
            raise KeyError(f"No color with ID {color_id}")
        conn.execute("UPDATE colors SET name = ?, hex_code = ? WHERE id = ?", (name, hex_code, color_id))
        return color_id


def update_filament_fields(filament_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Update only changed local filament fields for the inline inventory editor."""
    initialize()
    editable = {
        "vendor_id", "material_id", "name", "color_id", "diameter",
        "weight", "spool_weight", "price",
        "nozzle_temp_min", "nozzle_temp_max", "cali_idx", "bambu_filament_id", "bambu_setting_id", "comment",
    }
    updates = {key: value for key, value in values.items() if key in editable or key == "color_hex"}
    if not updates:
        raise ValueError("No editable filament field was supplied")
    numeric_fields = {"vendor_id", "material_id", "diameter", "weight", "spool_weight", "price", "nozzle_temp_min", "nozzle_temp_max", "cali_idx"}
    for key, value in list(updates.items()):
        if value == "":
            updates[key] = None
        elif key in numeric_fields:
            updates[key] = 1.75 if key == "diameter" and value == "" else (int(_number(value)) if key in {"vendor_id", "material_id", "nozzle_temp_min", "nozzle_temp_max", "cali_idx"} else _number(value))
        elif key in {"color_hex", "color_id"}:
            updates["color_id"] = value
            if key == "color_hex":
                updates.pop("color_hex", None)
        elif key in {"name", "bambu_filament_id", "bambu_setting_id", "comment"}:
            updates[key] = str(value).strip() or None
    if "name" in updates and not updates["name"]:
        raise ValueError("Name is required")
    with _connect() as conn:
        previous = conn.execute(
            "SELECT weight, spool_weight, price FROM filaments WHERE id = ? AND deleted = 0", (filament_id,)
        ).fetchone()
        if not previous:
            raise KeyError(f"No filament with ID {filament_id}")
        if "color_id" in updates:
            updates["color_id"] = _color_id(conn, updates["color_id"])
        updates["updated_at"] = _now()
        conn.execute(
            f"UPDATE filaments SET {','.join(f'{key} = ?' for key in updates)} WHERE id = ? AND deleted = 0",
            list(updates.values()) + [filament_id],
        )
        # Spools with inherited values are stored as NULL.  Older local
        # mirrors may contain a copied default, so clear those values when
        # the filament default changes; the spool view then resolves the new
        # filament value immediately. Explicit spool overrides remain intact.
        inherited_fields = {
            "weight": ("initial_weight",),
            "spool_weight": ("spool_weight",),
            "price": ("price",),
        }
        for filament_field, (spool_field,) in inherited_fields.items():
            if filament_field not in updates:
                continue
            old_value = previous[filament_field]
            new_value = updates[filament_field]
            if old_value is None:
                condition = f"({spool_field} IS NULL OR {spool_field} = 0)"
                params = [filament_id]
            else:
                condition = f"({spool_field} IS NULL OR {spool_field} = 0 OR ABS({spool_field} - ?) < 0.000001)"
                params = [filament_id, float(old_value)]
            conn.execute(
                f"UPDATE spools SET {spool_field} = NULL, updated_at = ? WHERE filament_id = ? AND deleted = 0 AND {condition}",
                [_now(), *params],
            )
    return next(item for item in list_filaments() if int(item["id"]) == int(filament_id))


def update_vendor_fields(vendor_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Persist a single local vendor edit from the inline inventory editor."""
    name = str(values.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required")
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM vendors WHERE id = ? AND deleted = 0", (vendor_id,)).fetchone():
            raise KeyError(f"No vendor with ID {vendor_id}")
        conn.execute("UPDATE vendors SET name = ? WHERE id = ?", (name, vendor_id))
    return next(item for item in list_entities("vendors") if int(item["id"]) == int(vendor_id))


def _spool_has_bookings(conn: sqlite3.Connection, spool_id: int) -> bool:
    """A booked spool must retain its original filament master record."""
    spool = conn.execute(
        "SELECT weight_correction FROM spools WHERE id = ? AND deleted = 0", (spool_id,)
    ).fetchone()
    if spool and abs(float(spool["weight_correction"] or 0)) > 0.000001:
        return True
    if conn.execute(
        "SELECT 1 FROM spool_consumption_events WHERE spool_id = ? AND deleted = 0 LIMIT 1", (spool_id,)
    ).fetchone():
        return True
    has_usage_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spool_usage'"
    ).fetchone()
    return bool(has_usage_table and conn.execute(
        "SELECT 1 FROM spool_usage WHERE spool_id = ? AND deleted = 0 LIMIT 1", (spool_id,)
    ).fetchone())


def _spool_is_in_active_print(conn: sqlite3.Connection, spool_id: int) -> bool:
    """Return whether this spool is assigned to a currently active print."""
    required = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('spool_usage', 'print_layer_tracking')"
    ).fetchone()[0]
    if int(required or 0) < 2:
        return False
    return bool(conn.execute(
        """SELECT 1
           FROM spool_usage u
           JOIN print_layer_tracking t ON t.print_id = u.print_id
          WHERE u.spool_id = ?
            AND u.deleted = 0 AND t.deleted = 0
            AND t.status IN ('PREPARING', 'RUNNING', 'PAUSED')
          LIMIT 1""",
        (int(spool_id),),
    ).fetchone())


def _spool_is_in_active_ams(conn: sqlite3.Connection, spool_id: int) -> bool:
    # Every spool assigned to a tray is locked in the inventory.  AMS changes
    # are made only by the AMS workflow, never from the inventory editor.
    return bool(conn.execute(
        """SELECT 1 FROM spools
          WHERE id = ? AND deleted = 0 AND trim(COALESCE(active_tray, '')) <> ''
          LIMIT 1""",
        (int(spool_id),),
    ).fetchone())


def _spool_is_nfc_bound(conn: sqlite3.Connection, spool_id: int) -> bool:
    """Return whether the printer assigned this spool from its NFC UID."""
    return bool(conn.execute(
        """SELECT 1 FROM spools
           WHERE id = ? AND deleted = 0
             AND trim(COALESCE(active_tray, '')) <> ''
             AND lower(COALESCE(active_tray_source, '')) = 'nfc'
           LIMIT 1""",
        (int(spool_id),),
    ).fetchone())


def _spool_lock_message(conn: sqlite3.Connection, spool_id: int) -> str:
    # Prefer the AMS-specific explanation when the spool is locked by its
    # current tray assignment; otherwise report the active-print lock.
    if _spool_is_nfc_bound(conn, spool_id):
        return "Die Spule ist per NFC im AMS gebunden und gesperrt"
    if _spool_is_in_active_ams(conn, spool_id):
        return "Die Spule ist in der AMS gebunden und gesperrt"
    return "Die Spule ist im aktuell laufenden Druck gebunden und gesperrt"


def _has_active_print(conn: sqlite3.Connection) -> bool:
    # Tray assignments are printer-wide state.  Lock them for every spool
    # while any print is preparing, running, or paused.
    required = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'print_layer_tracking'"
    ).fetchone()[0]
    if not int(required or 0):
        return False
    return bool(conn.execute(
        "SELECT 1 FROM print_layer_tracking WHERE deleted = 0 AND status IN ('PREPARING', 'RUNNING', 'PAUSED') LIMIT 1"
    ).fetchone())


def update_spool_fields(spool_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Persist only the explicitly changed local spool fields."""
    initialize()
    editable = {"filament_id", "price", "archived", "initial_weight", "spool_weight", "tag", "nfc_tag_id", "active_tray", "purchase_date", "comment"}
    updates = {key: value for key, value in values.items() if key in editable}
    if not updates:
        raise ValueError("No editable spool field was supplied")
    numeric_fields = {"filament_id", "price", "initial_weight", "spool_weight", "weight_correction"}
    for key, value in list(updates.items()):
        if key == "tag":
            updates[key] = normalize_nfc_tag(value) or None
        elif key == "nfc_tag_id":
            updates[key] = int(value) if str(value or "").strip() else None
        elif key == "active_tray":
            updates[key] = str(value).strip() or None
        elif key == "purchase_date":
            updates[key] = _date_only(value)
        elif key == "comment":
            updates[key] = str(value).strip() or None
        elif key == "archived":
            updates[key] = int(value is True or str(value).strip().lower() in {"1", "true", "yes", "on"})
        elif value == "":
            updates[key] = None
        elif key == "filament_id":
            updates[key] = int(_number(value))
        elif key in numeric_fields:
            updates[key] = _number(value)
            if key in {"price", "initial_weight", "spool_weight"} and updates[key] == 0:
                updates[key] = None
    with _connect() as conn:
        current = conn.execute(
            """SELECT s.filament_id, s.nfc_tag_id, s.archived,
                      s.initial_weight, s.spool_weight, s.price,
                      f.weight AS filament_weight,
                      f.spool_weight AS filament_spool_weight,
                      f.price AS filament_price
                 FROM spools s
                 JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
                WHERE s.id = ? AND s.deleted = 0""",
            (spool_id,),
        ).fetchone()
        if not current:
            raise KeyError(f"No spool with ID {spool_id}")
        desired_archived = int(updates.get("archived", current["archived"]) or 0)
        if bool(current["archived"]):
            if desired_archived or set(updates) != {"archived"}:
                raise ValueError("Die Spule ist archiviert und gesperrt.")
        if _has_active_print(conn) and {"nfc_tag_id", "tag", "active_tray"}.intersection(updates):
            raise ValueError("NFC-Tag und aktives Fach können während eines Drucks nicht geändert werden.")
        if "active_tray" in updates and _has_active_print(conn):
            raise ValueError("Das aktive Fach kann während eines Drucks nicht geändert werden.")
        if _spool_is_nfc_bound(conn, spool_id) and {"filament_id", "nfc_tag_id", "tag", "active_tray", "archived"}.intersection(updates):
            raise ValueError(_spool_lock_message(conn, spool_id))
        if _spool_is_in_active_print(conn, spool_id) or _spool_is_in_active_ams(conn, spool_id):
            raise ValueError(_spool_lock_message(conn, spool_id))
        old_nfc_id = int(current["nfc_tag_id"]) if current["nfc_tag_id"] else None
        if desired_archived and not bool(current["archived"]):
            # Freeze inherited filament defaults when the spool is archived.
            # Later edits to the filament master must not change its history.
            archive_defaults = {
                "initial_weight": current["filament_weight"],
                "spool_weight": current["filament_spool_weight"],
                "price": current["filament_price"],
            }
            for field, default_value in archive_defaults.items():
                existing_value = updates.get(field, current[field])
                if existing_value in (None, "", 0, 0.0) and default_value is not None:
                    updates[field] = default_value
        desired_nfc_id = updates.get("nfc_tag_id", old_nfc_id)
        if "tag" in updates and "nfc_tag_id" not in updates:
            desired_nfc_id = None
            if updates["tag"]:
                tag_row = conn.execute("SELECT id FROM nfc_tags WHERE tag_value = ? COLLATE NOCASE", (updates["tag"],)).fetchone()
                if not tag_row:
                    desired_nfc_id = _sync_nfc_tag_row(conn, updates["tag"])
                else:
                    desired_nfc_id = int(tag_row["id"])
            updates["nfc_tag_id"] = desired_nfc_id
        if desired_nfc_id:
            tag_row = conn.execute("SELECT tag_value FROM nfc_tags WHERE id = ?", (int(desired_nfc_id),)).fetchone()
            if not tag_row:
                raise ValueError("Ungültiger NFC-Tag")
            desired_tag = tag_row["tag_value"]
            if conn.execute(
                "SELECT 1 FROM spools WHERE nfc_tag_id = ? AND id <> ? AND deleted = 0 LIMIT 1",
                (int(desired_nfc_id), int(spool_id)),
            ).fetchone():
                raise ValueError("Dieser NFC-Tag ist bereits einer anderen Spule zugeordnet.")
        else:
            desired_tag = None
        updates["nfc_tag_id"] = None if desired_archived else desired_nfc_id
        if desired_archived:
            # Archiving releases the physical NFC tag for reuse while the tag
            # remains in the catalog for later assignment.
            desired_tag = None
        if desired_tag:
            _sync_nfc_tag_row(conn, desired_tag)
        elif desired_archived and old_nfc_id:
            # Archiving releases the tag for reuse and removes the previous
            # spool-specific comment; the tag record itself remains available.
            conn.execute(
                "UPDATE nfc_tags SET comment = NULL, updated_at = ? WHERE id = ?",
                (_now(), old_nfc_id),
            )
        if ("filament_id" in updates and int(updates["filament_id"]) != int(current["filament_id"])
                and _spool_has_bookings(conn, spool_id)):
            raise ValueError("Das Filament einer gebuchten Spule kann nicht mehr geändert werden")
        if "filament_id" in updates and not conn.execute("SELECT 1 FROM filaments WHERE id = ? AND deleted = 0", (updates["filament_id"],)).fetchone():
            raise ValueError("Unknown filament")
        updates.pop("tag", None)
        updates["updated_at"] = _now()
        conn.execute(
            f"UPDATE spools SET {','.join(f'{key} = ?' for key in updates)} WHERE id = ?",
            list(updates.values()) + [spool_id],
        )
    return get_spool(spool_id)


def adjust_spool_stock(spool_id: int | str, mode: str, amount: Any) -> dict[str, Any]:
    """Apply an absolute measured remaining weight or a relative stock change."""
    initialize()
    mode = str(mode or '').strip().lower()
    if mode not in {'absolute', 'delta'}:
        raise ValueError('Unknown stock adjustment mode')
    value = _number(amount)
    if mode == 'absolute' and value < 0:
        raise ValueError('Das Gewicht darf nicht negativ sein')
    with _connect() as conn:
        spool = conn.execute(
            """SELECT s.initial_weight, s.spool_weight, s.weight_correction, s.archived,
                      f.weight AS filament_weight, f.spool_weight AS filament_spool_weight
                 FROM spools s JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
                WHERE s.id = ? AND s.deleted = 0""",
            (int(spool_id),),
        ).fetchone()
        if not spool:
            raise KeyError(f'No spool with ID {spool_id}')
        if bool(spool['archived']):
            raise ValueError('Die Spule ist archiviert und gesperrt.')
        if _spool_is_in_active_print(conn, spool_id) or _spool_is_in_active_ams(conn, spool_id):
            raise ValueError(_spool_lock_message(conn, spool_id))
        used = float(conn.execute(
            'SELECT COALESCE(SUM(grams_used), 0) FROM spool_usage WHERE spool_id = ?',
            (int(spool_id),),
        ).fetchone()[0] or 0)
        initial = float(spool['initial_weight'] or 0)
        maximum = initial or float(spool['filament_weight'] or 0)
        tare = float(spool['spool_weight'] or spool['filament_spool_weight'] or 0)
        correction = float(spool['weight_correction'] or 0)
        if mode == 'absolute':
            if value < tare - 0.000001:
                raise ValueError(f'Das Gesamtgewicht darf nicht kleiner als das Leergewicht {tare:g} g sein')
            if maximum > 0 and value > maximum + tare + 0.000001:
                raise ValueError(f'Das Gesamtgewicht darf höchstens {maximum + tare:g} g betragen')
            correction = value - tare - maximum + used
        else:
            new_correction = correction - value
            new_remaining = maximum + new_correction - used
            if maximum > 0 and new_remaining > maximum + 0.000001:
                raise ValueError(f'Das Gewicht darf höchstens {maximum:g} g betragen')
            if maximum > 0 and new_remaining < -0.000001:
                raise ValueError('Das Gewicht darf nicht unter 0 g fallen')
            correction = new_correction
        conn.execute(
            'UPDATE spools SET weight_correction = ?, updated_at = ? WHERE id = ?',
            (correction, _now(), int(spool_id)),
        )
    return get_spool(spool_id)


def delete_entity(entity: str, entity_id: int) -> None:
    if entity not in {"vendors", "materials", "filaments", "colors"}:
        raise ValueError("Unknown inventory entity")
    initialize()
    with _connect() as conn:
        if entity == "filaments" and conn.execute(
            "SELECT 1 FROM spools WHERE filament_id = ? AND deleted = 0 LIMIT 1", (entity_id,)
        ).fetchone():
            raise ValueError("Löschen gesperrt: Das Filament wird aktuell von einer Spule verwendet.")
        if entity == "vendors" and conn.execute(
            "SELECT 1 FROM filaments WHERE vendor_id = ? AND deleted = 0 LIMIT 1", (entity_id,)
        ).fetchone():
            raise ValueError("Löschen gesperrt: Der Hersteller wird aktuell von einem Filament verwendet.")
        if entity == "materials" and conn.execute("SELECT 1 FROM filaments WHERE material_id = ? AND deleted = 0 LIMIT 1", (entity_id,)).fetchone():
            raise ValueError("Löschen gesperrt: Das Material wird aktuell von einem Filament verwendet.")
        if entity == "colors" and conn.execute("SELECT 1 FROM filaments WHERE color_id = ? AND deleted = 0 LIMIT 1", (entity_id,)).fetchone():
            raise ValueError("Löschen gesperrt: Die Farbe wird aktuell von einem Filament verwendet.")
        if not conn.execute(f"SELECT 1 FROM {entity} WHERE id = ? AND deleted = 0", (entity_id,)).fetchone():
            raise KeyError(f"No {entity} with ID {entity_id}")
        conn.execute(f"UPDATE {entity} SET deleted = 1 WHERE id = ?", (entity_id,))


def save_spool(values: dict[str, Any], spool_id: int | None = None) -> int:
    initialize()
    if spool_id is not None and "weight_correction" in values:
        raise ValueError("Die Bestandskorrektur kann nur über die Bestandsveränderung geändert werden")
    fields = ("filament_id", "price", "archived", "initial_weight", "spool_weight", "weight_correction", "nfc_tag_id", "active_tray", "purchase_date", "comment")
    row = {field: values.get(field) for field in fields}
    for field in ("price", "initial_weight", "spool_weight"):
        if row[field] not in (None, ""):
            row[field] = _number(row[field])
            if row[field] == 0:
                row[field] = None
    row["archived"] = int(row["archived"] is True or str(row["archived"]).strip().lower() in {"1", "true", "yes", "on"})
    row["weight_correction"] = _number(row["weight_correction"] or 0)
    row["purchase_date"] = _date_only(row["purchase_date"])
    row["comment"] = str(row["comment"] or "").strip() or None
    now = _now()
    with _connect() as conn:
        existing = conn.execute(
            """SELECT s.filament_id, s.nfc_tag_id, s.active_tray, s.active_tray_source,
                      s.archived, s.initial_weight, s.spool_weight, s.price,
                      s.purchase_date, s.comment,
                      f.weight AS filament_weight,
                      f.spool_weight AS filament_spool_weight,
                      f.price AS filament_price
                 FROM spools s
                 JOIN filaments f ON f.id = s.filament_id
                WHERE s.id = ?""",
            (spool_id,),
        ).fetchone() if spool_id is not None else None
        if spool_id is not None and not existing:
            raise KeyError(f"No spool with ID {spool_id}")
        if existing and bool(existing["archived"]):
            if row["archived"] or set(values) != {"archived"}:
                raise ValueError("Die Spule ist archiviert und gesperrt.")
        if existing and row["archived"] and not bool(existing["archived"]):
            # Freeze inherited filament defaults when archiving a spool.
            for field, default_field in (
                ("initial_weight", "filament_weight"),
                ("spool_weight", "filament_spool_weight"),
                ("price", "filament_price"),
            ):
                if row[field] in (None, "", 0, 0.0) and existing[default_field] is not None:
                    row[field] = existing[default_field]
        if existing and _spool_is_in_active_print(conn, spool_id):
            raise ValueError("Die Spule ist im aktuell laufenden Druck gebunden und gesperrt")
        if existing and _spool_is_nfc_bound(conn, spool_id):
            changed = (
                ("filament_id" in values and str(values.get("filament_id") or "") != str(existing["filament_id"] or ""))
                or ("active_tray" in values and str(values.get("active_tray") or "").strip() != str(existing["active_tray"] or "").strip())
                or ("nfc_tag_id" in values and str(values.get("nfc_tag_id") or "").strip() != str(existing["nfc_tag_id"] or "").strip())
                or ("archived" in values and int(bool(values.get("archived"))) != int(existing["archived"] or 0))
            )
            if changed:
                raise ValueError(_spool_lock_message(conn, spool_id))
        if existing and _has_active_print(conn):
            protected = {"nfc_tag_id", "tag", "active_tray"}.intersection(values)
            changed = any(
                (key == "active_tray" and str(values.get(key) or "").strip() != str(existing[key] or "").strip())
                or (key == "nfc_tag_id" and str(values.get(key) or "").strip() != str(existing[key] or "").strip())
                or (key == "tag" and normalize_nfc_tag(values.get(key)) != normalize_nfc_tag(existing[key]))
                for key in protected
            )
            if changed:
                raise ValueError("NFC-Tag und aktives Fach können während eines Drucks nicht geändert werden.")
        if (existing and "filament_id" in values and str(row["filament_id"]) != str(existing["filament_id"])
                and _spool_has_bookings(conn, spool_id)):
            raise ValueError("Das Filament einer gebuchten Spule kann nicht mehr geändert werden")
        existing_tag = None
        if existing and existing["nfc_tag_id"]:
            existing_tag_row = conn.execute("SELECT tag_value FROM nfc_tags WHERE id = ?", (int(existing["nfc_tag_id"]),)).fetchone()
            existing_tag = existing_tag_row["tag_value"] if existing_tag_row else None
        row["tag"] = normalize_nfc_tag(values.get("tag")) or None if "tag" in values else existing_tag
        row["nfc_tag_id"] = int(values["nfc_tag_id"]) if "nfc_tag_id" in values and str(values.get("nfc_tag_id") or "").strip() else (existing["nfc_tag_id"] if existing else None)
        if "tag" in values and "nfc_tag_id" not in values and row["tag"]:
            tag_row = conn.execute("SELECT id FROM nfc_tags WHERE tag_value = ? COLLATE NOCASE", (row["tag"],)).fetchone()
            row["nfc_tag_id"] = int(tag_row["id"]) if tag_row else _sync_nfc_tag_row(conn, row["tag"])
        if row["nfc_tag_id"]:
            tag_row = conn.execute("SELECT tag_value FROM nfc_tags WHERE id = ?", (int(row["nfc_tag_id"]),)).fetchone()
            if not tag_row:
                raise ValueError("Ungültiger NFC-Tag")
            if conn.execute(
                "SELECT 1 FROM spools WHERE nfc_tag_id = ? AND id <> ? AND deleted = 0 LIMIT 1",
                (int(row["nfc_tag_id"]), int(spool_id or -1)),
            ).fetchone():
                raise ValueError("Dieser NFC-Tag ist bereits einer anderen Spule zugeordnet.")
            row["tag"] = tag_row["tag_value"]
        row["active_tray"] = str(row["active_tray"] or "").strip() or None if "active_tray" in values else (existing["active_tray"] if existing else None)
        row["purchase_date"] = _date_only(row["purchase_date"]) if "purchase_date" in values else (existing["purchase_date"] if existing else None)
        row["comment"] = str(row["comment"] or "").strip() or None if "comment" in values else (existing["comment"] if existing else None)
        old_nfc_id = int(existing["nfc_tag_id"]) if existing and existing["nfc_tag_id"] else None
        if row["archived"]:
            row["tag"] = None
            row["nfc_tag_id"] = None
        if row["tag"]:
            _sync_nfc_tag_row(conn, row["tag"])
        elif row["archived"] and old_nfc_id:
            # Keep the catalog entry, but remove text that referred to the
            # archived spool so the tag is clean when assigned again.
            conn.execute(
                "UPDATE nfc_tags SET comment = NULL, updated_at = ? WHERE id = ?",
                (_now(), old_nfc_id),
            )
        measured_remaining = values.get("current_remaining_weight")
        adjustment_value = values.get("adjustment_weight")
        adjustment_mode = str(values.get("adjustment_mode") or "").strip().lower()
        if measured_remaining not in (None, ""):
            measured_value = _number(measured_remaining)
            if measured_value < 0:
                raise ValueError("Das Gewicht darf nicht negativ sein")
            used = 0.0
            if spool_id is not None:
                used = float(conn.execute(
                    "SELECT COALESCE(SUM(grams_used), 0) FROM spool_usage WHERE spool_id = ?",
                    (spool_id,),
                ).fetchone()[0] or 0)
            maximum = float(row["initial_weight"] or (existing["initial_weight"] if existing else 0) or (existing["filament_weight"] if existing else 0) or 0)
            tare = float(row["spool_weight"] or (existing["spool_weight"] if existing else 0) or (existing["filament_spool_weight"] if existing else 0) or 0)
            if measured_value < tare - 0.000001:
                raise ValueError(f"Das Gesamtgewicht darf nicht kleiner als das Leergewicht {tare:g} g sein")
            if maximum > 0 and measured_value > maximum + tare + 0.000001:
                raise ValueError(f"Das Gesamtgewicht darf höchstens {maximum + tare:g} g betragen")
            row["weight_correction"] = measured_value - tare - maximum + used
        elif adjustment_value not in (None, "") and spool_id is not None:
            previous = conn.execute("SELECT weight_correction FROM spools WHERE id = ?", (spool_id,)).fetchone()
            previous_correction = float(previous["weight_correction"] or 0) if previous else 0.0
            amount = _number(adjustment_value)
            if adjustment_mode == "absolute":
                if amount < 0:
                    raise ValueError("Das Gewicht darf nicht negativ sein")
                used = float(conn.execute(
                    "SELECT COALESCE(SUM(grams_used), 0) FROM spool_usage WHERE spool_id = ?",
                    (spool_id,),
                ).fetchone()[0] or 0)
                maximum = float(row["initial_weight"] or (existing["initial_weight"] if existing else 0) or (existing["filament_weight"] if existing else 0) or 0)
                tare = float(row["spool_weight"] or (existing["spool_weight"] if existing else 0) or (existing["filament_spool_weight"] if existing else 0) or 0)
                if amount < tare - 0.000001:
                    raise ValueError(f"Das Gesamtgewicht darf nicht kleiner als das Leergewicht {tare:g} g sein")
                if maximum > 0 and amount > maximum + tare + 0.000001:
                    raise ValueError(f"Das Gesamtgewicht darf höchstens {maximum + tare:g} g betragen")
                row["weight_correction"] = amount - tare - maximum + used
            else:
                used = float(conn.execute(
                    "SELECT COALESCE(SUM(grams_used), 0) FROM spool_usage WHERE spool_id = ?",
                    (spool_id,),
                ).fetchone()[0] or 0)
                maximum = float(row["initial_weight"] or (existing["initial_weight"] if existing else 0) or (existing["filament_weight"] if existing else 0) or 0)
                new_correction = previous_correction - amount
                new_remaining = maximum + new_correction - used
                if maximum > 0 and new_remaining > maximum + 0.000001:
                    raise ValueError(f"Das Gewicht darf höchstens {maximum:g} g betragen")
                if maximum > 0 and new_remaining < -0.000001:
                    raise ValueError("Das Gewicht darf nicht unter 0 g fallen")
                row["weight_correction"] = new_correction
        elif spool_id is not None:
            previous = conn.execute("SELECT weight_correction FROM spools WHERE id = ?", (spool_id,)).fetchone()
            if previous:
                row["weight_correction"] = float(previous["weight_correction"] or 0)
        if spool_id is None:
            row.update(created_at=now, updated_at=now)
            fields += ("created_at", "updated_at")
            cur = conn.execute(
                f"INSERT INTO spools ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                [row.get(field) for field in fields],
            )
            new_id = int(cur.lastrowid)
            if row["tag"]:
                _sync_nfc_tag_row(conn, row["tag"])
            return new_id
        row["updated_at"] = now
        fields += ("updated_at",)
        conn.execute(
            f"UPDATE spools SET {','.join(f'{field} = ?' for field in fields)} WHERE id = ?",
            [row.get(field) for field in fields] + [spool_id],
        )
        return spool_id


def list_spools(include_archived: bool = False) -> list[dict[str, Any]]:
    initialize()
    with _connect() as conn:
        has_usage_table = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spool_usage'"
        ).fetchone())
        has_consumption_events = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spool_consumption_events'"
        ).fetchone())
        usage_select = "COALESCE(u.used_weight, 0)" if has_usage_table else "0"
        last_used_sources = []
        if has_usage_table:
            # The print timestamp is the start of a job, not the last time
            # this spool was actually used. Keep it as a fallback for older
            # records without per-consumption events.
            last_used_sources.append(
                "SELECT replace(substr(p.print_date, 1, 19), 'T', ' ') AS used_at "
                "FROM spool_usage lu JOIN prints p ON p.id = lu.print_id "
                "WHERE lu.spool_id = s.id AND lu.deleted = 0 AND p.deleted = 0"
            )
        if has_consumption_events:
            last_used_sources.append(
                "SELECT replace(substr(ce.occurred_at, 1, 19), 'T', ' ') AS used_at "
                "FROM spool_consumption_events ce WHERE ce.spool_id = s.id AND ce.deleted = 0"
            )
        last_used_select = (
            f"(SELECT MAX(used_at) FROM ({' UNION ALL '.join(last_used_sources)}))"
            if last_used_sources else "NULL"
        )
        usage_join = """LEFT JOIN (SELECT spool_id, SUM(grams_used) AS used_weight
                                   FROM spool_usage WHERE spool_id IS NOT NULL AND deleted = 0 GROUP BY spool_id) u
                              ON u.spool_id = s.id""" if has_usage_table else ""
        usage_booking = "OR EXISTS (SELECT 1 FROM spool_usage fu WHERE fu.spool_id = s.id AND fu.deleted = 0)" if has_usage_table else ""
        active_print_select = """CASE WHEN EXISTS (
                         SELECT 1 FROM spool_usage au
                         JOIN print_layer_tracking at ON at.print_id = au.print_id
                         WHERE au.spool_id = s.id
                           AND au.deleted = 0 AND at.deleted = 0
                           AND at.status IN ('PREPARING', 'RUNNING', 'PAUSED')
                       ) THEN 1 ELSE 0 END""" if has_usage_table else "0"
        active_ams_select = "CASE WHEN trim(COALESCE(s.active_tray, '')) <> '' THEN 1 ELSE 0 END"
        sql = f"""SELECT s.*, nt.tag_value AS nfc_tag_value,
                    f.name AS filament_name, c.hex_code AS color_hex, f.diameter,
                    f.weight AS filament_default_weight,
                    f.spool_weight AS filament_default_spool_weight,
                    f.price AS filament_default_price,
                    f.nozzle_temp_min, f.nozzle_temp_max, f.cali_idx, f.bambu_filament_id,
                    f.bambu_setting_id, v.id AS vendor_id,
                    v.name AS vendor_name, m.id AS material_id, m.name AS material,
                    {usage_select} AS used_weight,
                    {last_used_select} AS last_used,
                    CASE WHEN ABS(COALESCE(s.weight_correction, 0)) > 0.000001
                              OR EXISTS (SELECT 1 FROM spool_consumption_events ce WHERE ce.spool_id = s.id AND ce.deleted = 0)
                              {usage_booking}
                         THEN 1 ELSE 0 END AS filament_locked
                    ,{active_print_select} AS active_print_locked
                    ,{active_ams_select} AS active_ams_locked
             FROM spools s JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
             LEFT JOIN vendors v ON v.id = f.vendor_id AND v.deleted = 0
             LEFT JOIN materials m ON m.id = f.material_id AND m.deleted = 0
             LEFT JOIN colors c ON c.id = f.color_id AND c.deleted = 0
             LEFT JOIN nfc_tags nt ON nt.id = s.nfc_tag_id AND nt.deleted = 0
             {usage_join}"""
        sql += " WHERE s.deleted = 0"
        if not include_archived:
            sql += " AND s.archived = 0"
        sql += " ORDER BY s.id"
        return [_as_spool(row) for row in conn.execute(sql)]


def get_spool(spool_id: int | str) -> dict[str, Any]:
    for spool in list_spools(include_archived=True):
        if str(spool["id"]) == str(spool_id):
            return spool
    raise KeyError(f"No spool with ID {spool_id}")


def duplicate_spool(spool_id: int | str) -> int:
    # Create a fresh copy containing only the spool master data. A locked
    # source spool may be copied; the new spool is never bound automatically.
    source = get_spool(spool_id)
    defaults = source.get("filament_defaults") or {}
    values = {
        "filament_id": source["filament_id"],
        "price": None if defaults.get("price") else source.get("price"),
        "initial_weight": None if defaults.get("initial_weight") else source.get("initial_weight"),
        "spool_weight": None if defaults.get("spool_weight") else source.get("spool_weight"),
        "weight_correction": 0,
        "tag": None,
        "active_tray": None,
        "purchase_date": None,
        "comment": source.get("comment"),
        "archived": False,
    }
    return save_spool(values)


def delete_spool(spool_id: int | str) -> None:
    initialize()
    with _connect() as conn:
        spool_id = int(spool_id)
        if not conn.execute("SELECT 1 FROM spools WHERE id = ? AND deleted = 0", (spool_id,)).fetchone():
            raise KeyError(f"No spool with ID {spool_id}")
        archived = conn.execute(
            "SELECT archived FROM spools WHERE id = ? AND deleted = 0", (spool_id,)
        ).fetchone()
        if archived and bool(archived["archived"]):
            raise ValueError("Die Spule ist archiviert und gesperrt.")
        if _spool_is_in_active_print(conn, spool_id) or _spool_is_in_active_ams(conn, spool_id):
            raise ValueError(_spool_lock_message(conn, spool_id))
        if _spool_has_bookings(conn, spool_id):
            raise ValueError("Eine gebuchte Spule kann nicht gelöscht werden")
        conn.execute("UPDATE spools SET deleted = 1, updated_at = ? WHERE id = ?", (_now(), spool_id))


def _as_spool(row: sqlite3.Row) -> dict[str, Any]:
    import json
    spool = dict(row)
    filament_defaults = {
        "initial_weight": spool.pop("filament_default_weight"),
        "spool_weight": spool.pop("filament_default_spool_weight"),
        "price": spool.pop("filament_default_price"),
    }
    resolved_filament_defaults = {
        field: default_value if default_value is not None else 0.0
        for field, default_value in filament_defaults.items()
    }
    spool["filament_defaults"] = {}
    for field, default_value in filament_defaults.items():
        if spool.get(field) is None or _number(spool.get(field) or 0) == 0:
            spool["filament_defaults"][field] = True
            spool[field] = resolved_filament_defaults[field]
    used = float(spool.pop("used_weight") or 0)
    initial = float(spool.get("initial_weight") or 0)
    correction = float(spool.get("weight_correction") or 0)
    spool["remaining_weight"] = max(0.0, initial + correction - used)
    spool["used_weight"] = used
    spool["filament_locked"] = bool(spool.pop("filament_locked"))
    spool["active_print_locked"] = bool(spool.pop("active_print_locked"))
    spool["active_ams_locked"] = bool(spool.pop("active_ams_locked"))
    active_tray_source = str(spool.pop("active_tray_source", "") or "").strip().lower()
    active_tray_nfc_uid = str(spool.pop("active_tray_nfc_uid", "") or "").strip() or None
    spool["nfc_ams_locked"] = active_tray_source == "nfc" and bool(str(spool.get("active_tray") or "").strip())
    spool["active_tray_source"] = active_tray_source or None
    spool["active_tray_nfc_uid"] = active_tray_nfc_uid
    spool["filament"] = {
        "id": spool["filament_id"], "name": spool.pop("filament_name"),
        "material": spool.pop("material"), "color_hex": spool.pop("color_hex"),
        "diameter": spool.pop("diameter"),
        "vendor": {"id": spool.pop("vendor_id"), "name": spool.pop("vendor_name")},
        "weight": resolved_filament_defaults["initial_weight"], "price": resolved_filament_defaults["price"],
        "spool_weight": resolved_filament_defaults["spool_weight"],
        "extra": {
            "nozzle_temperature": json.dumps([spool.pop("nozzle_temp_min"), spool.pop("nozzle_temp_max")])
            if spool.get("nozzle_temp_min") is not None or spool.get("nozzle_temp_max") is not None else None,
            "cali_idx": json.dumps(spool.pop("cali_idx")) if spool.get("cali_idx") is not None else None,
            "filament_id": json.dumps(spool.pop("bambu_filament_id")) if spool.get("bambu_filament_id") is not None else None,
            "setting_id": json.dumps(spool.pop("bambu_setting_id")) if spool.get("bambu_setting_id") is not None else None,
        },
    }
    nfc_tag_value = spool.pop("nfc_tag_value", None)
    spool["extra"] = {
        "tag": json.dumps(nfc_tag_value) if nfc_tag_value is not None else None,
        "active_tray": json.dumps(spool.get("active_tray")) if spool.get("active_tray") is not None else None,
        "active_tray_source": active_tray_source or None,
        "active_tray_nfc_uid": json.dumps(active_tray_nfc_uid) if active_tray_nfc_uid else None,
    }
    spool["archived"] = bool(spool["archived"])
    return spool


def set_spool_extra(spool_id: int | str, values: dict[str, Any], *, allow_active_print: bool = False) -> None:
    import json
    fields = {}
    for key in ("tag", "active_tray", "active_tray_source", "active_tray_nfc_uid"):
        if key in values:
            value = values[key]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    pass
            if key == "tag":
                fields[key] = normalize_nfc_tag(value) or None
            else:
                fields[key] = str(value or "").strip() or None
    if fields:
        fields["updated_at"] = _now()
        with _connect() as conn:
            current = conn.execute(
                "SELECT nfc_tag_id, archived, active_tray, active_tray_source FROM spools WHERE id = ? AND deleted = 0", (int(spool_id),)
            ).fetchone()
            if not current:
                raise KeyError(f"No spool with ID {spool_id}")
            system_clear = fields.get("active_tray_source") == "system_clear"
            if current and _has_active_print(conn) and {"tag", "active_tray"}.intersection(fields) and not (system_clear or allow_active_print):
                raise ValueError("NFC-Tag und aktives Fach können während eines Drucks nicht geändert werden.")
            if current and _spool_is_nfc_bound(conn, spool_id) and "active_tray" in fields and not (system_clear or allow_active_print):
                raise ValueError(_spool_lock_message(conn, spool_id))
            if "active_tray" in fields:
                if system_clear or not fields["active_tray"]:
                    fields["active_tray_source"] = None
                    fields["active_tray_nfc_uid"] = None
                elif "active_tray_source" not in fields:
                    fields["active_tray_source"] = "manual"
                    fields["active_tray_nfc_uid"] = None
                elif fields["active_tray_source"] != "nfc":
                    fields["active_tray_nfc_uid"] = None
            if system_clear:
                fields["active_tray_source"] = None
                fields["active_tray_nfc_uid"] = None
            if "tag" in fields and current:
                desired_tag = fields["tag"]
                if current["archived"]:
                    fields["tag"] = None
                    desired_tag = None
                if desired_tag:
                    tag_id = _sync_nfc_tag_row(conn, desired_tag)
                    fields["nfc_tag_id"] = tag_id
                else:
                    fields["nfc_tag_id"] = None
                fields.pop("tag", None)
            fields.pop("active_tray_source", None) if system_clear else None
            fields.pop("active_tray_nfc_uid", None) if system_clear else None
            if system_clear:
                fields["active_tray_source"] = None
                fields["active_tray_nfc_uid"] = None
            conn.execute(f"UPDATE spools SET {','.join(f'{key} = ?' for key in fields)} WHERE id = ? AND deleted = 0", list(fields.values()) + [int(spool_id)])


def sync_active_trays(assignments: dict[int | str, str]) -> dict[str, int]:
    # Make the local catalog reflect the complete current AMS snapshot.
    # Any spool absent from the snapshot is deliberately left without a tray.
    initialize()
    normalized = {
        int(spool_id): str(tray_uid).strip()
        for spool_id, tray_uid in (assignments or {}).items()
        if str(tray_uid or "").strip()
    }
    changed = 0
    cleared = 0
    with _connect() as conn:
        rows = conn.execute("SELECT id, active_tray, active_tray_source FROM spools WHERE deleted = 0").fetchall()
        for row in rows:
            spool_id = int(row["id"])
            desired = normalized.get(spool_id)
            current = str(row["active_tray"] or "").strip() or None
            if current == desired:
                continue
            if current and not desired and str(row["active_tray_source"] or "").lower() == "nfc":
                # A UID-bound spool is cleared only after a confirmed physical
                # empty status, never merely because a sparse snapshot omitted it.
                continue
            conn.execute(
                "UPDATE spools SET active_tray = ?, active_tray_source = ?, active_tray_nfc_uid = ?, updated_at = ? WHERE id = ?",
                (desired, "manual" if desired else None, None, _now(), spool_id),
            )
            changed += 1
            if current and not desired:
                cleared += 1
    return {"assigned": len(normalized), "changed": changed, "cleared": cleared}


def record_consumption(spool_id: int | str, weight_grams: float | None = None,
                       length_mm: float | None = None, occurred_at: str | None = None) -> None:
    if weight_grams is None and length_mm is None:
        raise ValueError("weight_grams or length_mm is required")
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM spools WHERE id = ? AND deleted = 0", (int(spool_id),)).fetchone():
            raise KeyError(f"No spool with ID {spool_id}")
        conn.execute(
            "INSERT INTO spool_consumption_events(spool_id, weight_grams, length_mm, occurred_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(spool_id), weight_grams, length_mm, occurred_at or _now(), _now()),
        )


def record_usage_marker(spool_id: int | str, occurred_at: str | None = None) -> None:
    """Record a confirmed AMS selection without booking material.

    This updates ``letzte Verwendung`` when the printer loads/switches to a
    spool but no measurable extrusion happens afterwards.  It deliberately
    stores no weight or length, so inventory consumption remains unchanged.
    """
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM spools WHERE id = ? AND deleted = 0", (int(spool_id),)).fetchone():
            raise KeyError(f"No spool with ID {spool_id}")
        now = occurred_at or _now()
        conn.execute(
            "INSERT INTO spool_consumption_events(spool_id, weight_grams, length_mm, occurred_at, created_at) VALUES (?, NULL, NULL, ?, ?)",
            (int(spool_id), now, _now()),
        )


def set_filament_extra(filament_id: int | str, values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    if "nozzle_temperature" in normalized:
        value = normalized.pop("nozzle_temperature")
        minimum, maximum = _temperature_range(value)
        normalized["nozzle_temp_min"] = minimum
        normalized["nozzle_temp_max"] = maximum
    allowed = {key: value for key, value in normalized.items() if key in {"nozzle_temp_min", "nozzle_temp_max", "cali_idx", "bambu_filament_id", "bambu_setting_id"}}
    with _connect() as conn:
        if allowed:
            conn.execute(
                f"UPDATE filaments SET {','.join(f'{key} = ?' for key in allowed)}, updated_at = ? WHERE id = ? AND deleted = 0",
                list(allowed.values()) + [_now(), int(filament_id)],
            )
    return get_spool_for_filament(int(filament_id))


def get_spool_for_filament(filament_id: int) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT id FROM spools WHERE filament_id = ? AND deleted = 0 ORDER BY id LIMIT 1", (filament_id,)).fetchone()
    return get_spool(row["id"]) if row else {"id": filament_id, "filament": {"id": filament_id}}
