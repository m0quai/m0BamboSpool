import sqlite3
import math
import time
from datetime import datetime
from pathlib import Path
from config import DATABASE_NAME, DATABASE_PATH, DATABASE_TYPE
from database import connect_database

db_config = {
    "db_path": str(DATABASE_PATH),
    "db_name": DATABASE_NAME,
    "db_type": DATABASE_TYPE,
}  # Compatibility for existing callers.


def printer_state_to_history_status(state: str | None, print_error=None) -> str | None:
    state = str(state or "").upper()
    if state == "FAILED":
        try:
            if print_error is not None and int(print_error) in {0, 50348044}:
                return "ABORTED"
        except (TypeError, ValueError):
            pass
        return "FAILED"
    return {
        "PREPARE": "PREPARING",
        "RUNNING": "RUNNING",
        "PAUSE": "PAUSED",
        "FINISH": "COMPLETED",
        "STOP": "ABORTED",
        "CANCEL": "ABORTED",
        "CANCELED": "ABORTED",
        "CANCELLED": "ABORTED",
        "ABORT": "ABORTED",
        "ABORTED": "ABORTED",
    }.get(state)


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def create_database() -> None:
    #
    # Ensure the SQLite schema exists (used for both fresh and upgrading databases).
    db_path = Path(db_config["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect_database(db_path)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS prints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_date TEXT NOT NULL,
            file_name TEXT NOT NULL,
            print_type TEXT NOT NULL,
            image_file TEXT,
            file_name_source TEXT NOT NULL DEFAULT 'legacy',
            deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
        )
    ''')

    tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "spool_usage" not in tables and "filament_usage" in tables:
        cursor.execute("ALTER TABLE filament_usage RENAME TO spool_usage")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS spool_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_id INTEGER NOT NULL,
            spool_id INTEGER,
            grams_used REAL NOT NULL,
            ams_slot INTEGER NOT NULL,
            estimated_grams REAL,
            length_used REAL,
            estimated_length REAL,
            calculated_length REAL,
            consumed_length REAL,
            gcode_filament_type TEXT,
            gcode_color TEXT,
            deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
        FOREIGN KEY (print_id) REFERENCES prints (id) ON DELETE CASCADE
        )
    ''')
    _ensure_column(cursor, "spool_usage", "deleted", "INTEGER NOT NULL DEFAULT 0")
    # Normalize the legacy name and remove descriptive copies that are
    # derivable through spool_id -> spools -> filaments.
    usage_columns = {row[1] for row in cursor.execute("PRAGMA table_info(spool_usage)")}
    for legacy_column in ("filament_type", "color"):
        if legacy_column in usage_columns:
            cursor.execute(f"ALTER TABLE spool_usage DROP COLUMN {legacy_column}")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_spool_usage_print ON spool_usage(print_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_spool_usage_spool ON spool_usage(spool_id)")
    has_spools_table = bool(cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spools'").fetchone())
    if has_spools_table:
        # Older databases may reach this module before the inventory migration.
        # Add the soft-delete columns before the view references them.
        for table in ("spools", "filaments", "materials", "colors"):
            _ensure_column(cursor, table, "deleted", "INTEGER NOT NULL DEFAULT 0")
        cursor.execute("DROP VIEW IF EXISTS v_spool_usage")
        cursor.execute('''
            CREATE VIEW v_spool_usage AS
            SELECT u.*, f.name AS filament_name, m.name AS material_name,
                   COALESCE(u.gcode_color, c.hex_code) AS color_hex,
                   u.gcode_color, u.gcode_filament_type
            FROM spool_usage u
            LEFT JOIN spools s ON s.id = u.spool_id AND s.deleted = 0
            LEFT JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
            LEFT JOIN materials m ON m.id = f.material_id AND m.deleted = 0
            LEFT JOIN colors c ON c.id = f.color_id AND c.deleted = 0
            WHERE u.deleted = 0
        ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS filament_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_id INTEGER NOT NULL,
            layer INTEGER NOT NULL,
            filament_index INTEGER NOT NULL,
            spool_id INTEGER NOT NULL,
            length_used REAL NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('pending', 'sent', 'confirmed')),
            deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
            UNIQUE (print_id, layer, filament_index),
            FOREIGN KEY (print_id) REFERENCES prints (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS print_layer_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_id INTEGER NOT NULL UNIQUE,
            total_layers INTEGER,
            layers_printed INTEGER,
            filament_grams_billed REAL,
            filament_grams_total REAL,
            status TEXT NOT NULL DEFAULT 'RUNNING',
            predicted_end_time TEXT,
            actual_end_time TEXT,
            deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
            FOREIGN KEY (print_id) REFERENCES prints (id) ON DELETE CASCADE
        )
    ''')

    _ensure_column(
        cursor,
        "spool_usage",
        "estimated_grams",
        "REAL",
    )
    _ensure_column(cursor, "prints", "file_name_source", "TEXT NOT NULL DEFAULT 'legacy'")
    _ensure_column(cursor, "prints", "deleted", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cursor, "spool_usage", "deleted", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cursor, "filament_usage_events", "deleted", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cursor, "print_layer_tracking", "deleted", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(
        cursor,
        "spool_usage",
        "length_used",
        "REAL",
    )
    _ensure_column(
        cursor,
        "spool_usage",
        "estimated_length",
        "REAL",
    )
    _ensure_column(cursor, "filament_usage_events", "status", "TEXT NOT NULL DEFAULT 'confirmed'")
    # Events written by older versions already recorded the corresponding usage
    # charge, so they must never become retry candidates after migration.
    cursor.execute("UPDATE filament_usage_events SET status = 'confirmed' WHERE status IS NULL OR status NOT IN ('pending', 'sent', 'confirmed')")
    _ensure_column(cursor, "spool_usage", "calculated_length", "REAL")
    _ensure_column(cursor, "spool_usage", "consumed_length", "REAL")
    cursor.execute("UPDATE spool_usage SET calculated_length = length_used WHERE calculated_length IS NULL AND length_used IS NOT NULL")
    # Legacy completed/failed jobs already had their usage recorded.
    # Backfill the confirmed total while leaving the currently running job open.
    cursor.execute(
        """UPDATE spool_usage
           SET calculated_length = COALESCE(calculated_length, length_used, 0),
               consumed_length = COALESCE(consumed_length, length_used, 0)
           WHERE consumed_length IS NULL
             AND print_id IN (
               SELECT f.print_id
               FROM spool_usage f
               LEFT JOIN print_layer_tracking t ON t.print_id = f.print_id
               WHERE COALESCE(t.status, 'COMPLETED') <> 'RUNNING'
             )"""
    )
    _ensure_column(
        cursor,
        "spool_usage",
        "physical_ams_slot",
        "INTEGER",
    )
    _ensure_column(cursor, "spool_usage", "gcode_filament_type", "TEXT")
    _ensure_column(cursor, "spool_usage", "gcode_color", "TEXT")

    # Ensure column definitions exist for older databases
    _ensure_column(
        cursor,
        "print_layer_tracking",
        "predicted_end_time",
        "TEXT",
    )
    _ensure_column(cursor, "prints", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(
        cursor,
        "print_layer_tracking",
        "actual_end_time",
        "TEXT",
    )
    _ensure_column(cursor, "print_layer_tracking", "completion_source", "TEXT")
    _ensure_column(cursor, "print_layer_tracking", "printer_completion_time", "TEXT")
    _ensure_column(cursor, "print_layer_tracking", "reconciliation_done", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cursor, "print_layer_tracking", "estimated_duration_minutes", "REAL")
    _ensure_column(cursor, "print_layer_tracking", "printer_percent", "REAL")
    _ensure_column(cursor, "print_layer_tracking", "last_status_at", "TEXT")
    _ensure_column(cursor, "print_layer_tracking", "last_usage_event_at", "TEXT")
    _ensure_column(cursor, "print_layer_tracking", "remote_3mf_path", "TEXT")

    # Local Bambu task/subtask identifiers are not stable.  History links a
    # running print by its persisted source path and local print ID instead.
    cursor.execute("PRAGMA table_info(print_layer_tracking)")
    if "printer_job_key" in {row[1] for row in cursor.fetchall()}:
        cursor.execute("ALTER TABLE print_layer_tracking DROP COLUMN printer_job_key")

    # Enforce the cross-module spool references after the legacy tables have
    # been migrated. SQLite requires a table rebuild to add these constraints.
    spool_usage_fk = {row[3] for row in cursor.execute("PRAGMA foreign_key_list(spool_usage)")}
    if has_spools_table and "spool_id" not in spool_usage_fk:
        cursor.execute("DROP VIEW IF EXISTS v_spool_usage")
        cursor.execute("""CREATE TABLE spool_usage_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_id INTEGER NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
            spool_id INTEGER REFERENCES spools(id) ON UPDATE CASCADE ON DELETE RESTRICT,
            grams_used REAL NOT NULL,
            ams_slot INTEGER NOT NULL,
            estimated_grams REAL,
            length_used REAL,
            estimated_length REAL,
            physical_ams_slot INTEGER,
            calculated_length REAL,
            consumed_length REAL,
            gcode_filament_type TEXT,
            gcode_color TEXT,
            deleted INTEGER NOT NULL DEFAULT 0
        )""")
        cursor.execute("""INSERT INTO spool_usage_new
            (id, print_id, spool_id, grams_used, ams_slot, estimated_grams,
             length_used, estimated_length, physical_ams_slot, calculated_length,
             consumed_length, gcode_filament_type, gcode_color, deleted)
            SELECT id, print_id, spool_id, grams_used, ams_slot, estimated_grams,
                   length_used, estimated_length, physical_ams_slot, calculated_length,
                   consumed_length, gcode_filament_type, gcode_color, deleted
            FROM spool_usage""")
        cursor.execute("DROP TABLE spool_usage")
        cursor.execute("ALTER TABLE spool_usage_new RENAME TO spool_usage")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_spool_usage_print ON spool_usage(print_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_spool_usage_spool ON spool_usage(spool_id)")
        cursor.execute('''
            CREATE VIEW v_spool_usage AS
            SELECT u.*, f.name AS filament_name, m.name AS material_name,
                   COALESCE(u.gcode_color, c.hex_code) AS color_hex,
                   u.gcode_color, u.gcode_filament_type
            FROM spool_usage u
            LEFT JOIN spools s ON s.id = u.spool_id AND s.deleted = 0
            LEFT JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
            LEFT JOIN materials m ON m.id = f.material_id AND m.deleted = 0
            LEFT JOIN colors c ON c.id = f.color_id AND c.deleted = 0
            WHERE u.deleted = 0
        ''')

    event_spool_fk = {row[3] for row in cursor.execute("PRAGMA foreign_key_list(filament_usage_events)")}
    if has_spools_table and "spool_id" not in event_spool_fk:
        cursor.execute("""CREATE TABLE filament_usage_events_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            print_id INTEGER NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
            layer INTEGER NOT NULL,
            filament_index INTEGER NOT NULL,
            spool_id INTEGER NOT NULL REFERENCES spools(id) ON UPDATE CASCADE ON DELETE RESTRICT,
            length_used REAL NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('pending', 'sent', 'confirmed')),
            deleted INTEGER NOT NULL DEFAULT 0,
            UNIQUE (print_id, layer, filament_index)
        )""")
        cursor.execute("""INSERT INTO filament_usage_events_new
            (id, print_id, layer, filament_index, spool_id, length_used, created_at, status, deleted)
            SELECT id, print_id, layer, filament_index, spool_id, length_used, created_at, status, deleted
            FROM filament_usage_events""")
        cursor.execute("DROP TABLE filament_usage_events")
        cursor.execute("ALTER TABLE filament_usage_events_new RENAME TO filament_usage_events")

    # Keep the compatibility view in sync after every schema migration.  A
    # view created by an older image would otherwise hide the persisted 3MF
    # colour and history would regress to the stale spool colour.
    if has_spools_table:
        cursor.execute("DROP VIEW IF EXISTS v_spool_usage")
        cursor.execute('''
            CREATE VIEW v_spool_usage AS
            SELECT u.*, f.name AS filament_name, m.name AS material_name,
                   COALESCE(u.gcode_color, c.hex_code) AS color_hex,
                   u.gcode_color, u.gcode_filament_type
            FROM spool_usage u
            LEFT JOIN spools s ON s.id = u.spool_id AND s.deleted = 0
            LEFT JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
            LEFT JOIN materials m ON m.id = f.material_id AND m.deleted = 0
            LEFT JOIN colors c ON c.id = f.color_id AND c.deleted = 0
            WHERE u.deleted = 0
        ''')
    conn.commit()
    conn.close()


def recalculate_usage_grams_from_lengths(*, force: bool = False) -> dict[str, int]:
    """Normalize historical usage grams after material densities changed.

    Lengths are the authoritative measurement from the validated G-code.  Older
    rows were converted with the former neutral density, so their stored grams
    can disagree with the current material master.  This migration is guarded
    by runtime metadata and is therefore safe to call during every startup.
    """
    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    try:
        required = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"spool_usage", "spools", "filaments", "materials"}.issubset(required):
            return {"usage_rows": 0, "tracking_rows": 0}
        marker = "material_density_grams_v1"
        if not force:
            existing = conn.execute(
                "SELECT metadata_value FROM runtime_metadata WHERE namespace = 'history' AND metadata_key = ? AND deleted = 0",
                (marker,),
            ).fetchone()
            if existing:
                return {"usage_rows": 0, "tracking_rows": 0}

        rows = conn.execute(
            """SELECT u.id, u.print_id, u.length_used, u.estimated_length,
                      f.diameter, m.density
                 FROM spool_usage u
                 LEFT JOIN spools s ON s.id = u.spool_id
                 LEFT JOIN filaments f ON f.id = s.filament_id
                 LEFT JOIN materials m ON m.id = f.material_id
                WHERE u.deleted = 0 AND u.length_used IS NOT NULL
                  AND u.length_used >= 0"""
        ).fetchall()
        updated = 0
        for row in rows:
            try:
                diameter = float(row["diameter"] or 1.75)
                density = float(row["density"] or 1.24)
                length_mm = max(0.0, float(row["length_used"] or 0.0))
                estimated_length = row["estimated_length"]
                estimated_length_mm = max(0.0, float(estimated_length)) if estimated_length is not None else None
            except (TypeError, ValueError):
                continue
            if diameter <= 0 or density <= 0:
                continue
            grams_per_mm = math.pi * ((diameter / 2.0) / 10.0) ** 2 * 0.1 * density
            grams = length_mm * grams_per_mm
            # A zero/empty estimated length is not a usable measurement.  Keep
            # the legacy expected grams in that case instead of accidentally
            # erasing a valid estimate during the one-time repair.
            estimated_grams = (
                estimated_length_mm * grams_per_mm
                if estimated_length_mm is not None and estimated_length_mm > 0
                else None
            )
            conn.execute(
                "UPDATE spool_usage SET grams_used = ?, estimated_grams = COALESCE(?, estimated_grams) WHERE id = ?",
                (grams, estimated_grams, row["id"]),
            )
            updated += 1

        tracking_rows = conn.execute(
            """SELECT t.print_id
                 FROM print_layer_tracking t
                WHERE t.deleted = 0"""
        ).fetchall()
        tracking_updated = 0
        for tracking in tracking_rows:
            print_id = int(tracking["print_id"])
            totals = conn.execute(
                """SELECT COALESCE(SUM(grams_used), 0),
                          COALESCE(SUM(estimated_grams), 0)
                     FROM spool_usage
                    WHERE print_id = ? AND deleted = 0""",
                (print_id,),
            ).fetchone()
            conn.execute(
                """UPDATE print_layer_tracking
                      SET filament_grams_billed = ?, filament_grams_total = ?
                    WHERE print_id = ?""",
                (float(totals[0] or 0), float(totals[1] or 0), print_id),
            )
            tracking_updated += 1

        conn.execute(
            """INSERT INTO runtime_metadata(namespace, metadata_key, metadata_value, updated_at, deleted)
               VALUES ('history', ?, 'completed', CURRENT_TIMESTAMP, 0)
               ON CONFLICT(namespace, metadata_key) DO UPDATE SET
                 metadata_value = excluded.metadata_value,
                 updated_at = excluded.updated_at,
                 deleted = 0""",
            (marker,),
        )
        conn.commit()
        return {"usage_rows": updated, "tracking_rows": tracking_updated}
    finally:
        conn.close()


def insert_print(file_name: str, print_type: str, image_file: str = None, print_date: str = None) -> int:
    #
    # Inserts a new print job into the database and returns the print ID.
    # If no print_date is provided, the current timestamp is used.
    if print_date is None:
        print_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = connect_database(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO prints (print_date, file_name, print_type, image_file, file_name_source)
        VALUES (?, ?, ?, ?, 'provisional')
    ''', (print_date, file_name, print_type, image_file))
    print_id = cursor.lastrowid
    if print_id is None:
        conn.rollback()
        conn.close()
        raise RuntimeError("Print-History konnte keine Print-ID erzeugen")
    conn.commit()
    conn.close()
    return print_id


def ensure_layer_tracking(print_id: int, status: str = "PREPARING") -> None:
    # Create the lightweight tracking row before 3MF metadata is ready.
    if print_id is None:
        return
    conn = connect_database(db_config["db_path"])
    existing = conn.execute(
        "SELECT 1 FROM print_layer_tracking WHERE print_id = ?", (print_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE print_layer_tracking SET status = ? WHERE print_id = ?",
            (status, print_id),
        )
    else:
        conn.execute(
            "INSERT INTO print_layer_tracking (print_id, status) VALUES (?, ?)",
            (print_id, status),
        )
    conn.commit()
    conn.close()


def abort_active_prints_for_new_printer_job(status_at: str) -> int:
    # The printer can run only one physical job. Starting another one means
    # prior active history attempts did not end successfully.
    conn = connect_database(db_config["db_path"])
    cursor = conn.execute(
        """UPDATE print_layer_tracking
           SET status = 'ABORTED', actual_end_time = ?, last_status_at = ?
           WHERE status IN ('PREPARING', 'RUNNING', 'PAUSED')""",
        (status_at, status_at),
    )
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    return changed


def update_print_image(print_id: int, image_file: str) -> None:
    if print_id is None or not image_file:
        return
    conn = connect_database(db_config["db_path"])
    conn.execute("UPDATE prints SET image_file = ? WHERE id = ?", (image_file, print_id))
    conn.commit()
    conn.close()


def update_print_file_name(print_id: int, file_name: str) -> None:
    # Replace the provisional MQTT name after the 3MF object name is known.
    if print_id is None or not file_name:
        return
    conn = connect_database(db_config["db_path"])
    conn.execute(
        "UPDATE prints SET file_name = ?, file_name_source = 'model' "
        "WHERE id = ? AND file_name_source NOT IN ('metadata', 'manual')",
        (str(file_name).strip(), print_id),
    )
    conn.commit()
    conn.close()

def get_print_image(print_id: int) -> str | None:
    if print_id is None:
        return None
    conn = connect_database(db_config["db_path"])
    row = conn.execute("SELECT image_file FROM prints WHERE id = ?", (print_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def get_latest_running_print_id() -> int | None:
    conn = connect_database(db_config["db_path"])
    row = conn.execute(
        "SELECT print_id FROM print_layer_tracking WHERE status = 'RUNNING' ORDER BY print_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return int(row[0]) if row else None


def get_latest_active_print_id() -> int | None:
    # PREPARING and PAUSED are active jobs too.  History must keep refreshing
    # before the 3MF is available and while the printer is paused.
    conn = connect_database(db_config["db_path"])
    row = conn.execute(
        "SELECT print_id FROM print_layer_tracking "
        "WHERE status IN ('PREPARING', 'RUNNING', 'PAUSED') "
        "ORDER BY print_id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return int(row[0]) if row else None


def find_latest_print_id(file_name: str | None) -> int | None:
    # Find the newest history entry for a printer-reported file/subtask name.
    if not file_name:
        return None

    name = str(file_name).strip()
    candidates = [name]
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(suffix):
            candidates.append(name[: -len(suffix)])
    candidates = list(dict.fromkeys(candidates))

    conn = connect_database(db_config["db_path"])
    placeholders = ",".join("?" for _ in candidates)
    row = conn.execute(
        f"SELECT id FROM prints WHERE file_name IN ({placeholders}) "
        "AND COALESCE(is_deleted, 0) = 0 ORDER BY id DESC LIMIT 1",
        candidates,
    ).fetchone()
    conn.close()
    return int(row[0]) if row else None


def _normalise_print_name(value: str | None) -> str:
    name = str(value or "").strip().lower()
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def find_open_print_for_printer_source(*names: str | None) -> dict | None:
    # Find the newest matching history row that may still need terminal-status
    # reconciliation. A failed row can be reclassified when later MQTT evidence
    # confirms that the user stopped the print manually.
    wanted = {_normalise_print_name(name) for name in names if name}
    wanted.discard("")
    if not wanted:
        return None

    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id, p.file_name, t.status, t.total_layers,
                         t.layers_printed, t.predicted_end_time,
                         t.estimated_duration_minutes, t.reconciliation_done,
                         t.remote_3mf_path
             FROM prints p
             JOIN print_layer_tracking t ON t.print_id = p.id
               WHERE COALESCE(p.is_deleted, 0) = 0 AND COALESCE(p.deleted, 0) = 0 AND t.deleted = 0
                 AND t.status IN ('PREPARING', 'RUNNING', 'PAUSED', 'ABORTED', 'FAILED')
               AND COALESCE(t.reconciliation_done, 0) = 0
             ORDER BY p.id DESC"""
    ).fetchall()
    conn.close()
    for row in rows:
        candidates = {
            _normalise_print_name(row["file_name"]),
            _normalise_print_name(row["remote_3mf_path"]),
        }
        if wanted.intersection(candidates):
            return dict(row)
    return None


def find_active_print_for_printer_source(*names: str | None) -> dict | None:
    # Only an active source-matching attempt can be resumed; terminal history
    # rows must never become the current job.
    def normalise(value):
        return _normalise_print_name(str(value or "").replace("\\", "/").rsplit("/", 1)[-1])

    wanted = {normalise(name) for name in names if name}
    with connect_database(db_config["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT p.id, p.file_name, t.status, t.remote_3mf_path
               FROM prints p JOIN print_layer_tracking t ON t.print_id = p.id
               WHERE COALESCE(p.is_deleted, 0) = 0 AND COALESCE(p.deleted, 0) = 0 AND t.deleted = 0
                 AND t.status IN ('PREPARING', 'RUNNING', 'PAUSED')
               ORDER BY p.id DESC"""
        ).fetchall()
    for row in rows:
        if wanted.intersection({normalise(row["file_name"]), normalise(row["remote_3mf_path"])}):
            return dict(row)
    return None


def get_completed_remote_3mf_cleanup_path(print_id: int) -> str | None:
    """Return a printer-side 3MF path only when it is safe to remove.

    Bambu reuses names such as ``/BoxenInhalt.gcode.3mf`` for consecutive
    jobs.  A completed older print must therefore never delete a path that a
    newer active print is currently using.
    """
    with connect_database(db_config["db_path"]) as conn:
        row = conn.execute(
            """SELECT remote_3mf_path FROM print_layer_tracking
               WHERE print_id = ? AND status = 'COMPLETED'
                 AND COALESCE(deleted, 0) = 0
                 AND remote_3mf_path IS NOT NULL AND remote_3mf_path <> ''""",
            (int(print_id),),
        ).fetchone()
        if not row:
            return None
        remote_path = str(row[0]).strip()
        active = conn.execute(
            """SELECT 1 FROM print_layer_tracking
               WHERE print_id <> ?
                 AND status IN ('PREPARING', 'RUNNING', 'PAUSED')
                 AND COALESCE(deleted, 0) = 0
                 AND remote_3mf_path = ?
               LIMIT 1""",
            (int(print_id), remote_path),
        ).fetchone()
        return None if active else remote_path


def find_recent_aborted_print(file_name: str, cooldown_seconds: int = 120):
    """Return a recently aborted attempt for the same printer file.

    Bambu sends the same source in several forms (plain name, ``.3mf`` and
    ``.gcode.3mf`` paths).  Compare the canonical print name instead of the
    raw database value so a delayed packet cannot create a second attempt
    after a restart of MOS.
    """
    if not file_name:
        return None
    import datetime as _datetime
    now = _datetime.datetime.now()
    cutoff = now - _datetime.timedelta(seconds=int(cooldown_seconds))
    with connect_database(db_config["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT p.id, p.file_name, t.remote_3mf_path, t.actual_end_time
               FROM prints p JOIN print_layer_tracking t ON t.print_id = p.id
               WHERE COALESCE(p.is_deleted, 0) = 0 AND COALESCE(p.deleted, 0) = 0 AND t.deleted = 0
                 AND t.status = 'ABORTED'
               ORDER BY p.id DESC""",
        ).fetchall()
    wanted = _normalise_print_name(str(file_name).replace("\\", "/").rsplit("/", 1)[-1])
    for row in rows:
        names = {
            _normalise_print_name(str(row["file_name"] or "")),
            _normalise_print_name(str(row["remote_3mf_path"] or "")),
        }
        if wanted not in names:
            continue
        try:
            ended = _datetime.datetime.strptime(str(row["actual_end_time"]), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if cutoff <= ended <= now:
            return {"print_id": int(row["id"]), "actual_end_time": row["actual_end_time"]}
    return None


def find_previous_metadata_print_name(remote_3mf_path: str, current_print_id: int) -> str | None:
    # Reuse a user-facing name already extracted from metadata for this exact
    # printer file when a later attempt contains only its generated filename.
    if not remote_3mf_path:
        return None
    with connect_database(db_config["db_path"]) as conn:
        row = conn.execute(
            """SELECT p.file_name
               FROM prints p
               JOIN print_layer_tracking t ON t.print_id = p.id
               WHERE p.id != ?
                 AND COALESCE(p.is_deleted, 0) = 0 AND COALESCE(p.deleted, 0) = 0 AND t.deleted = 0
                 AND t.remote_3mf_path = ?
                 AND p.file_name_source IN ('metadata', 'manual')
                 AND TRIM(COALESCE(p.file_name, '')) != ''
               ORDER BY CASE p.file_name_source WHEN 'manual' THEN 0 ELSE 1 END,
                        p.id DESC
               LIMIT 1""",
            (int(current_print_id), str(remote_3mf_path)),
        ).fetchone()
    return str(row[0]) if row else None


def get_filament_usage_for_reconciliation(print_id: int) -> list[dict]:
    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, ams_slot, spool_id, physical_ams_slot,
                         gcode_filament_type, gcode_color,
                         grams_used, estimated_grams,
                         length_used, estimated_length, calculated_length,
                         consumed_length
             FROM spool_usage
             WHERE print_id = ? AND deleted = 0
             ORDER BY ams_slot""",
        (int(print_id),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def repair_filament_usage_spool_bindings(print_id: int, bindings: dict[tuple[str, str], tuple[int, int]]) -> int:
    """Align active-print usage rows with the local AMS signature mapping.

    A provisional row may have been created while MQTT still reported the
    previous tray.  Once the validated 3MF and local AMS assignments are
    available, the G-code material/colour signature is the safe key to move
    that row to the correct spool.  Only unique signatures are passed by the
    tracker, so repeated colours are never guessed.
    """
    if not print_id or not bindings:
        return 0
    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, gcode_filament_type, gcode_color
           FROM spool_usage
           WHERE print_id = ? AND deleted = 0""",
        (int(print_id),),
    ).fetchall()
    changed = 0
    for row in rows:
        key = (
            str(row["gcode_filament_type"] or "").strip().upper(),
            str(row["gcode_color"] or "").strip().replace("#", "").upper()[0:6],
        )
        target = bindings.get(key)
        if target is None:
            continue
        spool_id, physical_slot = target
        cursor = conn.execute(
            """UPDATE spool_usage
               SET spool_id = ?, physical_ams_slot = ?
               WHERE id = ?
                 AND (COALESCE(spool_id, -1) != ? OR COALESCE(physical_ams_slot, -1) != ?)""",
            (int(spool_id), int(physical_slot), int(row["id"]), int(spool_id), int(physical_slot)),
        )
        changed += cursor.rowcount
    if changed:
        conn.commit()
    conn.close()
    return changed


def set_estimated_duration_from_remaining(print_id: int, remaining_minutes: float) -> None:
    if print_id is None or remaining_minutes <= 0:
        return
    for attempt in range(3):
        conn = None
        try:
            conn = connect_database(db_config["db_path"])
            row = conn.execute(
                "SELECT print_date FROM prints WHERE id = ?",
                (int(print_id),),
            ).fetchone()
            elapsed_minutes = 0.0
            if row and row[0]:
                try:
                    started_at = datetime.fromisoformat(str(row[0]))
                    elapsed_minutes = max((datetime.now() - started_at).total_seconds() / 60.0, 0.0)
                except ValueError:
                    elapsed_minutes = 0.0
            estimated_total = max(float(remaining_minutes) + elapsed_minutes, float(remaining_minutes))
            conn.execute(
                "UPDATE print_layer_tracking SET estimated_duration_minutes = ? WHERE print_id = ?",
                (estimated_total, int(print_id)),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            transient = any(token in str(exc).lower() for token in ("disk i/o", "database is locked", "busy"))
            if not transient or attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))
        finally:
            if conn is not None:
                conn.close()


def update_printer_job_status(
    print_id: int,
    *,
    percent: float | None = None,
    layers_printed: int | None = None,
    status_at: str | None = None,
    predicted_end_time: str | None = None,
) -> None:
    if print_id is None:
        return
    fields = []
    values = []
    if percent is not None:
        fields.append("printer_percent = ?")
        values.append(max(0.0, min(100.0, float(percent))))
    if layers_printed is not None:
        fields.append("layers_printed = MAX(COALESCE(layers_printed, 0), ?)")
        values.append(max(0, int(layers_printed)))
    if status_at:
        fields.append("last_status_at = ?")
        values.append(status_at)
    if predicted_end_time:
        fields.append("predicted_end_time = ?")
        values.append(predicted_end_time)
    if not fields:
        return
    values.append(int(print_id))
    conn = connect_database(db_config["db_path"])
    conn.execute(f"UPDATE print_layer_tracking SET {', '.join(fields)} WHERE print_id = ?", values)
    conn.commit()
    conn.close()


def set_printer_percent_from_layers(print_id: int) -> float | None:
    # A terminal Bambu report may reset mc_percent to zero.  Retain the
    # measurable progress from the last layer checkpoint in that case.
    if print_id is None:
        return None
    conn = connect_database(db_config["db_path"])
    row = conn.execute(
        "SELECT layers_printed, total_layers FROM print_layer_tracking WHERE print_id = ?",
        (int(print_id),),
    ).fetchone()
    if not row or row[0] is None or row[1] is None or int(row[1]) <= 0:
        conn.close()
        return None
    percent = round(max(0.0, min(100.0, float(row[0]) * 100.0 / float(row[1]))), 2)
    conn.execute(
        "UPDATE print_layer_tracking SET printer_percent = ? WHERE print_id = ?",
        (percent, int(print_id)),
    )
    conn.commit()
    conn.close()
    return percent


def update_latest_printer_job_status(names: tuple[str | None, ...], *, percent: float | None, status_at: str) -> None:
    wanted = {_normalise_print_name(name) for name in names if name}
    wanted.discard("")
    if not wanted:
        return
    conn = connect_database(db_config["db_path"])
    rows = conn.execute(
        """SELECT p.id, p.file_name FROM prints p
           WHERE COALESCE(p.is_deleted, 0) = 0 AND COALESCE(p.deleted, 0) = 0 ORDER BY p.id DESC"""
    ).fetchall()
    for print_id, file_name in rows:
        if _normalise_print_name(file_name) in wanted:
            fields = ["last_status_at = ?"]
            values: list[object] = [status_at]
            if percent is not None:
                fields.append("printer_percent = ?")
                values.append(max(0.0, min(100.0, float(percent))))
            values.append(int(print_id))
            conn.execute(f"UPDATE print_layer_tracking SET {', '.join(fields)} WHERE print_id = ?", values)
            break
    conn.commit()
    conn.close()


def mark_print_reconciled(
    print_id: int,
    usage_updates: list[dict],
    completion_time: str,
    source: str = "mqtt_finish_recovery",
) -> None:
    # Persist recovered totals and close the job after successful external transfers.
    conn = connect_database(db_config["db_path"])
    conn.execute("PRAGMA foreign_keys = ON")
    for update in usage_updates:
        conn.execute(
            """UPDATE spool_usage
               SET grams_used = ?, length_used = ?, calculated_length = ?,
                   consumed_length = ?
               WHERE id = ?""",
            (
                update["grams_used"],
                update["length_used"],
                update["length_used"],
                update["length_used"],
                int(update["id"]),
            ),
        )
    conn.execute(
        """UPDATE print_layer_tracking
           SET status = 'COMPLETED',
               layers_printed = COALESCE(total_layers, layers_printed),
               filament_grams_billed = COALESCE(
                   (SELECT SUM(grams_used) FROM spool_usage WHERE print_id = ?),
                   filament_grams_billed
               ),
               estimated_duration_minutes = COALESCE(
                   estimated_duration_minutes,
                   (julianday(predicted_end_time) -
                    (SELECT julianday(print_date) FROM prints WHERE id = ?)) * 1440.0
               ),
               actual_end_time = ?, completion_source = ?,
               printer_completion_time = ?, printer_percent = 100,
               last_usage_event_at = COALESCE(
                   last_usage_event_at,
                   (SELECT MAX(created_at) FROM filament_usage_events WHERE print_id = ?)
               ),
               reconciliation_done = 1
           WHERE print_id = ?""",
        (int(print_id), int(print_id), completion_time, source, completion_time, int(print_id), int(print_id)),
    )
    conn.execute(
        "UPDATE filament_usage_events SET deleted = 1 WHERE print_id = ? AND status = 'confirmed' AND deleted = 0",
        (int(print_id),),
    )
    conn.commit()
    conn.close()


def cancel_stale_running_prints(actual_end_time: str) -> int:
    # Mark interrupted legacy runs as canceled without touching filament usage.
    conn = connect_database(db_config["db_path"])
    cursor = conn.execute(
        "UPDATE print_layer_tracking SET status = 'ABORTED', actual_end_time = ? "
        "WHERE status = 'RUNNING' AND predicted_end_time IS NOT NULL AND predicted_end_time < ?",
        (actual_end_time, actual_end_time),
    )
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    return changed


def reconcile_multiple_active_prints(status_at: str) -> int:
    """Keep only the newest physical print attempt active.

    The printer can execute one job at a time.  A reconnect can nevertheless
    replay an old ``RUNNING`` row after a newer attempt already exists.  Close
    those stale rows as aborted so the history and consumption tracker never
    expose two active jobs simultaneously.
    """
    conn = connect_database(db_config["db_path"])
    active = conn.execute(
        """SELECT print_id FROM print_layer_tracking
           WHERE status IN ('PREPARING', 'RUNNING', 'PAUSED') AND deleted = 0
           ORDER BY print_id DESC"""
    ).fetchall()
    if len(active) <= 1:
        conn.close()
        return 0
    keep_id = int(active[0][0])
    stale_ids = [int(row[0]) for row in active[1:]]
    placeholders = ",".join("?" for _ in stale_ids)
    cursor = conn.execute(
        f"""UPDATE print_layer_tracking
            SET status = 'ABORTED', actual_end_time = COALESCE(actual_end_time, ?),
                last_status_at = ?
            WHERE print_id IN ({placeholders})
              AND status IN ('PREPARING', 'RUNNING', 'PAUSED')""",
        (status_at, status_at, *stale_ids),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount

def insert_filament_usage(
    print_id: int,
    filament_type: str,
    color: str,
    grams_used: float,
    ams_slot: int,
    estimated_grams: float | None = None,
    length_used: float | None = None,
    estimated_length: float | None = None,
    physical_ams_slot: int | None = None,
) -> None:
    #
    # Inserts a new filament usage entry for a specific print job.
    if print_id is None:
        raise ValueError("Filamentverbrauch benötigt eine gültige Print-ID")

    conn = connect_database(db_config["db_path"])
    cursor = conn.cursor()
    # The first row can be created provisionally from the currently active
    # tray before the 3MF is ready.  Enrich that row instead of inserting a
    # duplicate once the real metadata has been parsed.
    cursor.execute(
        """UPDATE spool_usage
           SET grams_used = MAX(COALESCE(grams_used, 0), ?),
               gcode_filament_type = COALESCE(NULLIF(?, ''), gcode_filament_type),
               gcode_color = COALESCE(NULLIF(?, ''), gcode_color),
               estimated_grams = COALESCE(?, estimated_grams),
               length_used = MAX(COALESCE(length_used, 0), ?),
               estimated_length = COALESCE(?, estimated_length),
               calculated_length = MAX(COALESCE(calculated_length, 0), ?),
               physical_ams_slot = COALESCE(?, physical_ams_slot)
           WHERE print_id = ? AND ams_slot = ?""",
        (
            grams_used,
            filament_type,
            color,
            estimated_grams,
            length_used,
            estimated_length,
            length_used,
            physical_ams_slot,
            print_id,
            ams_slot,
        ),
    )
    if cursor.rowcount == 0:
        cursor.execute('''
            INSERT INTO spool_usage (print_id, grams_used, ams_slot, estimated_grams, length_used, estimated_length, calculated_length, physical_ams_slot, gcode_filament_type, gcode_color)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (print_id, grams_used, ams_slot, estimated_grams, length_used, estimated_length, length_used, physical_ams_slot, filament_type, color))
    conn.commit()
    conn.close()

def normalize_usage_slots(print_id: int, filament_ids: list[int], mapping: list[int | None]) -> None:
    """Remove provisional rows whose logical slot is not present in the 3MF.

    A provisional row is created from the live tray while the 3MF downloads.
    Once the original Bambu IDs are known, rows must be keyed by those IDs;
    physical tray matching alone can otherwise merge A1 into the wrong ID.
    """
    ids = sorted({int(value) for value in filament_ids})
    if not ids:
        return
    conn = connect_database(db_config["db_path"])
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, ams_slot, physical_ams_slot, grams_used FROM spool_usage "
        f"WHERE print_id = ? AND ams_slot NOT IN ({placeholders}) ORDER BY id",
        (int(print_id), *ids),
    ).fetchall()
    for row in rows:
        target = None
        row_id, _row_slot, physical, row_grams = row
        if physical is not None:
            for filament_id in ids:
                try:
                    if int(mapping[int(filament_id)]) == int(physical):
                        target = filament_id
                        break
                except (IndexError, TypeError, ValueError):
                    continue
        if target is not None:
            existing = conn.execute(
                "SELECT id FROM spool_usage WHERE print_id = ? AND ams_slot = ? LIMIT 1",
                (int(print_id), int(target)),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE spool_usage SET grams_used = COALESCE(grams_used,0) + "
                    "COALESCE((SELECT grams_used FROM spool_usage WHERE id = ?),0) "
                    "WHERE id = ?",
                    (row_id, existing[0]),
                )
                conn.execute("UPDATE spool_usage SET deleted = 1 WHERE id = ?", (row_id,))
            else:
                conn.execute("UPDATE spool_usage SET ams_slot = ? WHERE id = ?", (int(target), row_id))
        elif float(row_grams or 0) == 0:
            conn.execute("UPDATE spool_usage SET deleted = 1 WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()

def update_filament_spool(print_id: int, filament_id: int, spool_id: int) -> None:
    #
    # Updates the spool_id for a given filament usage entry, ensuring it belongs to the specified print job.
    conn = connect_database(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE spool_usage
        SET spool_id = ?
        WHERE ams_slot = ? AND print_id = ?
    ''', (spool_id, filament_id, print_id))
    conn.commit()
    conn.close()

def bind_filament_usage_spool(print_id: int, filament_id: int, spool_id: int, physical_ams_slot: int | None) -> None:
    # Bind an untouched provisional history row without rewriting usage already
    # recorded for an earlier physical spool.
    conn = connect_database(db_config["db_path"])
    conn.execute(
        """UPDATE spool_usage
           SET spool_id = ?, physical_ams_slot = COALESCE(?, physical_ams_slot)
           WHERE print_id = ? AND ams_slot = ?
             AND COALESCE(grams_used, 0) = 0
             AND COALESCE(length_used, 0) = 0""",
        (int(spool_id), physical_ams_slot, int(print_id), int(filament_id)),
    )
    conn.commit()
    conn.close()

def record_filament_usage_segment(
    print_id: int,
    filament_id: int,
    spool_id: int,
    physical_ams_slot: int | None,
    filament_type: str,
    color: str,
    grams_used: float,
    length_used: float,
) -> None:
    # Keep one history row for each logical filament / spool / physical tray
    # segment so AMS auto-switches remain visible in the print and spool history.
    conn = connect_database(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id FROM spool_usage
           WHERE print_id = ? AND spool_id = ?
             AND physical_ams_slot IS ?
           ORDER BY id LIMIT 1""",
        (int(print_id), int(spool_id), physical_ams_slot),
    )
    row = cursor.fetchone()
    if row:
        cursor.execute(
            """UPDATE spool_usage
               SET grams_used = COALESCE(grams_used, 0) + ?,
                   length_used = COALESCE(length_used, 0) + ?,
                   calculated_length = COALESCE(calculated_length, 0) + ?,
                   consumed_length = COALESCE(consumed_length, 0) + ?
               WHERE id = ?""",
            (float(grams_used), float(length_used), float(length_used), float(length_used), row[0]),
        )
    else:
        cursor.execute(
            """INSERT INTO spool_usage
               (print_id, spool_id, grams_used, ams_slot,
                estimated_grams, length_used, estimated_length, physical_ams_slot,
                calculated_length, consumed_length)
               VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)""",
            (int(print_id), int(spool_id), float(grams_used), int(filament_id),
             float(length_used), physical_ams_slot,
             float(length_used), float(length_used)),
        )
    conn.commit()
    conn.close()

def update_filament_physical_slot(print_id: int, filament_id: int, physical_ams_slot: int) -> None:
    # Persist the physical AMS tray for a logical filament channel.
    conn = connect_database(db_config["db_path"])
    conn.execute(
        "UPDATE spool_usage SET physical_ams_slot = ? WHERE ams_slot = ? AND print_id = ?",
        (physical_ams_slot, filament_id, print_id),
    )
    conn.commit()
    conn.close()

def claim_filament_usage_event(print_id: int | None, layer: int, filament_index: int, spool_id: int, length_used: float) -> str | None:
    # Create/inspect one consumption event and return its transfer status.
    if print_id is None:
        return "pending"
    conn = connect_database(db_config["db_path"])
    # Claim and state transition must be one SQLite transaction. Without an
    # immediate lock, the replay worker and the live MQTT layer can both see
    # the same missing event and race into a duplicate insert/consumption.
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT id, status, deleted FROM filament_usage_events WHERE print_id = ? AND layer = ? AND filament_index = ?",
        (print_id, int(layer), int(filament_index)),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO filament_usage_events
               (print_id, layer, filament_index, spool_id, length_used, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (print_id, int(layer), int(filament_index), int(spool_id), float(length_used), datetime.now().isoformat()),
        )
        row = conn.execute(
            "SELECT id, status, deleted FROM filament_usage_events WHERE print_id = ? AND layer = ? AND filament_index = ?",
            (print_id, int(layer), int(filament_index)),
        ).fetchone()
    elif row[2]:
        # Soft-deleted events retain the unique logical key. Reuse that row
        # during an idempotent replay instead of attempting a duplicate insert.
        conn.execute(
            "UPDATE filament_usage_events SET spool_id = ?, length_used = ?, status = 'pending', deleted = 0 WHERE id = ?",
            (int(spool_id), float(length_used), int(row[0])),
        )
        row = (row[0], "pending", 0)
    conn.commit()
    conn.close()
    return row[1] if row else None


def finalize_filament_usage_events(print_id: int) -> None:
    # Usage segments are persisted when consumption succeeds. Remove
    # only confirmed events after finalization; pending events remain recoverable.
    conn = connect_database(db_config["db_path"])
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "UPDATE filament_usage_events SET deleted = 1 WHERE print_id = ? AND status = 'confirmed' AND deleted = 0",
        (int(print_id),),
    )
    conn.commit()
    conn.close()


def set_filament_usage_event_status(print_id: int, layer: int, filament_index: int, status: str) -> None:
    # Advance a consumption event through pending, sent and confirmed.
    if status not in {"pending", "sent", "confirmed"}:
        raise ValueError(f"Invalid filament usage event status: {status}")
    conn = connect_database(db_config["db_path"])
    conn.execute(
        "UPDATE filament_usage_events SET status = ? WHERE print_id = ? AND layer = ? AND filament_index = ?",
        (status, int(print_id), int(layer), int(filament_index)),
    )
    if status == "confirmed":
        conn.execute(
            """UPDATE print_layer_tracking
               SET last_usage_event_at = COALESCE(
                   (SELECT MAX(created_at) FROM filament_usage_events WHERE print_id = ?),
                   last_usage_event_at
               )
               WHERE print_id = ?""",
            (int(print_id), int(print_id)),
        )
    conn.commit()
    conn.close()

def update_filament_grams_used(print_id: int, filament_id: int, grams_used: float, length_used: float | None = None) -> None:
    #
    # Updates the grams_used (and optional length_used) for a given filament usage entry, ensuring it belongs to the specified print job.
    set_parts = ["grams_used = ?"]
    params: list[float | int] = [grams_used]
    if length_used is not None:
        set_parts.append("length_used = ?")
        params.append(length_used)
        set_parts.append("calculated_length = ?")
        params.append(length_used)

    set_clause = ", ".join(set_parts)
    params.extend([filament_id, print_id])

    conn = connect_database(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute(f'''
        UPDATE spool_usage
        SET {set_clause}
        WHERE ams_slot = ? AND print_id = ?
    ''', params)
    conn.commit()
    conn.close()


def _ensure_spool_usage_view(conn: sqlite3.Connection) -> None:
    # The inventory migration can recreate tables after print_history startup.
    # Recreate the derived view before any history query so that ordering of
    # module initialization cannot cause a missing-view error.
    view_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'v_spool_usage'"
    ).fetchone()
    if view_exists:
        return
    required = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('spool_usage', 'spools', 'filaments', 'materials', 'colors')"
    )}
    if 'spool_usage' not in required:
        return
    conn.executescript("""
        -- Several history requests can initialize the view concurrently.
        -- The inventory migration owns destructive recreation; this helper
        -- only fills a missing view and must therefore be idempotent.
        CREATE VIEW IF NOT EXISTS v_spool_usage AS
        SELECT u.*, f.name AS filament_name, m.name AS material_name,
               c.hex_code AS color_hex
        FROM spool_usage u
        LEFT JOIN spools s ON s.id = u.spool_id AND s.deleted = 0
        LEFT JOIN filaments f ON f.id = s.filament_id AND f.deleted = 0
        LEFT JOIN materials m ON m.id = f.material_id AND m.deleted = 0
        LEFT JOIN colors c ON c.id = f.color_id AND c.deleted = 0
        WHERE u.deleted = 0;
    """)
    conn.commit()


def get_prints_with_filament(limit: int | None = None, offset: int | None = None, include_deleted: bool = False):
    #
    # Retrieves print jobs along with their associated filament usage, grouped by print job.
    #
    # A total count is returned to support pagination.
    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row  # Enable column name access
    _ensure_spool_usage_view(conn)

    count_cursor = conn.cursor()
    where_clause = "" if include_deleted else " WHERE COALESCE(is_deleted, 0) = 0 AND COALESCE(deleted, 0) = 0"
    count_cursor.execute("SELECT COUNT(*) FROM prints" + where_clause)
    total_count = count_cursor.fetchone()[0]

    cursor = conn.cursor()
    query = '''
        SELECT p.id AS id, p.print_date AS print_date, p.file_name AS file_name,
               p.file_name_source AS file_name_source, p.print_type AS print_type,
               p.image_file AS image_file, p.is_deleted AS is_deleted,
       (
           SELECT json_group_array(json_object(
               'spool_id', f.spool_id,
                'filament_type', f.material_name,
                'color', f.color_hex,
                'gcode_color', f.gcode_color,
                'gcode_filament_type', f.gcode_filament_type,
                'grams_used', f.grams_used,
                'estimated_grams', f.estimated_grams,
                'length_used', f.length_used,
                'estimated_length', f.estimated_length,
                'calculated_length', f.calculated_length,
                'consumed_length', f.consumed_length,
                'ams_slot', f.ams_slot,
                'physical_ams_slot', f.physical_ams_slot
            )) FROM v_spool_usage f WHERE f.print_id = p.id
        ) AS filament_info
        FROM prints p
    ''' + where_clause + '''
        ORDER BY p.print_date DESC, p.id DESC
    '''
    params: list[int] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
        if offset is not None:
            query += " OFFSET ?"
            params.append(offset)

    cursor.execute(query, params)
    prints = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return prints, total_count


def get_print_page(print_id: int, page_size: int, include_deleted: bool = False) -> int | None:
    # Resolve the page containing one history entry using the same ordering as
    # get_prints_with_filament. This keeps deep links independent of page 1.
    try:
        target_id = int(print_id)
        size = int(page_size)
    except (TypeError, ValueError):
        return None
    if target_id <= 0 or size <= 0:
        return None

    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    visibility = "1 = 1" if include_deleted else "COALESCE(is_deleted, 0) = 0 AND COALESCE(deleted, 0) = 0"
    target = conn.execute(
        f"SELECT id, print_date FROM prints WHERE id = ? AND {visibility}",
        (target_id,),
    ).fetchone()
    if target is None:
        conn.close()
        return None

    # print_date is populated for every normal print. The fallback keeps a
    # malformed legacy row reachable instead of silently sending it to page 1.
    if target["print_date"] is None:
        before_count = conn.execute(
            f"SELECT COUNT(*) FROM prints WHERE {visibility} AND print_date IS NULL AND id > ?",
            (target_id,),
        ).fetchone()[0]
    else:
        before_count = conn.execute(
            f"""SELECT COUNT(*) FROM prints
                WHERE {visibility}
                  AND (print_date > ? OR (print_date = ? AND id > ?))""",
            (target["print_date"], target["print_date"], target_id),
        ).fetchone()[0]
    conn.close()
    return int(before_count) // size + 1


def get_history_filter_options(include_deleted: bool = False) -> dict[str, list[str]]:
    # Return the complete date and status domains for the visible history,
    # independent of the currently paginated page.
    conn = connect_database(db_config["db_path"])
    visibility = "1 = 1" if include_deleted else "COALESCE(p.is_deleted, 0) = 0"
    rows = conn.execute(
        f"""SELECT DISTINCT substr(p.print_date, 1, 10) AS print_day,
                          COALESCE(UPPER(t.status), 'COMPLETED') AS status
                   FROM prints p
                   LEFT JOIN print_layer_tracking t ON t.print_id = p.id
                   WHERE {visibility}"""
    ).fetchall()
    conn.close()
    dates = sorted({str(row[0]) for row in rows if row[0]}, reverse=True)
    statuses = sorted({str(row[1]).upper() for row in rows if row[1]})
    return {"dates": dates, "statuses": statuses}


def soft_delete_print(print_id: int) -> None:
    conn = connect_database(db_config["db_path"])
    conn.execute("UPDATE prints SET is_deleted = 1, deleted = 1 WHERE id = ?", (print_id,))
    conn.commit()
    conn.close()


def restore_print(print_id: int) -> None:
    conn = connect_database(db_config["db_path"])
    conn.execute("UPDATE prints SET is_deleted = 0, deleted = 0 WHERE id = ?", (print_id,))
    conn.commit()
    conn.close()


def has_deleted_prints() -> bool:
    conn = connect_database(db_config["db_path"])
    row = conn.execute("SELECT 1 FROM prints WHERE COALESCE(is_deleted, 0) = 1 OR COALESCE(deleted, 0) = 1 LIMIT 1").fetchone()
    conn.close()
    return row is not None

def get_prints_by_spool(spool_id: int):
    #
    # Retrieves all print jobs that used a specific spool.
    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT p.* FROM prints p
        JOIN spool_usage f ON p.id = f.print_id
        WHERE f.spool_id = ? AND f.deleted = 0 AND p.deleted = 0
    ''', (spool_id,))
    prints = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return prints


def get_spool_print_usage(spool_id: int) -> list[dict]:
    # Return the print-level consumption records for one spool.
    return get_spool_print_usage_for_spools([spool_id]).get(int(spool_id), [])


def get_spool_print_usage_for_spools(spool_ids: list[int] | tuple[int, ...]) -> dict[int, list[dict]]:
    """Load print-level consumption records for several spools in one query.

    The inventory page displays the usage list for every spool.  Keeping this
    batched avoids opening one SQLite connection and running one query per
    spool on every page request.
    """
    ids = sorted({int(spool_id) for spool_id in (spool_ids or []) if spool_id is not None})
    if not ids:
        return {}
    conn = connect_database(db_config["db_path"])
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT f.spool_id, p.id AS print_id, p.print_date, p.file_name,
                  f.grams_used, f.length_used, f.calculated_length,
                  f.consumed_length,
                  COALESCE(t.status, 'COMPLETED') AS status,
                  t.actual_end_time,
                  CASE
                    WHEN t.actual_end_time IS NOT NULL
                    THEN ROUND((julianday(t.actual_end_time) - julianday(p.print_date)) * 1440.0)
                  END AS duration_minutes
           FROM prints p
           JOIN spool_usage f ON f.print_id = p.id
           LEFT JOIN print_layer_tracking t ON t.print_id = p.id
           WHERE f.spool_id IN ({placeholders}) AND f.deleted = 0
             AND p.deleted = 0 AND COALESCE(t.deleted, 0) = 0
           ORDER BY f.spool_id, p.print_date DESC, p.id DESC""",
        tuple(ids),
    ).fetchall()
    conn.close()
    usage_by_spool = {spool_id: [] for spool_id in ids}
    for row in rows:
        item = dict(row)
        usage_by_spool.setdefault(int(item.pop("spool_id")), []).append(item)
    return usage_by_spool

def get_filament_for_slot(print_id: int, ams_slot: int):
  conn = connect_database(db_config["db_path"])
  conn.row_factory = sqlite3.Row  # Enable column name access
  cursor = conn.cursor()

  cursor.execute('''
      SELECT * FROM v_spool_usage
      WHERE print_id = ? AND ams_slot = ?
  ''', (print_id, ams_slot))

  row = cursor.fetchone()
  conn.close()
  return dict(row) if row else None

def _ensure_layer_tracking_entry(print_id: int):
  conn = connect_database(db_config["db_path"])
  cursor = conn.cursor()
  if cursor.execute(
      "SELECT 1 FROM print_layer_tracking WHERE print_id = ?", (print_id,)
  ).fetchone() is None:
    cursor.execute(
        "INSERT INTO print_layer_tracking (print_id) VALUES (?)", (print_id,)
    )
  conn.commit()
  conn.close()

def update_layer_tracking(print_id: int, **fields):
  if not fields:
    return

  allowed_columns = {
      "total_layers",
      "layers_printed",
      "filament_grams_billed",
      "filament_grams_total",
      "status",
      "predicted_end_time",
      "actual_end_time",
      "printer_percent",
      "last_status_at",
      "last_usage_event_at",
      "remote_3mf_path",
  }

  sanitized = {key: value for key, value in fields.items() if key in allowed_columns}
  if not sanitized:
    return

  _ensure_layer_tracking_entry(print_id)

  set_clause = ", ".join(f"{key} = ?" for key in sanitized)
  params = list(sanitized.values()) + [print_id]

  conn = connect_database(db_config["db_path"])
  cursor = conn.cursor()
  cursor.execute(f'''
      UPDATE print_layer_tracking
      SET {set_clause}
      WHERE print_id = ? AND deleted = 0
  ''', params)
  conn.commit()
  conn.close()

def get_layer_tracking_for_prints(print_ids: list[int]):
  if not print_ids:
    return {}

  conn = connect_database(db_config["db_path"])
  conn.row_factory = sqlite3.Row
  cursor = conn.cursor()
  placeholders = ",".join("?" for _ in print_ids)
  cursor.execute(f'''
      SELECT print_id, total_layers, layers_printed, filament_grams_billed, filament_grams_total, status, predicted_end_time, actual_end_time, estimated_duration_minutes, printer_percent, last_status_at, last_usage_event_at
      FROM print_layer_tracking
      WHERE print_id IN ({placeholders})
  ''', print_ids)
  rows = cursor.fetchall()
  conn.close()
  return {row["print_id"]: dict(row) for row in rows}


def get_latest_print_summary() -> dict | None:
  # Return the latest visible print and its persisted tracking status.
  conn = connect_database(db_config["db_path"])
  conn.row_factory = sqlite3.Row
  row = conn.execute(
    """SELECT p.id, p.print_date, p.file_name,
              t.status, t.layers_printed, t.total_layers,
              t.actual_end_time, t.predicted_end_time, t.estimated_duration_minutes,
              t.printer_percent, t.last_status_at, t.last_usage_event_at,
              t.filament_grams_billed, t.filament_grams_total
       FROM prints p
       LEFT JOIN print_layer_tracking t ON t.print_id = p.id
       WHERE COALESCE(p.is_deleted, 0) = 0 AND COALESCE(p.deleted, 0) = 0 AND COALESCE(t.deleted, 0) = 0
       ORDER BY p.print_date DESC, p.id DESC
       LIMIT 1"""
  ).fetchone()
  conn.close()
  return dict(row) if row else None

def get_all_filament_usage_for_print(print_id: int):
  #
  # Retrieves all filament usage entries for a specific print.
  # Returns a dict mapping ams_slot to a dict with grams_used and length_used.
  conn = connect_database(db_config["db_path"])
  conn.row_factory = sqlite3.Row
  cursor = conn.cursor()

  cursor.execute('''
      SELECT ams_slot, SUM(COALESCE(grams_used, 0)) AS grams_used,
             SUM(COALESCE(length_used, 0)) AS length_used
      FROM spool_usage
      WHERE print_id = ? AND deleted = 0
      GROUP BY ams_slot
  ''', (print_id,))

  results = {
      row["ams_slot"]: {
          "grams_used": row["grams_used"],
          "length_used": row["length_used"],
      }
      for row in cursor.fetchall()
  }
  conn.close()
  return results

# Example for creating the database if it does not exist
create_database()
