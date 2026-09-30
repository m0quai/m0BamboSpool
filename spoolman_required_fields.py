import json
import logging
import os
import requests

log = logging.getLogger(__name__)

REQUIRED_FIELDS = {
    "filament": [
        {"key": "type", "name": "Type", "field_type": "choice",
         "choices": ["AERO","CF","GF","FR","Basic","HF","Translucent","Aero","Dynamic",
                     "Galaxy","Glow","Impact","Lite","Marble","Matte","Metal","Silk","Silk+",
                     "Sparkle","Tough","Tough+","Wood","Support for ABS","Support for PA PET",
                     "Support for PLA","Support for PLA-PETG","G","W","85A","90A","95A",
                     "95A HF","for AMS"]},
        {"key": "nozzle_temperature", "name": "Nozzle Temperature",
         "field_type": "integer_range", "unit": "°C"},
        {"key": "filament_id", "name": "Filament ID", "field_type": "text"},
    ],
    "spool": [
        {"key": "tag", "name": "tag", "field_type": "text"},
        {"key": "active_tray", "name": "Active Tray", "field_type": "text"},
    ],
}

def _runtime_base_url():
    # Prefer the runtime URL selected by app_custom.py. Environment
    # SPOOLMAN_BASE_URL is restored to the public/browser URL after importing
    # the original app, so it must NOT be used by this startup worker.
    runtime = os.getenv("SPOOLMAN_RUNTIME_BASE_URL", "").strip()
    if runtime:
        return runtime.rstrip("/")

    # Compatibility fallback for older configurations.
    internal = os.getenv("SPOOLMAN_INTERNAL_BASE_URL", "").strip()
    if internal:
        return internal.rstrip("/")

    return os.getenv("SPOOLMAN_BASE_URL", "http://localhost:7912").rstrip("/")

def _raise_with_body(response):
    if response.ok:
        return
    body = response.text.strip()
    if len(body) > 1200:
        body = body[:1200] + "..."
    raise RuntimeError(
        f"HTTP {response.status_code} {response.reason} for {response.url}"
        + (f" | response: {body}" if body else "")
    )

def ensure_required_spoolman_fields():
    api = _runtime_base_url() + "/api/v1"
    print(f"[m0BamboSpool] Checking required Spoolman fields via {api} ...")
    try:
        for entity, definitions in REQUIRED_FIELDS.items():
            response = requests.get(f"{api}/field/{entity}", timeout=10)
            _raise_with_body(response)
            existing = response.json() or []
            keys = {item.get("key") for item in existing if isinstance(item, dict)}

            for definition in definitions:
                key = definition["key"]
                if key in keys:
                    print(f"[m0BamboSpool] [OK] {entity}/{key}")
                    continue

                # Spoolman creates an extra field via
                # POST /field/{entity}/{field_key}. The key is part of the URL.
                # default_value must be present and JSON-encoded; this is also
                # how established Spoolman integrations create extra fields.
                body = {
                    "name": definition["name"],
                    "field_type": definition["field_type"],
                    "default_value": json.dumps(None),
                }
                if "unit" in definition:
                    body["unit"] = definition["unit"]
                if "choices" in definition:
                    body["choices"] = definition["choices"]

                created = requests.post(
                    f"{api}/field/{entity}/{key}",
                    json=body,
                    timeout=10,
                )
                _raise_with_body(created)
                print(f"[m0BamboSpool] [CREATED] {entity}/{key}")

        print("[m0BamboSpool] Required Spoolman fields ready.")
        return True
    except Exception as exc:
        print(f"[m0BamboSpool] Required field check skipped/failed: {exc}")
        return False
