# m0BamboSpool – zentrale To-do-Liste

Diese Liste enthält nur noch offene oder ausdrücklich zu verifizierende Aufgaben.
Erledigte Punkte werden entfernt und nicht als erledigt weitergeführt.

## AMSHelper / NFC

- NDEF-URL-Lesen und -Schreiben für NTAG215 abschließen und auf dem Zielgerät testen.
- SPI-Pinbelegung für vier PN532 festlegen.
- PN532-SPI-Pfad zunächst mit einem Reader testen.
- Danach auf vier Reader mit gemeinsamem SPI-Bus und vier CS-Leitungen erweitern.
- Hotspot-Modus zum Öffnen beziehungsweise Anbieten eines WLAN-Hotspots implementieren,
  falls dieser weiterhin benötigt wird.

## m0BamboSpool / AMS-Kommunikation

- Unterschiede zwischen AMS-Status und Spoolman-Zuordnung abschließend behandeln und
  mit realen Druck-/Wechselvorgängen verifizieren.
- Verhalten bei einem durch Bambu zurückgesetzten External-Spool-Zustand abschließend
  korrigieren; dazu gehören Wiederherstellung, Zuordnung und Logging.
- External-Spool-Druckstatus und Zuordnung bei laufenden und beendeten Jobs verifizieren.
- Profil-/PA-Index sowie Filamentwechsel über mehrere reale AMS-Antworten verifizieren.

## Weboberfläche / Verlauf

- Print-History gegen alle gewünschten Zustände, Filter, Sortierungen, Anker-Sprünge und
  UI-Aktionen vollständig testen.
- Automatische Aktualisierung ausschließlich während laufender Druckjobs verifizieren.
- Inventar-Links zu Spool, Hersteller und Filament mit den jeweiligen Spoolman-IDs testen.
- Mehrsprachigkeit auf allen Seiten und bei allen sichtbaren Buttons vollständig prüfen.

## Wartung / Betrieb

- Docker-Log-Leerung beim Recreate dokumentieren und prüfen.
- Versionierung bei Releases konsistent aktualisieren.
