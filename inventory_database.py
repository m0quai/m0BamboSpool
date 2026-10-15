"""Local, normalized spool inventory stored in the shared m0BamboSpool DB."""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATABASE_PATH, DATABASE_TYPE
from filament_color_catalog import SUNLU_COLOR_OPTIONS

DEFAULT_MATERIALS = (
    "PLA", "PLA+", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "HIPS",
)


def _connect() -> sqlite3.Connection:
    if DATABASE_TYPE != "sqlite":
        raise RuntimeError(f"Unsupported inventory database type: {DATABASE_TYPE}")
    path = DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def initialize() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vendors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE
            );
            CREATE TABLE IF NOT EXISTS colors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                hex_code TEXT NOT NULL COLLATE NOCASE UNIQUE
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
                extruder_temp INTEGER,
                bed_temp INTEGER,
                nozzle_temp INTEGER,
                cali_idx INTEGER,
                bambu_filament_id TEXT,
                bambu_setting_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filament_id INTEGER NOT NULL REFERENCES filaments(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                price REAL,
                archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                initial_weight REAL,
                spool_weight REAL,
                weight_correction REAL NOT NULL DEFAULT 0,
                tag TEXT,
                active_tray TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_spools_tag
                ON spools(tag COLLATE NOCASE) WHERE tag IS NOT NULL AND trim(tag) <> '';
            CREATE TABLE IF NOT EXISTS inventory_migrations (
                name TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spool_consumption_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                spool_id INTEGER NOT NULL REFERENCES spools(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                weight_grams REAL,
                length_mm REAL,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        conn.executemany(
            "INSERT OR IGNORE INTO materials(name) VALUES (?)",
            [(name,) for name in DEFAULT_MATERIALS],
        )
        # The initial palette is kept locally so every color selector uses the
        # same editable source instead of a hard-coded template list.
        conn.executemany(
            "INSERT OR IGNORE INTO colors(name, hex_code) VALUES (?, ?)",
            [(name, hex_code.upper()) for name, hex_code in SUNLU_COLOR_OPTIONS],
        )
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
        for column, definition in (
            ("bambu_filament_id", "TEXT"),
            ("bambu_setting_id", "TEXT"),
            ("weight", "REAL"),
            ("spool_weight", "REAL"),
            ("price", "REAL"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE filaments ADD COLUMN {column} {definition}")
        conn.executescript("""
            DROP VIEW IF EXISTS v_filaments;
            CREATE VIEW v_filaments AS
            SELECT f.*, c.name AS color_name, c.hex_code AS color_hex,
                   v.name AS vendor_name, m.name AS material_name
            FROM filaments f
            LEFT JOIN colors c ON c.id = f.color_id
            LEFT JOIN vendors v ON v.id = f.vendor_id
            LEFT JOIN materials m ON m.id = f.material_id;
        """)


def _color_id(conn: sqlite3.Connection, value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and int(value) == value:
        return int(value)
    normalized = str(value).strip().lstrip("#").upper()
    row = conn.execute("SELECT id FROM colors WHERE upper(replace(hex_code, '#', '')) = ?", (normalized,)).fetchone()
    if row:
        return int(row["id"])
    cursor = conn.execute("INSERT INTO colors(name, hex_code) VALUES (?, ?)", (f"Farbe #{normalized}", normalized))
    return int(cursor.lastrowid)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def import_spoolman(vendors: list[dict[str, Any]], filaments: list[dict[str, Any]], spools: list[dict[str, Any]]) -> int:
    """Import the remote catalog once, retaining source IDs where available."""
    initialize()
    now = _now()
    imported = 0
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM inventory_migrations WHERE name = 'spoolman-import'").fetchone():
            return 0
        local_count = sum(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("vendors", "filaments", "spools"))
        if local_count:
            conn.execute("INSERT INTO inventory_migrations(name, completed_at) VALUES ('spoolman-import', ?)", (now,))
            return 0
        for vendor in vendors:
            vendor_id = vendor.get("id")
            vendor_name = vendor.get("name")
            if vendor_id and vendor_name:
                conn.execute("INSERT OR IGNORE INTO vendors(id, name) VALUES (?, ?)", (vendor_id, vendor_name))
        vendor_ids = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM vendors")}
        for filament in filaments:
            filament_id = filament.get("id")
            if not filament_id:
                continue
            vendor = filament.get("vendor") or {}
            vendor_id = vendor.get("id") or vendor_ids.get(vendor.get("name"))
            if vendor.get("id") and vendor.get("name") and not conn.execute("SELECT 1 FROM vendors WHERE id = ?", (vendor.get("id"),)).fetchone():
                conn.execute("INSERT OR IGNORE INTO vendors(id, name) VALUES (?, ?)", (vendor["id"], vendor["name"]))
                vendor_id = vendor["id"]
            material_name = filament.get("material")
            material_id = _import_named(conn, "materials", None, material_name) if material_name else None
            conn.execute(
                """INSERT OR IGNORE INTO filaments
                   (id, vendor_id, material_id, name, color_id, diameter, weight, spool_weight, price,
                    extruder_temp, bed_temp, nozzle_temp, cali_idx, bambu_filament_id, bambu_setting_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (filament_id, vendor_id, material_id, filament.get("name") or f"Filament {filament_id}",
                 _color_id(conn, filament.get("color_id") or filament.get("color_hex")), _db_value(filament.get("diameter")) or 1.75,
                 _db_value(filament.get("weight")), _db_value(filament.get("spool_weight")), _db_value(filament.get("price")),
                 _db_value(filament.get("settings_extruder_temp")), _db_value(filament.get("settings_bed_temp")),
                 _db_value(_extra_value(filament.get("extra"), "nozzle_temperature")), _db_value(_extra_value(filament.get("extra"), "cali_idx")),
                 _db_value(_extra_value(filament.get("extra"), "filament_id")), _db_value(_extra_value(filament.get("extra"), "setting_id")), now, now),
            )
        for spool in spools:
            filament = spool.get("filament") or {}
            filament_id = filament.get("id")
            if not filament_id:
                continue
            extras = spool.get("extra") or {}
            tag = _extra_value(extras, "tag")
            if tag and conn.execute("SELECT 1 FROM spools WHERE tag = ? COLLATE NOCASE", (str(tag),)).fetchone():
                tag = None
            conn.execute(
                """INSERT OR IGNORE INTO spools
                   (id, filament_id, price, archived, initial_weight, spool_weight, weight_correction, tag, active_tray, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                (spool.get("id"), filament_id,
                 spool.get("price") if spool.get("price") is not None else filament.get("price"),
                 int(bool(spool.get("archived"))),
                 spool.get("initial_weight") if spool.get("initial_weight") is not None else filament.get("weight"),
                 spool.get("spool_weight") if spool.get("spool_weight") is not None else filament.get("spool_weight"),
                 tag, _extra_value(extras, "active_tray"), now, now),
            )
            imported += 1
        conn.execute("INSERT INTO inventory_migrations(name, completed_at) VALUES ('spoolman-import', ?)", (now,))
    return imported


def needs_spoolman_import() -> bool:
    initialize()
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM inventory_migrations WHERE name = 'spoolman-import'"
        ).fetchone() is None


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
    # SQLite accepts scalar values only; preserve structured Spoolman extras as JSON.
    if isinstance(value, (list, dict, tuple)):
        import json
        return json.dumps(value, ensure_ascii=False)
    return value


def _import_named(conn: sqlite3.Connection, table: str, entity_id: Any, name: Any) -> int | None:
    if not name:
        return None
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE", (str(name),)).fetchone()
    if row:
        return int(row["id"])
    if entity_id is not None:
        conn.execute(f"INSERT OR IGNORE INTO {table}(id, name) VALUES (?, ?)", (entity_id, str(name)))
        found = conn.execute(f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE", (str(name),)).fetchone()
    else:
        cur = conn.execute(f"INSERT INTO {table}(name) VALUES (?)", (str(name),))
        return int(cur.lastrowid)
    return int(found["id"]) if found else None


def list_entities(entity: str) -> list[dict[str, Any]]:
    initialize()
    if entity == "filaments":
        return list_filaments()
    tables = {"vendors": "vendors", "materials": "materials", "filaments": "filaments"}
    table = tables[entity]
    with _connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY name COLLATE NOCASE, id")]


def list_colors() -> list[dict[str, Any]]:
    """Return the local color catalog used by all filament color selectors."""
    initialize()
    with _connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id, name, hex_code FROM colors ORDER BY name COLLATE NOCASE, id"
        )]


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
        if conn.execute("SELECT 1 FROM filaments WHERE id = ?", (filament_id,)).fetchone():
            diameter = _db_value(filament.get("diameter"))
            if diameter is not None:
                conn.execute(
                    "UPDATE filaments SET diameter = ?, updated_at = ? WHERE id = ?",
                    (diameter, now, filament_id),
                )
            return
        vendor_row = conn.execute("SELECT id FROM vendors WHERE name = ? COLLATE NOCASE", (vendor_name,)).fetchone()
        if vendor_row:
            vendor_id = int(vendor_row["id"])
        else:
            cursor = conn.execute("INSERT INTO vendors(name) VALUES (?)", (vendor_name,))
            vendor_id = int(cursor.lastrowid)
        material_row = conn.execute("SELECT id FROM materials WHERE name = ? COLLATE NOCASE", (material_name,)).fetchone()
        if material_row:
            material_id = int(material_row["id"])
        else:
            cursor = conn.execute("INSERT INTO materials(name) VALUES (?)", (material_name,))
            material_id = int(cursor.lastrowid)
        conn.execute(
            """INSERT INTO filaments
               (id, vendor_id, material_id, name, color_id, diameter, weight, spool_weight, price,
                extruder_temp, bed_temp, nozzle_temp, cali_idx, bambu_filament_id, bambu_setting_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(filament_id), vendor_id, material_id, filament.get("name") or f"Filament {filament_id}",
                _color_id(conn, filament.get("color_id") or filament.get("color_hex")), _db_value(filament.get("diameter")) or 1.75,
                _db_value(filament.get("weight")), _db_value(filament.get("spool_weight")), _db_value(filament.get("price")),
                _db_value(filament.get("settings_extruder_temp")), _db_value(filament.get("settings_bed_temp")),
                _db_value(_extra_value(filament.get("extra"), "nozzle_temperature")), _db_value(_extra_value(filament.get("extra"), "cali_idx")),
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
        if conn.execute("SELECT 1 FROM spools WHERE id = ?", (spool_id,)).fetchone():
            return
        extras = spool.get("extra") or {}
        conn.execute(
            """INSERT INTO spools
               (id, filament_id, price, archived, initial_weight, spool_weight, weight_correction, tag, active_tray, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (
                int(spool_id), int(filament_id), spool.get("price") if spool.get("price") is not None else filament.get("price"),
                int(bool(spool.get("archived"))), spool.get("initial_weight") if spool.get("initial_weight") is not None else filament.get("weight"),
                spool.get("spool_weight") if spool.get("spool_weight") is not None else filament.get("spool_weight"),
                _extra_value(extras, "tag"), _extra_value(extras, "active_tray"), now, now,
            ),
        )


def save_entity(entity: str, values: dict[str, Any], entity_id: int | None = None) -> int:
    initialize()
    columns = {
            "vendors": ("name",),
        "materials": ("name",),
        "filaments": (
            "vendor_id", "material_id", "name", "color_id", "diameter",
            "weight", "spool_weight", "price", "extruder_temp", "bed_temp",
            "nozzle_temp", "cali_idx", "bambu_filament_id", "bambu_setting_id",
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
        row["color_id"] = values.get("color_id") or values.get("color_hex")
        row["diameter"] = _number(row["diameter"]) if row["diameter"] not in (None, "") else 1.75
        for field in ("weight", "spool_weight", "price", "extruder_temp", "bed_temp", "nozzle_temp", "cali_idx"):
            if row[field] not in (None, ""):
                row[field] = _number(row[field])
    now = _now()
    with _connect() as conn:
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


def update_filament_fields(filament_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Update only changed local filament fields for the inline inventory editor."""
    initialize()
    editable = {
        "vendor_id", "material_id", "name", "color_id", "diameter",
        "weight", "spool_weight", "price", "extruder_temp", "bed_temp",
        "nozzle_temp", "cali_idx", "bambu_filament_id", "bambu_setting_id",
    }
    updates = {key: value for key, value in values.items() if key in editable or key == "color_hex"}
    if not updates:
        raise ValueError("No editable filament field was supplied")
    numeric_fields = {"vendor_id", "material_id", "diameter", "weight", "spool_weight", "price", "extruder_temp", "bed_temp", "nozzle_temp", "cali_idx"}
    for key, value in list(updates.items()):
        if value == "":
            updates[key] = None
        elif key in numeric_fields:
            updates[key] = 1.75 if key == "diameter" and value == "" else (int(_number(value)) if key in {"vendor_id", "material_id", "extruder_temp", "bed_temp", "nozzle_temp", "cali_idx"} else _number(value))
        elif key in {"color_hex", "color_id"}:
            updates["color_id"] = value
            if key == "color_hex":
                updates.pop("color_hex", None)
        elif key in {"name", "bambu_filament_id", "bambu_setting_id"}:
            updates[key] = str(value).strip() or None
    if "name" in updates and not updates["name"]:
        raise ValueError("Name is required")
    with _connect() as conn:
        previous = conn.execute(
            "SELECT weight, spool_weight, price FROM filaments WHERE id = ?", (filament_id,)
        ).fetchone()
        if not previous:
            raise KeyError(f"No filament with ID {filament_id}")
        if "color_id" in updates:
            updates["color_id"] = _color_id(conn, updates["color_id"])
        updates["updated_at"] = _now()
        conn.execute(
            f"UPDATE filaments SET {','.join(f'{key} = ?' for key in updates)} WHERE id = ?",
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
                f"UPDATE spools SET {spool_field} = NULL, updated_at = ? WHERE filament_id = ? AND {condition}",
                [_now(), *params],
            )
    return next(item for item in list_filaments() if int(item["id"]) == int(filament_id))


def update_vendor_fields(vendor_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Persist a single local vendor edit from the inline inventory editor."""
    name = str(values.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required")
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM vendors WHERE id = ?", (vendor_id,)).fetchone():
            raise KeyError(f"No vendor with ID {vendor_id}")
        conn.execute("UPDATE vendors SET name = ? WHERE id = ?", (name, vendor_id))
    return next(item for item in list_entities("vendors") if int(item["id"]) == int(vendor_id))


def _spool_has_bookings(conn: sqlite3.Connection, spool_id: int) -> bool:
    """A booked spool must retain its original filament master record."""
    spool = conn.execute(
        "SELECT weight_correction FROM spools WHERE id = ?", (spool_id,)
    ).fetchone()
    if spool and abs(float(spool["weight_correction"] or 0)) > 0.000001:
        return True
    if conn.execute(
        "SELECT 1 FROM spool_consumption_events WHERE spool_id = ? LIMIT 1", (spool_id,)
    ).fetchone():
        return True
    has_usage_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spool_usage'"
    ).fetchone()
    return bool(has_usage_table and conn.execute(
        "SELECT 1 FROM spool_usage WHERE spool_id = ? LIMIT 1", (spool_id,)
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
            AND t.status IN ('PREPARING', 'RUNNING', 'PAUSED')
          LIMIT 1""",
        (int(spool_id),),
    ).fetchone())


def update_spool_fields(spool_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Persist only the explicitly changed local spool fields."""
    initialize()
    editable = {"filament_id", "price", "archived", "initial_weight", "spool_weight", "tag", "active_tray"}
    updates = {key: value for key, value in values.items() if key in editable}
    if not updates:
        raise ValueError("No editable spool field was supplied")
    numeric_fields = {"filament_id", "price", "initial_weight", "spool_weight", "weight_correction"}
    for key, value in list(updates.items()):
        if key in {"tag", "active_tray"}:
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
        current = conn.execute("SELECT filament_id FROM spools WHERE id = ?", (spool_id,)).fetchone()
        if not current:
            raise KeyError(f"No spool with ID {spool_id}")
        if _spool_is_in_active_print(conn, spool_id):
            raise ValueError("Die Spule ist im aktuell laufenden Druck gebunden und gesperrt")
        if ("filament_id" in updates and int(updates["filament_id"]) != int(current["filament_id"])
                and _spool_has_bookings(conn, spool_id)):
            raise ValueError("Das Filament einer gebuchten Spule kann nicht mehr geändert werden")
        if "filament_id" in updates and not conn.execute("SELECT 1 FROM filaments WHERE id = ?", (updates["filament_id"],)).fetchone():
            raise ValueError("Unknown filament")
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
            """SELECT s.initial_weight, s.weight_correction, f.weight AS filament_weight
                 FROM spools s JOIN filaments f ON f.id = s.filament_id
                WHERE s.id = ?""",
            (int(spool_id),),
        ).fetchone()
        if not spool:
            raise KeyError(f'No spool with ID {spool_id}')
        if _spool_is_in_active_print(conn, spool_id):
            raise ValueError('Die Spule ist im aktuell laufenden Druck gebunden und gesperrt')
        used = float(conn.execute(
            'SELECT COALESCE(SUM(grams_used), 0) FROM spool_usage WHERE spool_id = ?',
            (int(spool_id),),
        ).fetchone()[0] or 0)
        initial = float(spool['initial_weight'] or 0)
        maximum = initial or float(spool['filament_weight'] or 0)
        correction = float(spool['weight_correction'] or 0)
        if mode == 'absolute':
            if maximum > 0 and value > maximum + 0.000001:
                raise ValueError(f'Das Gewicht darf höchstens {maximum:g} g betragen')
            correction = value - maximum + used
        else:
            if maximum > 0 and maximum + correction + value - used > maximum + 0.000001:
                raise ValueError(f'Das Gewicht darf höchstens {maximum:g} g betragen')
            correction += value
        conn.execute(
            'UPDATE spools SET weight_correction = ?, updated_at = ? WHERE id = ?',
            (correction, _now(), int(spool_id)),
        )
    return get_spool(spool_id)


def delete_entity(entity: str, entity_id: int) -> None:
    if entity not in {"vendors", "materials", "filaments"}:
        raise ValueError("Unknown inventory entity")
    with _connect() as conn:
        conn.execute(f"DELETE FROM {entity} WHERE id = ?", (entity_id,))


def save_spool(values: dict[str, Any], spool_id: int | None = None) -> int:
    initialize()
    if spool_id is not None and "weight_correction" in values:
        raise ValueError("Die Bestandskorrektur kann nur über die Bestandsveränderung geändert werden")
    fields = ("filament_id", "price", "archived", "initial_weight", "spool_weight", "weight_correction", "tag", "active_tray")
    row = {field: values.get(field) for field in fields}
    for field in ("price", "initial_weight", "spool_weight"):
        if row[field] not in (None, ""):
            row[field] = _number(row[field])
            if row[field] == 0:
                row[field] = None
    row["archived"] = int(row["archived"] is True or str(row["archived"]).strip().lower() in {"1", "true", "yes", "on"})
    row["weight_correction"] = _number(row["weight_correction"] or 0)
    now = _now()
    with _connect() as conn:
        existing = conn.execute("SELECT filament_id, tag, active_tray FROM spools WHERE id = ?", (spool_id,)).fetchone() if spool_id is not None else None
        if existing and _spool_is_in_active_print(conn, spool_id):
            raise ValueError("Die Spule ist im aktuell laufenden Druck gebunden und gesperrt")
        if (existing and "filament_id" in values and str(row["filament_id"]) != str(existing["filament_id"])
                and _spool_has_bookings(conn, spool_id)):
            raise ValueError("Das Filament einer gebuchten Spule kann nicht mehr geändert werden")
        row["tag"] = str(row["tag"] or "").strip() or None if "tag" in values else (existing["tag"] if existing else None)
        row["active_tray"] = str(row["active_tray"] or "").strip() or None if "active_tray" in values else (existing["active_tray"] if existing else None)
        measured_remaining = values.get("current_remaining_weight")
        adjustment_value = values.get("adjustment_weight")
        adjustment_mode = str(values.get("adjustment_mode") or "").strip().lower()
        if measured_remaining not in (None, ""):
            used = 0.0
            if spool_id is not None:
                used = float(conn.execute(
                    "SELECT COALESCE(SUM(grams_used), 0) FROM spool_usage WHERE spool_id = ?",
                    (spool_id,),
                ).fetchone()[0] or 0)
            row["weight_correction"] = _number(measured_remaining) - float(row["initial_weight"] or 0) + used
        elif adjustment_value not in (None, "") and spool_id is not None:
            previous = conn.execute("SELECT weight_correction FROM spools WHERE id = ?", (spool_id,)).fetchone()
            previous_correction = float(previous["weight_correction"] or 0) if previous else 0.0
            amount = _number(adjustment_value)
            if adjustment_mode == "absolute":
                used = float(conn.execute(
                    "SELECT COALESCE(SUM(grams_used), 0) FROM spool_usage WHERE spool_id = ?",
                    (spool_id,),
                ).fetchone()[0] or 0)
                row["weight_correction"] = amount - float(row["initial_weight"] or 0) + used
            else:
                row["weight_correction"] = previous_correction + amount
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
            return int(cur.lastrowid)
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
        usage_select = "COALESCE(u.used_weight, 0)" if has_usage_table else "0"
        usage_join = """LEFT JOIN (SELECT spool_id, SUM(grams_used) AS used_weight
                                   FROM spool_usage WHERE spool_id IS NOT NULL GROUP BY spool_id) u
                              ON u.spool_id = s.id""" if has_usage_table else ""
        usage_booking = "OR EXISTS (SELECT 1 FROM spool_usage fu WHERE fu.spool_id = s.id)" if has_usage_table else ""
        active_print_select = """CASE WHEN EXISTS (
                         SELECT 1 FROM spool_usage au
                         JOIN print_layer_tracking at ON at.print_id = au.print_id
                         WHERE au.spool_id = s.id
                           AND at.status IN ('PREPARING', 'RUNNING', 'PAUSED')
                       ) THEN 1 ELSE 0 END""" if has_usage_table else "0"
        sql = f"""SELECT s.*, f.name AS filament_name, c.hex_code AS color_hex, f.diameter,
                    f.weight AS filament_default_weight,
                    f.spool_weight AS filament_default_spool_weight,
                    f.price AS filament_default_price, f.extruder_temp,
                    f.bed_temp, f.nozzle_temp, f.cali_idx, f.bambu_filament_id,
                    f.bambu_setting_id, v.id AS vendor_id,
                    v.name AS vendor_name, m.id AS material_id, m.name AS material,
                    {usage_select} AS used_weight,
                    CASE WHEN ABS(COALESCE(s.weight_correction, 0)) > 0.000001
                              OR EXISTS (SELECT 1 FROM spool_consumption_events ce WHERE ce.spool_id = s.id)
                              {usage_booking}
                         THEN 1 ELSE 0 END AS filament_locked
                    ,{active_print_select} AS active_print_locked
             FROM spools s JOIN filaments f ON f.id = s.filament_id
             LEFT JOIN vendors v ON v.id = f.vendor_id
             LEFT JOIN materials m ON m.id = f.material_id
             LEFT JOIN colors c ON c.id = f.color_id
             {usage_join}"""
        if not include_archived:
            sql += " WHERE s.archived = 0"
        sql += " ORDER BY s.id"
        return [_as_spool(row) for row in conn.execute(sql)]


def get_spool(spool_id: int | str) -> dict[str, Any]:
    for spool in list_spools(include_archived=True):
        if str(spool["id"]) == str(spool_id):
            return spool
    raise KeyError(f"No spool with ID {spool_id}")


def duplicate_spool(spool_id: int | str) -> int:
    """Create a fresh, unbooked copy of a local spool's settings."""
    source = get_spool(spool_id)
    if source.get('active_print_locked'):
        raise ValueError('Die Spule ist im aktuell laufenden Druck gebunden und gesperrt')
    defaults = source.get("filament_defaults") or {}
    values = {
        "filament_id": source["filament_id"],
        "price": None if defaults.get("price") else source.get("price"),
        "initial_weight": None if defaults.get("initial_weight") else source.get("initial_weight"),
        "spool_weight": None if defaults.get("spool_weight") else source.get("spool_weight"),
        "weight_correction": 0,
        "tag": None,
        "active_tray": None,
        "archived": False,
    }
    return save_spool(values)


def delete_spool(spool_id: int | str) -> None:
    initialize()
    with _connect() as conn:
        spool_id = int(spool_id)
        if not conn.execute("SELECT 1 FROM spools WHERE id = ?", (spool_id,)).fetchone():
            raise KeyError(f"No spool with ID {spool_id}")
        if _spool_is_in_active_print(conn, spool_id):
            raise ValueError("Die Spule ist im aktuell laufenden Druck gebunden und gesperrt")
        if _spool_has_bookings(conn, spool_id):
            raise ValueError("Eine gebuchte Spule kann nicht gelöscht werden")
        conn.execute("DELETE FROM spools WHERE id = ?", (spool_id,))


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
    spool["filament"] = {
        "id": spool["filament_id"], "name": spool.pop("filament_name"),
        "material": spool.pop("material"), "color_hex": spool.pop("color_hex"),
        "diameter": spool.pop("diameter"),
        "vendor": {"id": spool.pop("vendor_id"), "name": spool.pop("vendor_name")},
        "weight": resolved_filament_defaults["initial_weight"], "price": resolved_filament_defaults["price"],
        "spool_weight": resolved_filament_defaults["spool_weight"],
        "settings_extruder_temp": spool.pop("extruder_temp"),
        "settings_bed_temp": spool.pop("bed_temp"),
        "extra": {
            "nozzle_temperature": json.dumps(spool.pop("nozzle_temp")) if spool.get("nozzle_temp") is not None else None,
            "cali_idx": json.dumps(spool.pop("cali_idx")) if spool.get("cali_idx") is not None else None,
            "filament_id": json.dumps(spool.pop("bambu_filament_id")) if spool.get("bambu_filament_id") is not None else None,
            "setting_id": json.dumps(spool.pop("bambu_setting_id")) if spool.get("bambu_setting_id") is not None else None,
        },
    }
    spool["extra"] = {
        "tag": json.dumps(spool.get("tag")) if spool.get("tag") is not None else None,
        "active_tray": json.dumps(spool.get("active_tray")) if spool.get("active_tray") is not None else None,
    }
    spool["archived"] = bool(spool["archived"])
    return spool


def set_spool_extra(spool_id: int | str, values: dict[str, Any]) -> None:
    import json
    fields = {}
    for key in ("tag", "active_tray"):
        if key in values:
            value = values[key]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    pass
            fields[key] = str(value or "").strip() or None
    if fields:
        fields["updated_at"] = _now()
        with _connect() as conn:
            conn.execute(f"UPDATE spools SET {','.join(f'{key} = ?' for key in fields)} WHERE id = ?", list(fields.values()) + [int(spool_id)])


def record_consumption(spool_id: int | str, weight_grams: float | None = None,
                       length_mm: float | None = None, occurred_at: str | None = None) -> None:
    if weight_grams is None and length_mm is None:
        raise ValueError("weight_grams or length_mm is required")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO spool_consumption_events(spool_id, weight_grams, length_mm, occurred_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(spool_id), weight_grams, length_mm, occurred_at or _now(), _now()),
        )


def set_filament_extra(filament_id: int | str, values: dict[str, Any]) -> dict[str, Any]:
    allowed = {key: value for key, value in values.items() if key in {"nozzle_temp", "cali_idx", "bambu_filament_id", "bambu_setting_id"}}
    with _connect() as conn:
        if allowed:
            conn.execute(
                f"UPDATE filaments SET {','.join(f'{key} = ?' for key in allowed)}, updated_at = ? WHERE id = ?",
                list(allowed.values()) + [_now(), int(filament_id)],
            )
    return get_spool_for_filament(int(filament_id))


def get_spool_for_filament(filament_id: int) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT id FROM spools WHERE filament_id = ? ORDER BY id LIMIT 1", (filament_id,)).fetchone()
    return get_spool(row["id"]) if row else {"id": filament_id, "filament": {"id": filament_id}}
