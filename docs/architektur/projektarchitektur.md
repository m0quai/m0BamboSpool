# Projektarchitektur und Wartungsstrategie

> Einstieg: [START.md](../START.md) · Entscheidungen: [Design](../../DESIGN.md)

m0BamboSpool wird als eigenständiger Fork mit Bambu-Lab-Erweiterungen gepflegt.

## Modulgrenzen

- `app.py`: alleiniger Anwendungseinstieg und integrierte Fork-Anpassungen.
- `local_settings_routes.py`: lokale Drucker-Konfiguration.
- `templates/local_settings.html`: Oberfläche für die lokalen Einstellungen.
- `templates/base.html`: Hauptnavigation der lokalen Verwaltung.

## Wartungsstrategie

Neue eigene Module werden großen Änderungen an einzelnen Fachkomponenten vorgezogen. Änderungen bleiben modular und updatefreundlich. Es gibt nur einen Anwendungseinstieg.

Der aktuelle Code ist Quelle der Wahrheit für den Implementierungsstand; verbindliche Zielentscheidungen stehen in `DESIGN.md`.
