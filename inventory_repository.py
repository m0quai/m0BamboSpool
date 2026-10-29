# m0BamboSpool's narrow data-access boundary for spool inventory.
#
# Application code uses this module for all inventory access.  The local SQLite
# catalog is the single authoritative source for spools, filaments and usage.

from typing import Any
import json

import inventory_database as _local


def get_backend() -> str:
    return "local"


def ensure_local_catalog() -> None:
    # Create the local catalog tables.  The local catalog is authoritative and
    # must never silently import or refresh data from another service.
    _local.initialize()


def list_spools(*, include_archived: bool = False) -> list[dict[str, Any]]:
    return _local.list_spools(include_archived=include_archived)


def get_spool(spool_id: int | str) -> dict[str, Any]:
    return _local.get_spool(spool_id)


def record_consumption(
    spool_id: int | str,
    *,
    weight_grams: float | None = None,
    length_mm: float | None = None,
    occurred_at: str | None = None,
) -> bool:
    _local.record_consumption(spool_id, weight_grams, length_mm, occurred_at)
    return True


def record_usage_marker(spool_id: int | str, *, occurred_at: str | None = None) -> bool:
    _local.record_usage_marker(spool_id, occurred_at)
    return True


def update_spool_extra(
    spool_id: int | str,
    old_extra: dict[str, Any] | None,
    *,
    allow_active_print: bool = False,
    **values: Any,
) -> None:
    _local.set_spool_extra(spool_id, values, allow_active_print=allow_active_print)


def update_filament_metadata(
    filament_id: int | str,
    old_extra: dict[str, Any] | None,
    values: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(values)
    if "nozzle_temperature" in normalized:
        value = normalized.pop("nozzle_temperature")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                pass
        if isinstance(value, (list, tuple)):
            normalized["nozzle_temp_min"] = value[0] if value else None
            normalized["nozzle_temp_max"] = value[1] if len(value) > 1 else (value[0] if value else None)
        else:
            normalized["nozzle_temp_min"] = value
            normalized["nozzle_temp_max"] = value
    if "filament_id" in normalized:
        normalized["bambu_filament_id"] = normalized.pop("filament_id")
    if "setting_id" in normalized:
        normalized["bambu_setting_id"] = normalized.pop("setting_id")
    for key in ("nozzle_temp_min", "nozzle_temp_max", "cali_idx", "bambu_filament_id", "bambu_setting_id"):
        value = normalized.get(key)
        if isinstance(value, str):
            try:
                normalized[key] = json.loads(value)
            except (TypeError, ValueError):
                normalized[key] = value
    return _local.set_filament_extra(filament_id, normalized)


def get_settings() -> dict[str, Any]:
    return {"extra_fields_spool": [], "extra_fields_filament": [], "base_url": ""}
