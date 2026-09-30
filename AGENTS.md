# m0BamboSpool / AMSHelper – verbindliche Arbeitsregeln

Diese Datei ist die einzige verbindliche Codex- und Entwicklerdokumentation unter
den Solution Items. Ältere parallele Anleitungen dürfen keine abweichenden Regeln
enthalten.

## Repository, Branches und Arbeitsweise

- Repository: `m0quai/m0BamboSpool`.
- `dev` ist der aktuelle Entwicklungs- und Integrationsbranch.
- `main` bleibt der stabile Branch.
- Feature-Branches heißen ausschließlich `feature/<name>`.
- Fehlerbehebungs-Branches heißen ausschließlich `bug/<name>`.
- Jeder Feature- oder Bug-Branch wird immer direkt von `dev` abgeleitet:
  `git switch -c feature/<name> dev` beziehungsweise `git switch -c bug/<name> dev`.
- Der Präfix `codex/` wird für Branches nicht verwendet.
- Änderungen werden nach Prüfung in `dev` integriert. `main` wird nur nach einer
  bewussten Freigabe aktualisiert.

Vor jeder Änderung:

1. `git status --short` prüfen.
2. `git branch --show-current` prüfen.
3. Bei sauberem Arbeitsbaum den aktuellen Stand des aktiven Branches holen:
   `git pull --ff-only origin dev` (nur auf `dev`) beziehungsweise den passenden
   Feature-/Bug-Branch aktualisieren.
4. Diese Datei sowie die betroffenen Dateien und ihre Architektur lesen.
5. Lokale Änderungen niemals ungefragt verwerfen, überschreiben oder resetten.

Nach Änderungen:

- `git diff` und `git status` prüfen.
- Passende Tests, Builds und Laufzeitprüfungen ausführen.
- Projektdateien (`.sln`, `.pyproj`, `.nfproj`, `packages.config` usw.) mitpflegen.
- Keine Build-Artefakte, IDE-Dateien, Tokens, Passwörter oder sonstige Secrets
  committen.
- Commit-Nachrichten kurz und beschreibend halten.

Keine Force-Pushes und keine History-Rewrites ohne ausdrücklichen Auftrag.

- Kommentare im Source-Code immer mit `#` beziehungsweise dem jeweiligen
  sprachüblichen Kommentarzeichen schreiben; Triple-Quote-Strings nicht als
  Kommentare verwenden.
- Textdateien werden mit Windows-Zeilenenden (`CRLF`) gespeichert und beendet.

## m0BamboSpool

- m0BamboSpool wird als eigener Fork gepflegt; Upstream ist `drndos/openspoolman`.
- Änderungen möglichst updatefreundlich halten.
- `app.py` nicht ändern, sofern es nicht ausdrücklich erforderlich und beauftragt
  ist. Eigene Erweiterungen gehören bevorzugt in `app_custom.py` sowie getrennte
  Module oder Blueprints.
- Bestehende Trennung für Bambu-Authentifizierung (z. B. `bambu_auth.py`) achten.
- Inventarzugriffe laufen über die allgemeine Repository-/Datenzugriffsschicht
  (`inventory_repository.py`); direkte API-Aufrufe in UI- oder Fachlogik vermeiden.
- Die Spool-/UID-Zuordnung bleibt auf m0BamboSpool-Seite; AMSHelper löst keine UID
  selbst in eine Spool-ID auf.
- Weboberfläche und bestehende API-Verträge nicht ohne ausdrücklichen Auftrag
  inkompatibel ändern.
- Token-, Credential- und Secret-Dateien gehören nicht in Git.

## AMSHelper-Architektur

AMSHelper ist eine nanoFramework-Anwendung für ESP32-S3 und Bambu Lab P1S/AMS.

- `BambuMqtt`: lokaler Bambu-MQTT-Transport, TLS, Subscribe, Empfang und Reconnect.
- `BambuStatusParser`: nur tatsächlich vorhandene JSON-Felder auswerten; fehlende
  Felder nicht als neue Werte interpretieren.
- WLAN/Netzwerk bleibt eine eigene Komponente und gehört nicht in ein gemeinsames
  `Esp`-Objekt.
- `m0BamboSpoolClient` kapselt die Kommunikation mit m0BamboSpool.
- Vier `AmsTray`-Objekte repräsentieren Tray 0 bis 3.
- Traybezogene PN532-/NFC-Logik gehört in bzw. hinter die Tray-Abstraktion.
- Aktionen möglichst ereignis- oder statusgetrieben statt unnötigem Dauerpolling.
- Event-Handler nach Möglichkeit gekapselt und asynchron gestalten, soweit die
  nanoFramework-API dies unterstützt.

### MQTT

- TLS-Port `8883`, Benutzer `bblp`.
- Das Passwort ist der Printer LAN Access Code und darf niemals hardcodiert oder
  committed werden.
- Report: `device/<SERIAL>/report`.
- Requests: `device/<SERIAL>/request`.
- Standardtelemetrie gering halten; Debug-Ausgaben zielgerichtet und konfigurierbar.

### NFC und Hardware

- Zielhardware: ESP32-S3 mit vier PN532-Lesern und NTAG215-Tags.
- Gemeinsamer SPI-Bus mit getrenntem SS/CS je Reader.
- Kein I2C-Multiplexer und kein neues MIFARE-Classic-Design.
- Vorhandener Einzel-PN532-I2C-Code ist nur Übergangs-/Teststand.

## Diagnose und Logging

- Zentrale Trace-/Debug-Ausgabe verwenden (`TraceWriter` oder vorhandene zentrale
  Implementierung).
- Debug-Queues begrenzen; keine unbeschränkt wachsenden Queues.
- Heartbeat kompakt um freien Speicher sowie Queue-/Drop-Informationen ergänzen,
  sofern implementiert.
- Keine hochfrequente Standardtelemetrie ohne konkreten Diagnosezweck.

## C# und nanoFramework

- `if`-Anweisungen immer mit `{ }`, auch bei nur einer Anweisung.
- Bestehende Namespace-/Ordnerstruktur respektieren (`Ams`, `Config`, `Diagnostics`,
  `Hardware`, `Mqtt`, `Network`, `Nfc`, `m0BamboSpool`).
- Vor neuen NuGet-Paketen die nanoFramework-Kompatibilität prüfen.
- Keine Desktop-.NET-APIs verwenden, die nanoFramework nicht unterstützt.

## Tests und Validierung

- Für AMSHelper ist Visual-Studio-Debug der primäre Testweg.
- Bei Webänderungen zusätzlich die betroffene Seite im Browser/DOM prüfen.
- Bei Änderungen an Container-, MQTT- oder Laufzeitverhalten gezielt Docker-/Runtime-
  Prüfungen ausführen; ein erfolgreicher Build allein gilt nicht als UI-Abnahme.
- Bei AMSHelper-Änderungen Projekt-/NuGet-Referenzen und nanoFramework-Kompatibilität
  prüfen.

## Priorität bei Widersprüchen

1. Aktuelle ausdrückliche Benutzeranweisung.
2. Diese `AGENTS.md`.
3. Aktueller Code und aktuelle Projektdokumentation.
4. Ältere Annahmen oder frühere Chat-Kontexte.

Wenn Architektur und aktueller Code abweichen, die Abweichung zuerst benennen und
nicht stillschweigend eine neue Architektur einführen.

## Historischer Projektkontext

Bei OSM-Arbeiten den abgeglichenen Kontext in `OSM-PROJEKTKONTEXT.md`
berücksichtigen. Er enthält historische Fakten, Quellen und offene Aufgaben.
Diese AGENTS.md bleibt die einzige verbindliche Arbeitsanweisung; alte Chats
und Erinnerungen dürfen den aktuellen Stand dieses PCs nicht überschreiben.
