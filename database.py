"""Central connection and file migration for the OpenSpoolMan database."""

from __future__ import annotations

import os
import sqlite3
import json
from pathlib import Path

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
            f"Unsupported OpenSpoolMan database type: {DATABASE_TYPE!r}; only 'sqlite' is available"
        )

    target_path = Path(path).expanduser() if path is not None else DATABASE_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_database_file(target_path)
    connection = sqlite3.connect(str(target_path))
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_metadata (
            namespace TEXT NOT NULL,
            metadata_key TEXT NOT NULL,
            metadata_value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (namespace, metadata_key)
        )
        """
    )
    connection.commit()
    return connection


def get_runtime_metadata(
    namespace: str,
    metadata_key: str,
    path: str | Path | None = None,
) -> dict | list | str | int | float | bool | None:
    # Read a JSON-compatible value from persistent runtime metadata.
    with connect_database(path) as connection:
        row = connection.execute(
            "SELECT metadata_value FROM runtime_metadata "
            "WHERE namespace = ? AND metadata_key = ?",
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
            "metadata_value = excluded.metadata_value, updated_at = CURRENT_TIMESTAMP",
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
            "DELETE FROM runtime_metadata WHERE namespace = ? AND metadata_key = ?",
            (str(namespace), str(metadata_key)),
        )
        connection.commit()
