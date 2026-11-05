# m0BamboSpool – Ablauf- und Klassenplan

Stand: 05.11.2026. Diese Übersicht beschreibt den aktuellen Codebestand. Sie ist als Landkarte gedacht; für konkrete Details gilt der jeweilige Quelltext.

## 1. Systemgrenze

Die Anwendung läuft als Flask-/Waitress-Dienst im Docker-Container. SQLite ist der lokale Datenbestand und die führende Verwaltung für Filamente, Spulen, Farben, Materialien, NFC-Tags, Druckhistorie und Verbrauch. MQTT liefert den Drucker-/AMS-Zustand. 3MF/G-Code-Dateien liefern Projekt- und Verbrauchsprognosen. Der Browser rendert serverseitige Jinja-Vorlagen und fragt Live-Zustand bei Bedarf über HTTP ab.

```mermaid
flowchart LR
  Printer["Bambu Drucker"] -->|MQTT Status / AMS| MQTT[mqtt_bambulab.py]
  Printer -->|FTP 3MF| Worker[jobs_3mf.py + tools_3mf.py]
  Browser[Browser] <-->|HTTP / JSON| Flask["Flask in app.py"]
  MQTT --> Tracker[filament_usage_tracker.py]
  Worker --> Tracker
  Flask --> History[print_history.py]
  Tracker --> History
  Flask --> Inventory[inventory_service.py]
  Inventory --> Repo[inventory_repository.py]
  Repo --> LocalDB[inventory_database.py]
  History --> DB[(SQLite osm.db)]
  LocalDB --> DB
  MQTT --> DB
  Flask --> Templates[Jinja templates]
  Templates --> Browser
```

## 2. Programmstart

```mermaid
flowchart TD
  A[Container startet] --> B[Waitress lädt wsgi.py]
  B --> C[wsgi importiert app aus app.py]
  C --> D[Konfiguration und Version laden]
  D --> E[SQLite verbinden, Schema prüfen / Migrationen]
  E --> F[Blueprints und Routen registrieren]
  F --> G[Lokalen Inventarkatalog initialisieren]
  G --> H[MQTT Verbindung / Druckerstatus abonnieren]
  H --> I[HTTP Dienst bedient Browser und API]
  H --> J[MQTT Callback aktualisiert Zustand und Historie]
  J --> K[3MF-Aufträge an Hintergrund-Worker übergeben]
```

`app.py` ist der Anwendungseinstieg und bindet die Routen und Dienste zusammen. `wsgi.py` exportiert das Flask-Objekt für den Server. `local_settings_routes.py`, `api_routes.py` und `nfc_routes.py` kapseln ausgewählte Routengruppen.

## 3. Druckauftrag: Erkennung bis History und Verbrauch

```mermaid
flowchart TD
  A[MQTT project_file / RUNNING] --> B{Ist bereits ein aktiver Drucktransfer vorhanden?}
  B -- Ja --> C[Transfer und Print-ID wiederverwenden]
  B -- Nein --> D[Print-ID einmalig anlegen]
  C --> E[3MF-Auftrag in jobs_3mf einreihen]
  D --> E
  E --> F[FTP-Datei laden: frisches Handle, Stillstand / Retry beachten]
  F --> G{Datei und G-Code gültig?}
  G -- Nein --> H[Download abbrechen / erneuter Versuch; Verbrauch nicht final buchen]
  G -- Ja --> I[tools_3mf liest Projekt, Filamente, Farben, Mengen, Layer, Vorschau]
  I --> J[3MF-Prognose mit MQTT-Projektmapping und lokaler AMS-Belegung auflösen]
  J --> K{Mapping eindeutig und vollständig?}
  K -- Nein --> L[Unresolved erhalten; Verbrauch puffern]
  K -- Ja --> M[Zuordnung G-Code-Filament → physisches AMS-Fach → lokale Spule]
  M --> N[Layer-Tracker beobachtet Extrusion und Filamentwechsel]
  L --> N
  N --> O{Eindeutiges Tray und Spule für Ereignis?}
  O -- Nein --> P[Verbrauch gepuffert, keine falsche Spule belasten]
  O -- Ja --> Q[Event deduplizieren und Spulenverbrauch buchen]
  P --> R[MQTT Status / Zuordnung erneut bewerten]
  R --> K
  Q --> S[History- und Layerstatus aktualisieren]
  S --> T{Druck läuft weiter?}
  T -- Ja --> N
  T -- Nein --> U[Abschluss / Abbruch festschreiben, Tracker stoppen]
```

### Zuordnungsregeln

1. 3MF `slice_info.config` beschreibt logische Filament-IDs und erwartete Mengen. G-Code-Filamentindex ist nicht automatisch gleich dem AMS-Fach.
2. MQTT-Projektmapping und beobachtete `tray_tar`-Wechsel liefern die physische Fachzuordnung.
3. `inventory_service`/lokaler Katalog ordnet Fach-UID dem lokalen Spulenrecord zu. Die lokale AMS-Spulenbelegung ist die fachliche Quelle.
4. Farbe ist nur ein Plausibilitätsmerkmal. Sie ist nicht eindeutig, weil dieselbe Farbe mehrfach vorkommen kann.
5. Bei fehlender oder widersprüchlicher Evidenz bleibt die Zuordnung offen und Verbrauch wird zwischengespeichert.
6. Verbrauchsereignisse werden nach Druck, Layer und Filamentindex dedupliziert; erst bestätigte Ereignisse verändern den Spulenbestand.

## 4. Fachliche Hauptabläufe

### AMS-Status

```mermaid
flowchart TD
  A[MQTT Status mit AMS trays] --> B[AMS-Zustand normalisieren / leere Fächer erkennen]
  B --> C[Spule per lokaler Active-Tray-UID finden]
  C --> D{Tray leer?}
  D -- Ja --> E[Zuordnung freigeben, AMS-Ansicht als leer aktualisieren]
  D -- Nein --> F{NFC-Tag erkannt?}
  F -- Ja --> G[Tag → Spule → Fach prüfen und Zuordnung sichern]
  F -- Nein --> H[Vorhandene manuelle lokale Zuordnung erhalten]
  E --> I[AMS/API/Home/Inventar aktualisierbaren Zustand bereitstellen]
  G --> I
  H --> I
```

### Inventaränderung

```mermaid
flowchart LR
  UI[Inventarformular] --> Route[app.py Route]
  Route --> Service[inventory_service.py]
  Service --> Repo[inventory_repository.py]
  Repo --> DB[(SQLite)]
  Repo --> Cache[Inventarcache aktualisieren]
  Cache --> UI
  Route -->|gesperrt / archiviert / aktive AMS| Guard{Validierung}
  Guard -->|ungültig| Error[Änderung ablehnen und Meldung anzeigen]
  Guard -->|gültig| Service
```

### NFC-Zuordnung

```mermaid
flowchart TD
  A[ESP32 / NFC UID] --> B[AMSHelper überträgt UID]
  B --> C[nfc_routes.py nimmt UID für Fach entgegen]
  C --> D{Tag und Spule lokal bekannt?}
  D -- Nein --> E[NFC-Tag registrieren / Pending-Zuordnung merken]
  D -- Ja --> F[Konflikte prüfen: Tag bereits belegt? Fach geschützt? Druck aktiv?]
  F --> G{Konflikt?}
  G -- Ja --> H[Änderung ablehnen]
  G -- Nein --> I[Spule Active-Tray-UID und Quelle NFC setzen]
  I --> J[AMS-/Inventarzustand aktualisieren]
```

## 5. Module und Klassen / Abhängigkeiten

Das Projekt verwendet überwiegend Funktionsmodule statt einer großen Klassenhierarchie. Die wesentlichen Klassen sind die Worker- und SQLite-Verbindungsobjekte.

```mermaid
classDiagram
  class FlaskApp["app.py: Flask App / Route Controller"]
  class MQTT["mqtt_bambulab.py: MQTT Integration"]
  class Tracker["filament_usage_tracker.py: Filament Tracker"]
  class JobManager["jobs_3mf.py: Job3MFManager"]
  class History["print_history.py: History / Usage Repository"]
  class InventoryService["inventory_service.py: Inventory Service"]
  class InventoryRepo["inventory_repository.py: Local Repository Facade"]
  class InventoryDB["inventory_database.py: SQLite Catalog"]
  class DB["database.py: SQLite Connection / Schema"]
  class Retry["sqlite_retry.py: RetryingConnectionMixin"]
  class Tools3MF["tools_3mf.py: 3MF Parser / Transfer"]
  class Api["api_routes.py: REST Blueprint"]
  class Nfc["nfc_routes.py: NFC Blueprint"]
  class PendingNfc["nfc_pending_repository.py: Pending NFC Store"]
  class Logger["logger.py: Logging"]

  FlaskApp --> MQTT : Druckerzustand / Aktionen
  FlaskApp --> History : History-Routen und Ausgaben
  FlaskApp --> InventoryService : Inventar / AMS-Abfrage
  FlaskApp --> Api : registriert Blueprint
  FlaskApp --> Nfc : registriert Blueprint
  MQTT --> JobManager : Download einreihen / abbrechen
  MQTT --> Tracker : Status und AMS-Mapping
  MQTT --> History : Druckstatus persistieren
  JobManager --> Tools3MF : Datei laden / validieren / parsen
  JobManager --> Tracker : valide Metadaten übergeben
  Tracker --> Tools3MF : 3MF/G-Code/Layerdaten
  Tracker --> History : Events und Layerverbrauch
  Tracker --> InventoryService : lokale Spulenauflösung
  History --> DB : Drucke / Events / Usage
  InventoryService --> InventoryRepo : Stammdaten und Bestand
  InventoryRepo --> InventoryDB : lokale SQL-Operationen
  InventoryDB --> DB : SQLite-Verbindungen
  DB --> Retry : Connection-Retry
  Nfc --> PendingNfc : unbekannte Tags zwischenspeichern
  Nfc --> InventoryDB : Tag-/Spulenzuordnung
  Api --> InventoryService : lokaler Bestand und AMS
  MQTT --> Logger
  Tracker --> Logger
  JobManager --> Logger
```

### Verantwortlichkeiten

| Modul | Verantwortung | Wesentliche Abhängigkeiten |
|---|---|---|
| `app.py` | Flask-Einstieg, UI-Routen, Zusammensetzen der Dienste | MQTT, History, Inventory, Blueprints, Templates |
| `mqtt_bambulab.py` | MQTT-Verbindung, normalisierter Drucker-/AMS-Zustand, Druckereignisse und Befehle | Tracker, 3MF-Worker, History, Inventory-Service |
| `jobs_3mf.py` | Single-Flight-Warteschlange, Transferstatus, Retry/Abbruch | `tools_3mf`, Logger |
| `tools_3mf.py` | 3MF/G-Code lesen, validieren, FTP/Dateiübertragung und Vorschauen | Dateisystem, ZIP/XML, Logger |
| `filament_usage_tracker.py` | Layer-/Extrusionsbeobachtung, Mapping, Pufferung, Buchung und Reconciliation | History, Inventory, 3MF-Tools |
| `print_history.py` | Druckhistorie, Status, Spool-Usage und Verbrauchsevents persistieren/abfragen | `database.py`, lokale Filament-/Spulendaten |
| `inventory_service.py` | Fachlogik: Active-Tray, AMS-UID, Cache und Sperrregeln | Inventory-Repository, MQTT, History |
| `inventory_repository.py` | Schmale lokale Datenzugriffsfassade | `inventory_database.py` |
| `inventory_database.py` | Lokale Stammdaten und Spulenbestände | `database.py` |
| `database.py` | SQLite-Verbindungen, Schema und Migrationen | `sqlite_retry.py`, Konfiguration |
| `api_routes.py` | Versionierte JSON-API | MQTT, Inventory-Service |
| `nfc_routes.py` | NFC-Endpunkte, UID-Konfliktprüfung, Zuordnung | MQTT, Inventory-DB, Pending-Store |
| `local_settings_routes.py` | Lokale Drucker- und Anwendungseinstellungen | Konfiguration / DB |
| `ui_formatting.py`, `translations.py` | Zeitzonen-/Formatierungs- und Übersetzungshilfen | UI/Templates |

## 6. Zentrale Zustände und Bedingungen

| Zustand / Bedingung | Folge |
|---|---|
| Druck `PREPARE` | History-Datensatz/Metadaten vorbereiten; Filamentverbrauch puffern, bis Modell-Ausführung bestätigt ist |
| Druck `RUNNING` | Layertracking aktiv; Verbrauch nur anhand bestätigter, eindeutiger Ereignisse buchen |
| Druck `PAUSE` | Status pausiert; Zuordnungen/Verbrauch bleiben am Druckauftrag gebunden |
| Druck `FINISH` | abgeschlossene History und bestätigte Buchungen festschreiben; Tracker beenden |
| `STOP`/`FAILED` | Abbruch-/Fehlerstatus klassifizieren; nicht bestätigte Daten nicht als bestätigten Verbrauch ausgeben |
| G-Code fehlt/ungültig | Druckdaten nicht final verwenden; Transfer erneut versuchen bzw. Verbrauch puffern |
| AMS-Fach eindeutig | logisches Filament an physisches Fach und lokale Spule binden |
| AMS-Fach mehrdeutig/unbekannt | kein Farb-Raten; Verbrauch bleibt ungeklärt/gepuffert |
| NFC-Tag unbekannt | Pending-Tag erfassen, damit eine spätere Zuordnung möglich ist |
| Spule archiviert oder AMS-/NFC-gebunden | erlaubte Änderungen durch Inventarregeln begrenzen; Löschung/Verlagerung blockieren |

## 7. Datenfluss der Verbrauchsbuchung

```mermaid
sequenceDiagram
  participant P as Drucker
  participant M as mqtt_bambulab
  participant W as 3MF Worker
  participant T as Filament Tracker
  participant H as print_history
  participant I as Inventory Service
  participant D as SQLite
  P->>M: project_file / RUNNING / AMS Status
  M->>H: Druck-ID idempotent anlegen oder aktiven Eintrag finden
  M->>W: 3MF-Transfer einreihen
  W->>T: geprüfte 3MF-Metadaten + Prognose
  T->>I: Tray-UID nach lokaler Spule auflösen
  I->>D: Fach- und Spulenzuordnung lesen
  D-->>I: lokale Zuordnung
  I-->>T: Spule oder unresolved
  P->>M: Layer-/Extrusions- und Tray-Änderungen
  M->>T: Tracking-Snapshot
  T->>T: eindeutiges Mapping und Event-Dedupe prüfen
  alt Zuordnung bestätigt
    T->>H: Verbrauchssegment/Event buchen
    H->>D: idempotenter Datenbank-Write
    D-->>H: Buchung bestätigt
  else Mapping noch offen
    T->>T: Verbrauch zwischenpuffern
  end
  H-->>P: Status wird via Web-UI/API angezeigt
```

## 8. Einstiegspunkte zum Weiterlesen

- [Projektarchitektur](projektarchitektur.md)
- [Start und Betrieb](../START.md)
- [API-Referenz](../referenz/api.md)
- [NFC-Architektur](nfc.md)
- [Designentscheidungen](../../DESIGN.md)
