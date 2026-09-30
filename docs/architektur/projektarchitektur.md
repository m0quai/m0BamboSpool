# Projektarchitektur und Wartungsstrategie

> Einstieg: [START.md](../START.md) · Entscheidungen: [Design](../../DESIGN.md)

m0BamboSpool wird als eigener Fork mit Bambu-Lab-Erweiterungen gepflegt. Eigene Funktionalität bleibt möglichst von Upstream-Code getrennt.

## Modulgrenzen

- `app_custom.py`: eigener Anwendungseinstieg; `app.py` bleibt für projektspezifische Anpassungen unangetastet.
- `bambu_auth.py`: Bambu-Authentifizierungslogik.
- `bambu_auth_routes.py`: Routen/Handler für Bambu-Authentifizierung und Konfiguration.
- `bambu_certificate.py`: Zertifikats-/Signierungsfunktionalität.
- `templates/bambu_auth.html`: Bambu-spezifische Oberfläche.
- `templates/base.html`: Hauptnavigation einschließlich „Bambu Cloud“.

## Wartungsstrategie

Neue eigene Module werden großen Änderungen an Upstream-Dateien vorgezogen. Integration erfolgt möglichst über `app_custom.py`. Änderungen an Upstream-Templates bleiben klein. Bestehende eigene Module werden nicht ohne konkreten Grund umbenannt.

Der aktuelle Code ist Quelle der Wahrheit für den Implementierungsstand; verbindliche Zielentscheidungen stehen in `DESIGN.md`.
