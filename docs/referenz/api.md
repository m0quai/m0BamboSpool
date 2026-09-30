# m0BamboSpool JSON API

> Einstieg: [START.md](../START.md)

Alle Endpoints liegen unter `/api/v1` und verwenden ein einheitliches Erfolg-/Fehler-Schema.

## Endpoints

- `GET /api/v1/printers` – bekannte Druckerinstanz(en).
- `GET /api/v1/printers/{printer_id}/ams` – AMS-/Tray-Status.
- `GET /api/v1/spools` – Spulen aus Spoolman.
- `POST /api/v1/printers/{printer_id}/ams/{tray_index}/assign` – Spule einem Tray zuweisen; optional `ams_id`.
- `POST /api/v1/printers/{printer_id}/ams/{tray_index}/unassign` – Zuweisung entfernen; optional `spool_id`.

Typische Fehler: `READ_ONLY_MODE`, `PRINTER_NOT_FOUND`, `TRAY_NOT_FOUND`, `SPOOL_NOT_FOUND`, `PRINTER_OFFLINE`.

Für exakte aktuelle Request-/Response-Felder ist `api_routes.py` die Quelle der Wahrheit.
