"""Central connection and file migration for the m0BamboSpool database."""

from __future__ import annotations

import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime

from config import DATABASE_NAME, DATABASE_PATH, DATABASE_PATH_OVERRIDE, DATABASE_TYPE


LEGACY_DATABASE_NAME = "3d_printer_logs.db"


def _migrate_legacy_database_file(target_path: Path) -> None:
    default_target = Path(__file__).resolve().parent / "data" / DATABASE_NAME
    if (
        DATABASE_PATH_OVERRIDE
        or DATABASE_NAME != "osm.db"
        or target_path.resolve() != default_target.resolve()
        or target_path.exists()
    ):
        return

    legacy_path = target_path.with_name(LEGACY_DATABASE_NAME)
    if not legacy_path.exists():
        return

    # A previous process must be stopped before this runs. Move SQLite sidecars
    # first so a database left in WAL/journal mode remains consistent.
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            source = Path(f"{legacy_path}{suffix}")
            destination = Path(f"{target_path}{suffix}")
            if source.exists():
                if destination.exists():
                    raise FileExistsError(f"Database migration target already exists: {destination}")
                os.replace(source, destination)
                moved.append((source, destination))
        os.replace(legacy_path, target_path)
        moved.append((legacy_path, target_path))
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                os.replace(destination, source)
        raise


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
    if DATABASE_TYPE != "sqlite":
        raise RuntimeError(
            f"Unsupported m0BamboSpool database type: {DATABASE_TYPE!r}; only 'sqlite' is available"
        )

    target_path = Path(path).expanduser() if path is not None else DATABASE_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_database_file(target_path)
    connection = sqlite3.connect(str(target_path))
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_metadata (
            namespace TEXT NOT NULL,
            metadata_key TEXT NOT NULL,
            metadata_value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
            PRIMARY KEY (namespace, metadata_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS humidity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT NOT NULL,
            ams_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            humidity_raw REAL,
            humidity_level INTEGER,
            temperature REAL,
            deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
        )
        """
    )
    for table in ("runtime_metadata", "humidity_history"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "deleted" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_humidity_history_lookup
        ON humidity_history (printer_id, ams_id, observed_at)
        """
    )
    # Keep one migration marker so the history reset is performed exactly once.
    reset_marker = connection.execute(
        "SELECT metadata_value FROM runtime_metadata "
        "WHERE namespace = ? AND metadata_key = ?",
        ("migration", "humidity_history_quarter_hour_v1"),
    ).fetchone()
    if reset_marker is None:
        connection.execute("UPDATE humidity_history SET deleted = 1")
        connection.execute(
            "INSERT INTO runtime_metadata (namespace, metadata_key, metadata_value) "
            "VALUES (?, ?, ?)",
            ("migration", "humidity_history_quarter_hour_v1", "reset"),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_humidity_history_quarter_hour "
        "ON humidity_history (printer_id, ams_id, observed_at)"
    )
    connection.commit()
    return connection


def record_humidity_snapshots(
    printer_id: str,
    metrics: list[dict] | tuple[dict, ...] | None,
    observed_at: str | None = None,
) -> int:
    # Store one latest-value row per printer, AMS and full quarter-hour.
    if not printer_id or not metrics:
        return 0
    timestamp_dt = datetime.fromisoformat(observed_at) if observed_at else datetime.now().astimezone()
    if timestamp_dt.tzinfo is None:
        timestamp_dt = timestamp_dt.astimezone()
    timestamp_dt = timestamp_dt.replace(
        minute=(timestamp_dt.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    timestamp = timestamp_dt.isoformat(timespec="seconds")
    inserted = 0
    with connect_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index, metric in enumerate(metrics):
            if not isinstance(metric, dict):
                continue
            ams_id = str(metric.get("id", index)).strip()
            if not ams_id:
                ams_id = str(index)
            raw_value = metric.get("humidity_raw")
            try:
                humidity_raw = float(str(raw_value).rstrip("%")) if raw_value not in (None, "") else None
            except (TypeError, ValueError):
                humidity_raw = None
            try:
                level_value = metric.get("humidity_level", metric.get("humidity"))
                humidity_level = int(level_value) if level_value not in (None, "") else None
            except (TypeError, ValueError):
                humidity_level = None
            try:
                temperature_value = metric.get("temperature", metric.get("temp"))
                temperature = float(temperature_value) if temperature_value not in (None, "") else None
            except (TypeError, ValueError):
                temperature = None
            if humidity_raw is None and humidity_level is None:
                continue
            existing = connection.execute(
                "SELECT id FROM humidity_history "
                "WHERE printer_id = ? AND ams_id = ? AND observed_at = ?",
                (str(printer_id), ams_id, timestamp),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE humidity_history SET humidity_raw = ?, humidity_level = ?, temperature = ?, deleted = 0 "
                    "WHERE id = ?",
                    (humidity_raw, humidity_level, temperature, existing[0]),
                )
            else:
                connection.execute(
                    "INSERT INTO humidity_history "
                    "(printer_id, ams_id, observed_at, humidity_raw, humidity_level, temperature) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(printer_id), ams_id, timestamp, humidity_raw, humidity_level, temperature),
                )
                inserted += 1
        connection.commit()
    return inserted


def list_humidity_history(
    printer_id: str,
    ams_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    # Return humidity history in chronological order for the chart.
    try:
        safe_limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        safe_limit = 500
    with connect_database() as connection:
        if ams_id not in (None, ""):
            rows = connection.execute(
                "SELECT id, printer_id, ams_id, observed_at, humidity_raw, humidity_level, temperature "
                "FROM humidity_history WHERE printer_id = ? AND ams_id = ? AND deleted = 0 "
                "ORDER BY observed_at DESC, id DESC LIMIT ?",
                (str(printer_id), str(ams_id), safe_limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, printer_id, ams_id, observed_at, humidity_raw, humidity_level, temperature "
                "FROM humidity_history WHERE printer_id = ? AND deleted = 0 "
                "ORDER BY observed_at DESC, id DESC LIMIT ?",
                (str(printer_id), safe_limit),
            ).fetchall()
    columns = ("id", "printer_id", "ams_id", "observed_at", "humidity_raw", "humidity_level", "temperature")
    return [dict(zip(columns, row)) for row in reversed(rows)]


def get_runtime_metadata(
    namespace: str,
    metadata_key: str,
    path: str | Path | None = None,
) -> dict | list | str | int | float | bool | None:
    # Read a JSON-compatible value from persistent runtime metadata.
    with connect_database(path) as connection:
        row = connection.execute(
            "SELECT metadata_value FROM runtime_metadata "
            "WHERE namespace = ? AND metadata_key = ? AND deleted = 0",
            (str(namespace), str(metadata_key)),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return row[0]


def set_runtime_metadata(
    namespace: str,
    metadata_key: str,
    value,
    path: str | Path | None = None,
) -> None:
    # Persist a JSON-compatible runtime value for reuse after restart.
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO runtime_metadata(namespace, metadata_key, metadata_value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(namespace, metadata_key) DO UPDATE SET "
            "metadata_value = excluded.metadata_value, updated_at = CURRENT_TIMESTAMP, deleted = 0",
            (str(namespace), str(metadata_key), encoded),
        )
        connection.commit()


def delete_runtime_metadata(
    namespace: str,
    metadata_key: str,
    path: str | Path | None = None,
) -> None:
    # Delete one runtime metadata entry when it is no longer valid.
    with connect_database(path) as connection:
        connection.execute(
            "UPDATE runtime_metadata SET deleted = 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE namespace = ? AND metadata_key = ?",
            (str(namespace), str(metadata_key)),
        )
        connection.commit()
