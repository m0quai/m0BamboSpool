import json
import traceback

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import mqtt_bambulab
import nfc_pending_repository
import inventory_repository as spool_repo
import inventory_database as inventory_db
import inventory_service

bp = Blueprint("ams_nfc", __name__, url_prefix="/ams/nfc")
nfc_pending_repository.initialize_storage()


def _clean_extra_value(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _normalize_uid(value):
    return str(value or "").strip().upper().replace(":", "-")


def _find_spool_by_uid(uid):
    wanted = _normalize_uid(uid)
    for spool in inventory_service.fetchSpools(cached=True):
        extras = spool.get("extra") or {}
        tag = _normalize_uid(_clean_extra_value(extras.get("tag")))
        if tag == wanted:
            return spool
    return None


def _resolve_tray(tray_index):
    config = mqtt_bambulab.getLastAMSConfig() or {}
    for ams in config.get("ams", []):
        ams_id = int(ams.get("id", -1))
        for tray in ams.get("tray", []):
            if int(tray.get("id", -1)) == tray_index:
                return ams_id, tray_index
    if 0 <= tray_index <= 3:
        return 0, tray_index
    return None, None


def _remember_pending(uid, tray_index, ams_id):
    inventory_db.register_nfc_tag(uid)
    nfc_pending_repository.remember_pending_tag(uid, tray_index, ams_id)


def _remove_pending(uid):
    nfc_pending_repository.remove_pending_tag(uid)


def _assign_spool_to_tray(spool, ams_id, tray_id):
    spool_id = spool["id"]
    mqtt_bambulab.setActiveTray(spool_id, spool.get("extra") or {}, ams_id, tray_id, source="nfc", nfc_uid=spool.get("extra", {}).get("tag"))
    from app import setActiveSpool
    setActiveSpool(ams_id, tray_id, spool)
    return spool_id


@bp.post("/<int:tray_index>/set")
def set_nfc_tray(tray_index):
    body = request.get_json(silent=True) or {}
    uid = _normalize_uid(body.get("uid"))
    if not uid:
        return jsonify({"success": False, "error": "Field 'uid' is required."}), 400

    ams_id, tray_id = _resolve_tray(tray_index)
    if ams_id is None:
        return jsonify({"success": False, "error": f"Tray '{tray_index}' not found."}), 404

    try:
        if uid == "CLEAR":
            if inventory_service.get_nfc_bound_spool_for_tray(ams_id, tray_id):
                return jsonify({
                    "success": False,
                    "error": "NFC-bound spool can only be released after physical removal from the AMS.",
                }), 409
            inventory_service.clear_active_spool_for_tray(ams_id, tray_id)
            return jsonify({"success": True, "action": "clear", "tray_index": tray_index, "ams_id": ams_id})

        spool = _find_spool_by_uid(uid)
        if not spool or spool.get("id") is None:
            _remember_pending(uid, tray_index, ams_id)
            return jsonify({
                "success": True,
                "action": "pending",
                "tray_index": tray_index,
                "ams_id": ams_id,
                "uid": uid,
                "message": "Unknown NFC tag stored for assignment.",
            }), 202

        spool_id = _assign_spool_to_tray(spool, ams_id, tray_id)
        _remove_pending(uid)
        return jsonify({"success": True, "action": "assign", "tray_index": tray_index, "ams_id": ams_id, "uid": uid, "spool_id": spool_id})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.get("/pending")
def pending_tags():
    return render_template("nfc_pending.html", pending_tags=nfc_pending_repository.list_pending_tags(), spools=inventory_service.fetchSpools(cached=True))


@bp.post("/pending/<path:uid>/assign")
def assign_pending_tag(uid):
    normalized_uid = _normalize_uid(uid)
    spool_id_raw = request.form.get("spool_id", "").strip()
    try:
        spool_id = int(spool_id_raw)
    except ValueError:
        return redirect(url_for("ams_nfc.pending_tags", error="Bitte eine Spule auswählen."))

    pending = nfc_pending_repository.get_pending_tag(normalized_uid)
    if pending is None:
        return redirect(url_for("ams_nfc.pending_tags", error="NFC-Tag wurde nicht gefunden."))

    spool = next((item for item in inventory_service.fetchSpools(cached=False) if int(item.get("id", -1)) == spool_id), None)
    if spool is None:
        return redirect(url_for("ams_nfc.pending_tags", error="Spule wurde nicht gefunden."))

    try:
        extras = spool.get("extra") or {}
        spool_repo.update_spool_extra(spool_id, extras, tag=json.dumps(normalized_uid))
        inventory_db.ensure_local_spool_from_inventory(spool)
        inventory_db.update_spool_fields(spool_id, {"tag": normalized_uid})
        spool.setdefault("extra", {})["tag"] = json.dumps(normalized_uid)
        _assign_spool_to_tray(spool, int(pending["ams_id"]), int(pending["tray_index"]))
    except ValueError as exc:
        return redirect(url_for("ams_nfc.pending_tags", error=str(exc)))
    except Exception as exc:
        traceback.print_exc()
        return redirect(url_for("ams_nfc.pending_tags", error=str(exc)))
    _remove_pending(normalized_uid)
    return redirect(url_for("ams_nfc.pending_tags", success="NFC-Tag wurde der Spule zugeordnet."))
