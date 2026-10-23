"""SQLite storage for NFC tags that have not yet been assigned to a spool."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database import connect_database


_LEGACY_JSON_FILE = Path(__file__).resolve().parent / "data" / "nfc_pending.json"


def _connect() -> sqlite3.Connection:
    connection = connect_database()
    connection.row_factory = sqlite3.Row
    return connection


def _normalize_uid(uid: Any) -> str:
    return str(uid or "").strip().upper().replace(":", "-")


def _ensure_schema() -> None:
    connection = _connect()
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS nfc_pending_tags (
                   uid TEXT PRIMARY KEY NOT NULL CHECK (length(trim(uid)) > 0),
                   ams_id INTEGER NOT NULL CHECK (ams_id >= 0),
                   tray_index INTEGER NOT NULL CHECK (tray_index >= 0),
                   seen_at TEXT NOT NULL,
                   deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
               )"""
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(nfc_pending_tags)")}
        if "deleted" not in columns:
            connection.execute("ALTER TABLE nfc_pending_tags ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_nfc_pending_tags_seen_at "
            "ON nfc_pending_tags (seen_at)"
        )
        connection.commit()
    finally:
        connection.close()


def _read_legacy_items(path: Path) -> list[dict[str, Any]]:
    raw_items = json.loads(path.read_text(encoding="utf-8"))
    if raw_items == {}:
        return []
    if not isinstance(raw_items, list):
        raise ValueError("Legacy NFC pending file must contain a JSON list")

    normalized_items = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"Legacy NFC pending item {index} is not an object")
        uid = _normalize_uid(item.get("uid"))
        if not uid:
            raise ValueError(f"Legacy NFC pending item {index} has no UID")
        try:
            ams_id = int(item["ams_id"])
            tray_index = int(item["tray_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Legacy NFC pending item {index} has invalid tray data") from exc
        if ams_id < 0 or tray_index < 0:
            raise ValueError(f"Legacy NFC pending item {index} has negative tray data")
        seen_at = str(item.get("seen_at") or datetime.now(timezone.utc).isoformat())
        normalized_items.append({
            "uid": uid,
            "ams_id": ams_id,
            "tray_index": tray_index,
            "seen_at": seen_at,
        })
    return normalized_items


def migrate_legacy_json(path: Path = _LEGACY_JSON_FILE) -> int:
    """Copy legacy entries into SQLite, then remove the file after verification."""
    if not path.exists():
        return 0

    items = _read_legacy_items(path)
    connection = _connect()
    try:
        with connection:
            for item in items:
                connection.execute(
                    """INSERT INTO nfc_pending_tags (uid, ams_id, tray_index, seen_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(uid) DO UPDATE SET
                           ams_id = excluded.ams_id,
                           tray_index = excluded.tray_index,
                           seen_at = excluded.seen_at,
                           deleted = 0""",
                    (item["uid"], item["ams_id"], item["tray_index"], item["seen_at"]),
                )
            for item in items:
                row = connection.execute(
                    "SELECT ams_id, tray_index, seen_at FROM nfc_pending_tags WHERE uid = ? AND deleted = 0",
                    (item["uid"],),
                ).fetchone()
                if row is None or tuple(row) != (
                    item["ams_id"], item["tray_index"], item["seen_at"]
                ):
                    raise RuntimeError(f"Could not verify migrated NFC tag {item['uid']}")
    finally:
        connection.close()

    path.unlink()
    return len(items)


def list_pending_tags() -> list[dict[str, Any]]:
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT uid, ams_id, tray_index, seen_at "
            "FROM nfc_pending_tags WHERE deleted = 0 ORDER BY seen_at, uid"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_pending_tag(uid: str) -> dict[str, Any] | None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT uid, ams_id, tray_index, seen_at "
            "FROM nfc_pending_tags WHERE uid = ? AND deleted = 0",
            (_normalize_uid(uid),),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def remember_pending_tag(uid: str, tray_index: int, ams_id: int) -> None:
    normalized_uid = _normalize_uid(uid)
    if not normalized_uid:
        raise ValueError("NFC tag UID must not be empty")
    seen_at = datetime.now(timezone.utc).isoformat()
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """INSERT INTO nfc_pending_tags (uid, ams_id, tray_index, seen_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(uid) DO UPDATE SET
                       ams_id = excluded.ams_id,
                       tray_index = excluded.tray_index,
                       seen_at = excluded.seen_at,
                       deleted = 0""",
                (normalized_uid, int(ams_id), int(tray_index), seen_at),
            )
    finally:
        connection.close()


def remove_pending_tag(uid: str) -> bool:
    connection = _connect()
    try:
        with connection:
            cursor = connection.execute(
                "UPDATE nfc_pending_tags SET deleted = 1 WHERE uid = ? AND deleted = 0",
                (_normalize_uid(uid),),
            )
        return cursor.rowcount > 0
    finally:
        connection.close()


def has_pending_tags() -> bool:
    connection = _connect()
    try:
        return connection.execute(
            "SELECT 1 FROM nfc_pending_tags WHERE deleted = 0 LIMIT 1"
        ).fetchone() is not None
    finally:
        connection.close()


def initialize_storage() -> int:
    _ensure_schema()
    return migrate_legacy_json()
