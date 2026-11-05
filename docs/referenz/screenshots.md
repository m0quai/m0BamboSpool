# Screenshots

> Einstieg: [START.md](../START.md)

Dokumentationsscreenshots werden mit `scripts/generate_screenshots.py` aus der über `app.py` gestarteten Anwendung erzeugt und unter `docs/img/` aktualisiert. Die Konfiguration liegt in `scripts/screenshot_config.json`. Screenshot-spezifische Abhängigkeiten nur installieren, wenn Dokumentationsbilder tatsächlich neu erzeugt werden.

Live-Aufnahmen müssen read-only erfolgen, sofern nicht ausdrücklich Schreibzugriffe verlangt werden. Credential-/Konfigurationsdaten aus `config.env` dürfen niemals in Screenshots oder Git-Ausgaben gelangen.

Für die vollständigen aktuellen CLI-Optionen sind `scripts/generate_screenshots.py` und dessen Konfiguration die Quelle der Wahrheit.
