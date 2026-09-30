"""Local, normalized spool inventory stored in the shared OpenSpoolMan DB."""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATABASE_PATH, DATABASE_TYPE

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
            CREATE TABLE IF NOT EXISTS filaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor_id INTEGER REFERENCES vendors(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                material_id INTEGER REFERENCES materials(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                name TEXT NOT NULL,
                color_hex TEXT,
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
        # Keep migrations compatible with osm.db files created before Bambu
        # profile IDs became part of the internal filament record.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(filaments)")}
        for column in ("bambu_filament_id", "bambu_setting_id"):
            if column not in columns:
                conn.execute(f"ALTER TABLE filaments ADD COLUMN {column} TEXT")


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
                   (id, vendor_id, material_id, name, color_hex, extruder_temp, bed_temp, nozzle_temp, cali_idx,
                    bambu_filament_id, bambu_setting_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (filament_id, vendor_id, material_id, filament.get("name") or f"Filament {filament_id}",
                 _db_value(filament.get("color_hex")), _db_value(filament.get("settings_extruder_temp")), _db_value(filament.get("settings_bed_temp")),
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
    tables = {"vendors": "vendors", "materials": "materials", "filaments": "filaments"}
    table = tables[entity]
    with _connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY name COLLATE NOCASE, id")]


def list_filaments() -> list[dict[str, Any]]:
    initialize()
    with _connect() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT f.*, v.name AS vendor_name, m.name AS material_name
               FROM filaments f LEFT JOIN vendors v ON v.id = f.vendor_id
               LEFT JOIN materials m ON m.id = f.material_id
               ORDER BY f.name COLLATE NOCASE, f.id"""
        )]


def save_entity(entity: str, values: dict[str, Any], entity_id: int | None = None) -> int:
    initialize()
    columns = {
        "vendors": ("name",),
        "materials": ("name",),
        "filaments": ("vendor_id", "material_id", "name", "color_hex", "extruder_temp", "bed_temp", "nozzle_temp", "cali_idx"),
    }
    if entity not in columns:
        raise ValueError("Unknown inventory entity")
    fields = columns[entity]
    row = {field: values.get(field) for field in fields}
    if not str(row.get("name") or "").strip():
        raise ValueError("Name is required")
    row["name"] = str(row["name"]).strip()
    if entity == "filaments":
        row["color_hex"] = (str(row["color_hex"]).strip().lstrip("#") or None) if row["color_hex"] is not None else None
    now = _now()
    with _connect() as conn:
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


def delete_entity(entity: str, entity_id: int) -> None:
    if entity not in {"vendors", "materials", "filaments"}:
        raise ValueError("Unknown inventory entity")
    with _connect() as conn:
        conn.execute(f"DELETE FROM {entity} WHERE id = ?", (entity_id,))


def save_spool(values: dict[str, Any], spool_id: int | None = None) -> int:
    initialize()
    fields = ("filament_id", "price", "archived", "initial_weight", "spool_weight", "weight_correction", "tag", "active_tray")
    row = {field: values.get(field) for field in fields}
    row["archived"] = int(row["archived"] is True or str(row["archived"]).strip().lower() in {"1", "true", "yes", "on"})
    row["weight_correction"] = float(row["weight_correction"] or 0)
    now = _now()
    with _connect() as conn:
        existing = conn.execute("SELECT tag, active_tray FROM spools WHERE id = ?", (spool_id,)).fetchone() if spool_id is not None else None
        row["tag"] = str(row["tag"] or "").strip() or None if "tag" in values else (existing["tag"] if existing else None)
        row["active_tray"] = str(row["active_tray"] or "").strip() or None if "active_tray" in values else (existing["active_tray"] if existing else None)
        measured_remaining = values.get("current_remaining_weight")
        adjustment_value = values.get("adjustment_weight")
        adjustment_mode = str(values.get("adjustment_mode") or "").strip().lower()
        if measured_remaining not in (None, ""):
            used = 0.0
            if spool_id is not None:
                used = float(conn.execute(
                    "SELECT COALESCE(SUM(grams_used), 0) FROM filament_usage WHERE spool_id = ?",
                    (spool_id,),
                ).fetchone()[0] or 0)
            row["weight_correction"] = float(measured_remaining) - float(row["initial_weight"] or 0) + used
        elif adjustment_value not in (None, "") and spool_id is not None:
            previous = conn.execute("SELECT weight_correction FROM spools WHERE id = ?", (spool_id,)).fetchone()
            previous_correction = float(previous["weight_correction"] or 0) if previous else 0.0
            amount = float(adjustment_value)
            if adjustment_mode == "absolute":
                used = float(conn.execute(
                    "SELECT COALESCE(SUM(grams_used), 0) FROM filament_usage WHERE spool_id = ?",
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
    sql = """SELECT s.*, f.name AS filament_name, f.color_hex, f.extruder_temp,
                    f.bed_temp, f.nozzle_temp, f.cali_idx, f.bambu_filament_id,
                    f.bambu_setting_id, v.id AS vendor_id,
                    v.name AS vendor_name, m.id AS material_id, m.name AS material,
                    COALESCE(u.used_weight, 0) AS used_weight
             FROM spools s JOIN filaments f ON f.id = s.filament_id
             LEFT JOIN vendors v ON v.id = f.vendor_id
             LEFT JOIN materials m ON m.id = f.material_id
             LEFT JOIN (SELECT spool_id, SUM(grams_used) AS used_weight
                        FROM filament_usage WHERE spool_id IS NOT NULL GROUP BY spool_id) u
                    ON u.spool_id = s.id"""
    if not include_archived:
        sql += " WHERE s.archived = 0"
    sql += " ORDER BY s.id"
    with _connect() as conn:
        return [_as_spool(row) for row in conn.execute(sql)]


def get_spool(spool_id: int | str) -> dict[str, Any]:
    for spool in list_spools(include_archived=True):
        if str(spool["id"]) == str(spool_id):
            return spool
    raise KeyError(f"No spool with ID {spool_id}")


def _as_spool(row: sqlite3.Row) -> dict[str, Any]:
    import json
    spool = dict(row)
    used = float(spool.pop("used_weight") or 0)
    initial = float(spool.get("initial_weight") or 0)
    correction = float(spool.get("weight_correction") or 0)
    spool["remaining_weight"] = max(0.0, initial + correction - used)
    spool["used_weight"] = used
    spool["filament"] = {
        "id": spool["filament_id"], "name": spool.pop("filament_name"),
        "material": spool.pop("material"), "color_hex": spool.pop("color_hex"),
        "vendor": {"id": spool.pop("vendor_id"), "name": spool.pop("vendor_name")},
        "weight": spool.get("initial_weight"), "price": spool.get("price"),
        "spool_weight": spool.get("spool_weight"),
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
