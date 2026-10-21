# m0BamboSpool

`m0BamboSpool` ist ein persönliches Dashboard für Bambu-Lab-Drucker, AMS-Fächer
und Filamentverbrauch. Das Projekt wird als eigenständige, auf den täglichen
Druckbetrieb zugeschnittene Anwendung weiterentwickelt.

## Zweck

Die Anwendung verbindet den Druckerstatus, die AMS-Zuordnung, Druckhistorie und
Filamentverbrauch in einer Oberfläche. Ziel ist eine nachvollziehbare Zuordnung
von Filament zu Spulen und Druckaufträgen – auch dann, wenn der Rechner oder die
Netzwerkverbindung während eines Drucks vorübergehend nicht verfügbar war.

## Umgesetzte Funktionen

- Bambu-Lab-Kommunikation über lokales MQTT mit Status-, Temperatur- und AMS-Daten
- Home-Seite mit aktuellem Druckerstatus und Details zum letzten Druck
- AMS-Ansicht mit Fächern, Filament, Farbcodes und externer Spule
- Druckhistorie mit Status, Fortschritt, Schichten, Verbrauch und erwarteten Zeiten
- Inventaransicht für Spulen, Hersteller, Filamente, Material und Archivstatus
- Filter- und Sortierfunktionen für die relevanten Tabellen
- Mehrsprachige Benutzeroberfläche für Deutsch und Englisch
- 3MF-Verarbeitung mit Modell-/Objektnamen, Vorschaubildern und gespeicherten
  Metadaten
- Wiederaufnahme und Recovery von Druckaufträgen nach Verbindungsunterbrechungen
- Idempotente Filament-Verbrauchsbuchungen mit Ereignisstatus, damit Buchungen
  nicht doppelt erfasst werden
- Zentrale Datenschnittstelle über `inventory_repository.py` für alle lokalen
  Inventarzugriffe
- Lokale Verwaltungs- und Datenbanktabellen für Hersteller, Filamente und Spulen
  als Grundlage für eine eigene Verwaltung
- About-Seite mit Projektinformationen und Referenzen

## Eigene NFC-Hardware

Zum Projekt gehört AMSHelper für eine eigene Hardware-Anbindung. Zielplattform
ist ein ESP32-S3 mit vier PN532-Lesern und NTAG215-Tags – je ein Reader pro
AMS-Fach. Die Firmware liest Spulen-Tags, ordnet sie den Fächern zu und
kommuniziert über MQTT mit m0BamboSpool.

Die Hardware-Komponenten sind getrennt aufgebaut:

- Bambu-MQTT-Transport und Statusparser
- WLAN-/Netzwerkkomponente
- vier `AmsTray`-Objekte für die AMS-Fächer
- PN532-/NFC-Abstraktion für die Tray-bezogene Zuordnung
- m0BamboSpool-Client für die Kommunikation mit dem Dashboard

Die technische Dokumentation der Hardware liegt unter [`docs/amshelper`](docs/amshelper/).

## Eigene Verwaltung

Die lokale SQLite-Datenbank ist die alleinige Verwaltungsbasis. Die lokale
Repository-/Datenzugriffsschicht kapselt alle Zugriffe und enthält nur die
Felder, die m0BamboSpool tatsächlich benötigt.

Die vorbereiteten lokalen Verwaltungsfunktionen umfassen:

- Hersteller, Materialien, Filamente und Spulen
- Zuordnung von Spulen zu Filamenten und AMS-Fächern
- Archivstatus und letzte Verwendung
- Editieren, Neuanlage und Kopieren von Spulen
- manuelle Bestandskorrektur absolut oder als Differenz
- farbliche Darstellung und Hex-Farbwerte für Filamente

## Start mit Docker

Voraussetzungen sind Docker und Docker Compose. Konfiguration und Zugangsdaten
werden ausschließlich über `config.env` beziehungsweise
`config.env.template` bereitgestellt.

```powershell
Set-Location C:\Development\Docker\m0BamboSpool
docker compose up -d --build
```

Projektspezifische Konfigurationsschlüssel verwenden `M0BAMBOSPOOL_`.

Danach ist die Oberfläche unter [http://localhost:8000](http://localhost:8000)
erreichbar. Der Container heißt `m0BamboSpool`.

Die persistenten Daten liegen in:

- `data/` – lokale Datenbank und Metadaten
- `logs/` – Laufzeit- und Diagnoseprotokolle
- `static/prints/` – Vorschaubilder und druckbezogene Dateien

## Projekt

- [m0BamboSpool](https://github.com/m0quai/m0BamboSpool)

## Entwicklungsregeln

Die verbindlichen Arbeitsregeln stehen in [`AGENTS.md`](AGENTS.md). Der
Entwicklungsbranch ist `dev`; neue Arbeiten erfolgen ausschließlich auf
`feature/<name>`- oder `bug/<name>`-Branches, die von `dev` abgeleitet werden.
