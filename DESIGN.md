# m0BamboSpool – Design-Entscheidungen

Diese Datei ist die zentrale und verbindliche Sammlung dauerhafter Projektentscheidungen.

## Dokumentation und Workflow

- `AGENTS.md` im Repository-Root ist die einzige verbindliche Arbeits- und Agentenregel.
- `docs/START.md` ist der Einstieg in die Fachdokumentation.
- Markdown verwendet Windows-Zeilenenden CRLF.
- Dokumentation wird auf Deutsch geführt.
- Dauerhafte Entscheidungen werden hier gepflegt; Detaildokumente definieren keine konkurrierenden Zielarchitekturen.
- Der Repository-Code ist Quelle der Wahrheit für den Implementierungsstand; diese Datei ist Quelle der Wahrheit für die beschlossene Zielarchitektur.
- `dev` ist der Entwicklungs- und Integrationsbranch, `main` bleibt stabil.
- Feature-Branches heißen `feature/<name>`, Fehlerbehebungs-Branches `bug/<name>` und werden immer von `dev` abgeleitet. Der Präfix `codex/` wird nicht verwendet.
- Änderungen werden geprüft und anschließend in `dev` integriert. Patch-ZIPs sind kein regulärer Übergabeweg.

## m0BamboSpool-Struktur

- m0BamboSpool wird als eigener Fork gepflegt.
- `app.py` darf für projektspezifische Anpassungen nicht geändert werden.
- Eigener Einstieg: `app_custom.py`.
- Bambu-Routen: `bambu_auth_routes.py`.
- Bambu-Authentifizierung: `bambu_auth.py`.
- Bambu-UI: `templates/bambu_auth.html`.
- Navigation: `templates/base.html`.
- Zertifikats-/Signing-Funktionalität: `bambu_certificate.py`.
- Eigene Funktionalität bleibt modular und updatefreundlich; keine aufgabenfremden Refactorings.
- Inventardaten werden über `inventory_repository.py` als zentrale Datenzugriffsschicht
  bezogen. Fachlogik und UI greifen nicht direkt auf die Spoolman-HTTP-API zu.
- Nicht benötigte Spoolman-Felder werden an dieser Grenze reduziert; interne Modelle
  enthalten nur die für m0BamboSpool benötigten Daten.
- Dauerhafte UI-Texte liegen zentral in `translations.py`; sichtbare Texte und
  Buttons dürfen nicht seitenweise fest verdrahtet werden.

## Bambu-Verbindungsmodi und Cloud

- Standard ist **Lokaler LAN-Modus**; **Online-Authentifizierung** bleibt erhalten.
- LAN-Modus verwendet `Printer Access LAN` / `PRINTER_ACCESS_LAN`.
- MQTT-Status in der UI nur verbunden/nicht verbunden; keine gelbe Zwischenanzeige.
- Bambu Cloud ist direkt im Hauptmenü integriert.
- Login mit E-Mail/Passwort und ggf. E-Mail-Verifizierungscode.
- Passwort wird nicht gespeichert.
- Access Token, numerische User-ID und Token-Metadaten dürfen lokal gespeichert werden.
- User-ID wird über `/v1/design-user-service/my/preference` ermittelt und der Token dort live geprüft.
- Token-, Credential- und Secret-Dateien dürfen nicht in Git gelangen.

## Bambu MQTT

- P1S lokal: TLS Port 8883, Benutzer `bblp`, Passwort = LAN Access Code.
- Report: `device/<SERIAL>/report`; Request: `device/<SERIAL>/request`.
- `BambuMqtt` ist ausschließlich für Transport, TLS, Subscribe, Empfang und Reconnect zuständig.
- JSON-Auswertung ist separat gekapselt und verarbeitet inkrementelle Reports über `Has...`-Flags.
- `nozzle_temper` gehört nicht zur gewünschten Standardausgabe; vollständiges Report-Dumping ist konfigurierbar.
- `pushall` und `ams_filament_setting` funktionieren im LAN-Pfad ohne RSA-SHA256/X.509-Signierung.
- Signing bleibt für den Online-/Cloud-Kontext relevant.
- Sparse AMS-Materialdaten werden serverseitig mit dem zuletzt bestätigten Cache ergänzt.
- `Clear` löscht zuerst am Drucker, danach die m0BamboSpool-/Spoolman-Zuordnung und invalidiert den Cache sofort.

## AMSHelper-Softwarearchitektur

- ESP32-S3, C#/.NET nanoFramework.
- Verifizierter Hardwarestand: ESP32-S3 Revision v0.2, 16 MB Flash, physisch
  erkanntes 8 MB PSRAM, eigenes Target `ESP32_S3_N16_NOPSRAM`, Visual-Studio-
  Device-Explorer über COM8.
- Deployment und Debugging des AMSHelper-Projekts aus Visual Studio funktionieren.
- WLAN, DHCP/IP-Konfiguration, DNS und Namensauflösung sind auf diesem Target
  bestätigt; WLAN bleibt in einer eigenen Netzwerkklasse.
- WLAN/Netzwerk gehört nicht in das gemeinsame ESP-/Hardwareobjekt.
- Vier `AmsTray`-Objekte repräsentieren AMS-Slot 0–3.
- `AmsTray` kapselt traybezogene PN532-/NTAG-/UID- und Zustandslogik; Reader wird intern initialisiert.
- **AMSHelper interessiert fachlich ausschließlich die lokale PN532-/NTAG-UID und der Tray-Zustand. Material, Farbe, Bambu-Tag-UID, Restmenge und sonstige Spulendaten werden im AMSHelper nicht benötigt und nicht als Tray-Fachzustand geführt.**
- Beim initialen Gesamtstatus werden alle vier Trays einmal mit Belegungszustand, PN532-Readerstatus und – falls bereits vorhanden – PN532-UID ausgegeben.
- Kein eigener Heartbeat-/Scheduler-Thread pro Tray. Der frühere Tray-Heartbeat mit `0/1/2/3`-Ausgabe ist entfernt.
- MQTT-Empfang und Tray-Aktionen bleiben logisch getrennt; Ereignisse werden bevorzugt.
- `m0BamboSpoolClient` kapselt HTTP-Kommunikation.
- AMSHelper meldet UID + AMS-Slot und zusätzlich „Tray leer“.
- UID→Spool-Auflösung erfolgt ausschließlich serverseitig in m0BamboSpool/Spoolman.
- C#-`if`-Anweisungen werden immer mit `{ }` geschrieben.

### Debug-/Trace-Ausgabe

- Fachthreads (MQTT, NFC, AMS, WLAN usw.) schreiben nicht physisch parallel in `System.Diagnostics.Debug`.
- Alle Debug-Ausgaben laufen über einen zentralen `TraceWriter` mit genau einem Writer-Thread.
- Producer legen `Write`-/`WriteLine`-Einträge nur in eine Queue; dadurch soll Debug-Ausgabe MQTT/NFC nicht unnötig blockieren.
- Die Trace-Queue ist auf **128 Einträge** begrenzt. Bei Überlauf wird der **älteste** Eintrag verworfen; Debug-Ausgabe darf die Gerätefunktion nicht gefährden.
- Der TraceWriter führt einen Drop-Zähler für verworfene Einträge.
- Der zentrale Speicher-/Queue-Heartbeat ist derzeit **temporär deaktiviert** (`TraceHeartbeatEnabled = false`). Die Trace-Queue selbst bleibt aktiv.

## NFC / NTAG215

- Zieltag ist NTAG215; Hardware-UID ist die stabile Identität.
- Bei neuem/leeren Tag wird NDEF geprüft und einmalig eine URL wie `/nfc/<UID>` geschrieben; sie bleibt bei Wiederverwendung unverändert.
- MIFARE Classic ist nicht Bestandteil der Zielimplementierung.
- NTAG213/215/216 werden über NXP `GET_VERSION (0x60)` unterschieden.
- NTAG21x `READ (0x30)` liefert vier Pages/16 Byte.
- Der früheste bestätigte NFC-Trigger des P1S ist `ams_get_rfid slot_id=<Tray>`; dieser startet den PN532-Lesezyklus.
- Pro `ams_get_rfid`-Zyklus wird genau eine gültige PN532-UID erfasst. Nach erfolgreichem Read endet das PN532-Polling sofort; spätere `tray_reading_bits` desselben Zyklus starten es nicht erneut.
- Beim PN532-Passive-Target-Scan wird `MaxRetryPassiveActivation = 0x00` verwendet, damit ein nicht gefundener Tag keinen zusätzlichen PN532-Retry verursacht.
- Der Reader-Thread prüft ein neu gesetztes Polling-Signal mit kurzer Idle-Latenz (20 ms); aktive Scans werden mit 50 ms Pause gefahren.

## PN532-Hardware

### Verifizierter aktueller Teststand

Ein PN532 V3 läuft über I²C1: VCC 3.3 V, GND, SDA GPIO8, SCL GPIO9; IRQ/RSTO unbenutzt. Adresse `0x24`. Firmwareerkennung und NTAG215-Zugriff sind bestätigt.

Dieser I²C-Aufbau ist der aktuelle Einzelreader-Teststand, **nicht** die Zielarchitektur für vier Reader.

### Zielarchitektur

- Vier PN532, einer pro AMS-Slot.
- Endgültige Vier-Reader-Anbindung über **SPI**.
- Gemeinsamer Clock/MOSI/MISO-Bus, eigener Chip-Select je Reader.
- Die frühere TCA9548A-/Vierfach-I²C-Planung ist verworfen.
- Direkte Parallelschaltung von vier PN532 mit gleicher I²C-Adresse ist ebenfalls verworfen.
- Der funktionierende I²C-Einzelreaderpfad bleibt erhalten, bis SPI praktisch umgesetzt und getestet ist.

## UI

- AMS links, `External Spool` rechts.
- Große Bildschirme: AMS `col-lg-8`, External Spool `col-lg-4`.
- Beide sind getrennte Geschwister-Karten.
- Grüne Erfolgsmeldungen schließen nach 10 Sekunden automatisch; manuelles Schließen bleibt möglich.
- History und Inventory verwenden tabellarische Darstellung mit Filterung und Sortierung;
  Filterzustände bleiben bei Aktualisierungen erhalten.
- Die Spoolman-Navigation wird in einem wiederverwendeten Fenster geöffnet, sofern
  bereits eines vorhanden ist.

## Entwicklung und Sicherheit

- Primärer Testweg ist Visual Studio Debug; Docker-Rebuild/Compose ist nicht Standard.
- Dokumentationsscreenshots verwenden den Einstieg `app_custom.py` und Live-Daten im Read-only-Modus; einen Snapshot-/Testdatenmodus gibt es nicht.
- Vor Änderungen den aktuellen Stand von `dev` beziehungsweise des zugehörigen Feature-/Bug-Branches prüfen und unabhängige Nutzeränderungen nicht überschreiben.
- Änderungen auf die konkrete Aufgabe begrenzen, Upstream-Kompatibilität erhalten und keine aufgabenfremden Formatierungs- oder Refactoring-Änderungen einführen.
- Den LAN- und Online-Verbindungsmodus erhalten, sofern nicht ausdrücklich anders entschieden.
- Vor Commit oder Übergabe Diff und geänderte Dateien auf unbeabsichtigte Änderungen und Geheimnisse prüfen.
- Tatsächlich ausgeführte Tests von offenen Laufzeit- oder Hardwaretests unterscheiden.
- Keine echten Geheimnisse in Git, Dokumentation oder Debugausgaben: insbesondere keine Passwörter,
  WLAN-Zugangsdaten, Bambu-LAN-Access-Codes, Tokens, API-Schlüssel, Session-Cookies oder privaten Schlüssel.
- In Dokumentation und Beispielkonfiguration ausschließlich erkennbare Platzhalter verwenden.
- Bambu-Passwörter und LAN-Access-Codes nur zur Anmeldung beziehungsweise Verbindung verwenden und nicht speichern.
- Access Token, numerische User-ID und erforderliche Token-Metadaten dürfen nur entsprechend der Implementierung
  lokal gespeichert werden; Credential-Dateien bleiben von Git ausgeschlossen.
