# m0BamboSpool – Dokumentation

Diese Datei ist der einzige Einstiegspunkt für Projektkontext, Entwicklungsregeln und Architekturentscheidungen.

**Regel für Menschen und Coding-Agenten:** Vor Änderungen zuerst diese Datei lesen und anschließend nur die für die Aufgabe verlinkten Dokumente.

## Verbindliche Reihenfolge

1. [AGENTS.md](../AGENTS.md) – einzige verbindliche Arbeits-, Branch- und Agentenregel.
2. [Design](../DESIGN.md) – verbindliche technische, organisatorische und Formatierungsentscheidungen.
3. Die zur Aufgabe passende Fachdokumentation.
4. Vor Änderungen immer den aktuellen Stand von `dev` beziehungsweise des zugehörigen Feature-/Bug-Branches prüfen.

Bei Widersprüchen gilt: aktuelle Nutzeranweisung → `DESIGN.md` → aktueller Repository-Code für den Implementierungsstand → Fachdokumentation → historische Beschreibung.

## Projektarchitektur

- [Gesamtarchitektur](architektur/projektarchitektur.md)
- [NFC-Architektur](architektur/nfc.md)
- [API](referenz/api.md)

## AMSHelper / ESP32-S3

- [PN532 / NTAG215](amshelper/pn532-ntag215.md)
- [Hardware](amshelper/hardware.md)
- [nanoFramework-Build](amshelper/nanoframework-build.md)

## Bambu Lab

- [Authentifizierung](bambu/authentifizierung.md)
- [MQTT und Signierung](bambu/mqtt-signierung.md)

## Referenz

- [API](referenz/api.md)
- [Screenshots](referenz/screenshots.md)

## Dokumentationsregeln

- Markdown-Dateien werden mit Windows-Zeilenenden **CRLF** gespeichert.
- `AGENTS.md` im Repository-Root ist die einzige verbindliche Arbeits- und Agentenregel.
- Dauerhafte Architektur-, Hardware-, Software-, UI-, Formatierungs- und Workflowentscheidungen werden in `DESIGN.md` gepflegt.
- Detaildokumente beschreiben Zustand und Umsetzung und definieren keine konkurrierende Zielarchitektur.
- Veraltete Alternativen werden entfernt oder ausdrücklich als verworfen markiert.
- Änderungen werden auf `feature/<name>` oder `bug/<name>` von `dev` abgeleitet, geprüft und anschließend in `dev` integriert. Der Präfix `codex/` wird nicht verwendet.
