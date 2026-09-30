# OpenSpoolMan's narrow data-access boundary for spool inventory.
#
# Application code should use this module instead of constructing SpoolMan API
# requests or depending on SpoolMan's complete object model.  The implementation
# currently delegates to :mod:`spoolman_client`; replacing that adapter later
# with a local database does not require changing the callers.

from typing import Any
import json

import spoolman_client as _spoolman_api
import inventory_database as _local
from config import USE_SPOOLMAN

_REMOTE = {
    "list": _spoolman_api.fetchSpoolList,
    "vendors": _spoolman_api.fetchVendorList,
    "filaments": _spoolman_api.fetchFilamentList,
    "get": _spoolman_api.getSpoolById,
    "consume": _spoolman_api.consumeSpool,
    "spool_extra": _spoolman_api.patchExtraTags,
    "filament_extra": _spoolman_api.patchFilamentExtra,
    "settings": _spoolman_api.fetchSettings,
}


def get_backend() -> str:
    return "spoolman" if USE_SPOOLMAN else "local"


def ensure_local_catalog() -> None:
    # Create the local catalog tables and import the remote catalog once for preview pages.
    _local.initialize()
    _import_remote_once()


def _import_remote_once() -> None:
    if not _local.needs_spoolman_import():
        return
    try:
        vendors = _REMOTE["vendors"]()
        filaments = _REMOTE["filaments"]()
        spools = _REMOTE["list"](include_archived=True)
        _local.import_spoolman(vendors, filaments, spools)
    except Exception:
        return


def list_spools(*, include_archived: bool = False) -> list[dict[str, Any]]:
    if get_backend() == "local":
        _import_remote_once()
        return _local.list_spools(include_archived=include_archived)
    try:
        return _REMOTE["list"](include_archived=include_archived)
    except Exception:
        # Keep historical spool references resolvable while Spoolman is
        # temporarily unavailable.  The local catalog is a read-only mirror
        # for this fallback; consumption still uses the configured backend.
        _local.initialize()
        return _local.list_spools(include_archived=include_archived)


def get_spool(spool_id: int | str) -> dict[str, Any]:
    if get_backend() == "local":
        _import_remote_once()
        return _local.get_spool(spool_id)
    try:
        return _REMOTE["get"](spool_id)
    except Exception:
        _local.initialize()
        return _local.get_spool(spool_id)


def record_consumption(
    spool_id: int | str,
    *,
    weight_grams: float | None = None,
    length_mm: float | None = None,
    occurred_at: str | None = None,
) -> bool:
    if get_backend() == "local":
        _local.record_consumption(spool_id, weight_grams, length_mm, occurred_at)
        return True
    return _REMOTE["consume"](
        spool_id,
        use_weight=weight_grams,
        use_length=length_mm,
        occurred_at=occurred_at,
    )


def update_spool_extra(spool_id: int | str, old_extra: dict[str, Any] | None, **values: Any) -> None:
    if get_backend() == "local":
        _local.set_spool_extra(spool_id, values)
        return
    _REMOTE["spool_extra"](spool_id, dict(old_extra or {}), values)


def update_filament_metadata(
    filament_id: int | str,
    old_extra: dict[str, Any] | None,
    values: dict[str, Any],
) -> dict[str, Any]:
    if get_backend() == "local":
        normalized = dict(values)
        if "nozzle_temperature" in normalized:
            normalized["nozzle_temp"] = normalized.pop("nozzle_temperature")
        if "filament_id" in normalized:
            normalized["bambu_filament_id"] = normalized.pop("filament_id")
        if "setting_id" in normalized:
            normalized["bambu_setting_id"] = normalized.pop("setting_id")
        for key in ("nozzle_temp", "cali_idx", "bambu_filament_id", "bambu_setting_id"):
            value = normalized.get(key)
            if isinstance(value, str):
                try:
                    normalized[key] = json.loads(value)
                except (TypeError, ValueError):
                    normalized[key] = value
        return _local.set_filament_extra(filament_id, normalized)
    return _REMOTE["filament_extra"](filament_id, old_extra, values)


def get_settings() -> dict[str, Any]:
    if get_backend() == "local":
        return {"extra_fields_spool": [], "extra_fields_filament": [], "base_url": ""}
    return _REMOTE["settings"]()


def install_spoolman_compatibility_adapter() -> None:
    """Keep existing app.py callers on the selected backend without editing app.py."""
    _spoolman_api.fetchSpoolList = lambda include_archived=False: list_spools(include_archived=include_archived)
    _spoolman_api.getSpoolById = get_spool
    _spoolman_api.consumeSpool = lambda spool_id, use_weight=None, use_length=None, occurred_at=None: record_consumption(
        spool_id, weight_grams=use_weight, length_mm=use_length, occurred_at=occurred_at
    )
    _spoolman_api.patchExtraTags = lambda spool_id, old_extras, new_extras: update_spool_extra(spool_id, old_extras, **new_extras)
    _spoolman_api.patchFilamentExtra = lambda filament_id, old_extra, values: update_filament_metadata(filament_id, old_extra, values)
    _spoolman_api.fetchSettings = get_settings
